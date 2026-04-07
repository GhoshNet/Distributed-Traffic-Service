"""
Replica Recovery — Handles node/service recovery after crashes or partitions.

Recovery strategies:
1. **PostgreSQL streaming replica**: Auto-recovers via WAL replay from primary.
   The replica catches up automatically when reconnected — no manual intervention.

2. **Redis replica + Sentinel**: Sentinel promotes the replica to primary on failure.
   When the failed node returns, it joins as a replica and syncs from the new primary.

3. **Service-level recovery**: After a service restarts, it:
   - Reconnects to its database (tables already exist via `init_db`)
   - Reconnects to RabbitMQ with `connect_robust()` (auto-reconnect)
   - Drains any accumulated outbox events to RabbitMQ
   - Rebuilds in-memory state (e.g., enforcement cache from events)

4. **Total failure recovery**: If all services crash simultaneously:
   - PostgreSQL data is on persistent Docker volumes — survives restarts
   - RabbitMQ has durable queues and persistent messages — survives restarts
   - Redis has AOF persistence — recovers data on restart
   - Outbox events in the DB are published once services reconnect
   - HMAC audit chain in analytics can be verified to detect any data loss

This module provides utility functions for recovery operations.
"""

import logging
import json
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


async def rebuild_enforcement_cache(redis_client, journey_db_session_factory):
    """
    Rebuild the enforcement Redis cache from the journeys database.

    Called after:
    - Redis data loss (FLUSHALL, restart without AOF)
    - Enforcement service restart
    - Partition heal between enforcement and journey service

    Scans all CONFIRMED/IN_PROGRESS journeys and re-populates the cache.
    """
    from sqlalchemy import select, and_

    logger.info("Rebuilding enforcement cache from journeys database...")
    count = 0

    async with journey_db_session_factory() as db:
        # Import here to avoid circular dependency in shared module
        now = datetime.utcnow()
        query = """
            SELECT id, user_id, origin, destination, departure_time,
                   estimated_arrival_time, vehicle_registration, status
            FROM journeys
            WHERE status IN ('CONFIRMED', 'IN_PROGRESS')
              AND estimated_arrival_time >= :now
        """
        from sqlalchemy import text
        result = await db.execute(text(query), {"now": now})
        rows = result.fetchall()

        pipe = redis_client.pipeline()
        for row in rows:
            journey_id, user_id, origin, destination, departure, arrival, vehicle_reg, status = row
            ttl = int((arrival - now).total_seconds()) + 3600
            if ttl <= 0:
                continue

            cache_data = json.dumps({
                "journey_id": journey_id,
                "user_id": user_id,
                "origin": origin,
                "destination": destination,
                "departure_time": departure.isoformat(),
                "estimated_arrival_time": arrival.isoformat(),
                "vehicle_registration": vehicle_reg,
                "status": status,
            })

            pipe.setex(f"active_journey:vehicle:{vehicle_reg}", ttl, cache_data)
            if user_id:
                pipe.setex(f"active_journey:user:{user_id}", ttl, cache_data)
            count += 1

        await pipe.execute()

    logger.info(f"Enforcement cache rebuilt: {count} active journeys cached")
    return count


async def verify_data_consistency(analytics_db_session_factory, hmac_secret: bytes) -> dict:
    """
    Verify data consistency after recovery by checking the analytics audit chain.

    Returns a report of the verification including any gaps or corrupted entries
    that may indicate data loss during the failure period.
    """
    import hmac as hmac_lib
    import hashlib
    from sqlalchemy import select, func

    logger.info("Verifying data consistency via audit chain...")

    async with analytics_db_session_factory() as db:
        from sqlalchemy import text
        result = await db.execute(text(
            "SELECT id, event_type, prev_hash, event_hash, metadata_json, created_at "
            "FROM event_logs ORDER BY created_at ASC"
        ))
        events = result.fetchall()

    total = len(events)
    valid = 0
    gaps = []
    corrupted = []
    expected_prev_hash = "0" * 64

    for e in events:
        event_id, event_type, prev_hash, event_hash, metadata_json, created_at = e

        if prev_hash != expected_prev_hash:
            gaps.append({
                "event_id": event_id,
                "expected_prev": expected_prev_hash[:16] + "...",
                "actual_prev": (prev_hash or "NULL")[:16] + "...",
            })

        payload = f"{event_id}|{event_type}|{prev_hash}|{metadata_json}".encode()
        computed = hmac_lib.new(hmac_secret, payload, hashlib.sha256).hexdigest()

        if computed != event_hash:
            corrupted.append(event_id)
        else:
            valid += 1

        expected_prev_hash = event_hash or expected_prev_hash

    report = {
        "total_events": total,
        "valid_events": valid,
        "chain_gaps": len(gaps),
        "corrupted_events": len(corrupted),
        "is_consistent": len(gaps) == 0 and len(corrupted) == 0,
        "gap_details": gaps[:10],
        "corrupted_ids": corrupted[:10],
    }

    if report["is_consistent"]:
        logger.info(f"Audit chain verified: {total} events, all consistent")
    else:
        logger.warning(
            f"Audit chain issues: {len(gaps)} gaps, {len(corrupted)} corrupted "
            f"out of {total} events"
        )

    return report


async def drain_outbox_backlog(journey_db_session_factory, broker) -> int:
    """
    Force-drain all unpublished outbox events after recovery.

    Normally the outbox publisher runs on a 2-second poll interval,
    but after a total failure recovery, we want to drain immediately
    to restore eventual consistency as fast as possible.
    """
    from sqlalchemy import select

    logger.info("Draining outbox backlog after recovery...")
    count = 0

    async with journey_db_session_factory() as db:
        from sqlalchemy import text
        result = await db.execute(text(
            "SELECT id, routing_key, payload FROM outbox_events "
            "WHERE published = false ORDER BY created_at ASC"
        ))
        events = result.fetchall()

        for event_id, routing_key, payload in events:
            try:
                data = json.loads(payload)
                await broker.publish(routing_key=routing_key, data=data)
                await db.execute(
                    text("UPDATE outbox_events SET published = true WHERE id = :id"),
                    {"id": event_id}
                )
                count += 1
            except Exception as e:
                logger.warning(f"Failed to drain outbox event {event_id}: {e}")
                break

        await db.commit()

    logger.info(f"Outbox backlog drained: {count} events published")
    return count
