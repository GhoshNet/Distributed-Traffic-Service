"""
Analytics & Monitoring Service - Event consumer and statistics.

Consumes ALL journey events from RabbitMQ and:
1. Logs them to the event_logs table for historical analysis
2. Updates hourly aggregate statistics
3. Maintains real-time counters in Redis
"""

import json
import uuid
import os
import logging
from datetime import datetime, timedelta

import redis.asyncio as redis_async
from sqlalchemy import select, func, and_

from .database import EventLog, HourlyStats, async_session
from shared.messaging import MessageBroker, ANALYTICS_QUEUE
from shared.schemas import EventType

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/5")
redis_client = redis_async.from_url(REDIS_URL, decode_responses=True)


async def handle_event(data: dict, routing_key: str):
    """Process an incoming event for analytics."""
    logger.info(f"Analytics received event: {routing_key}")

    # Store event in database
    async with async_session() as db:
        event = EventLog(
            id=str(uuid.uuid4()),
            event_type=routing_key,
            journey_id=data.get("journey_id"),
            user_id=data.get("user_id"),
            origin=data.get("origin"),
            destination=data.get("destination"),
            metadata_json=json.dumps(data),
        )
        db.add(event)
        await db.commit()

    # Update Redis counters for real-time stats
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        counter_key = f"analytics:daily:{today}"

        pipe = redis_client.pipeline()
        pipe.hincrby(counter_key, "total_events", 1)
        pipe.hincrby(counter_key, routing_key, 1)
        pipe.expire(counter_key, 86400 * 2)  # 2 days TTL
        await pipe.execute()
    except Exception as e:
        logger.warning(f"Failed to update Redis counters: {e}")


async def get_system_stats() -> dict:
    """Get real-time system statistics from Redis + DB."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    stats = {
        "total_events_today": 0,
        "confirmed_today": 0,
        "rejected_today": 0,
        "cancelled_today": 0,
    }

    try:
        counter_key = f"analytics:daily:{today}"
        data = await redis_client.hgetall(counter_key)
        stats["total_events_today"] = int(data.get("total_events", 0))
        stats["confirmed_today"] = int(data.get(EventType.JOURNEY_CONFIRMED.value, 0))
        stats["rejected_today"] = int(data.get(EventType.JOURNEY_REJECTED.value, 0))
        stats["cancelled_today"] = int(data.get(EventType.JOURNEY_CANCELLED.value, 0))
    except Exception as e:
        logger.warning(f"Failed to get Redis stats: {e}")

    # Get historical stats from DB
    async with async_session() as db:
        result = await db.execute(
            select(func.count()).select_from(EventLog)
        )
        stats["total_events_all_time"] = result.scalar() or 0

        # Events in last hour
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        result = await db.execute(
            select(func.count()).select_from(EventLog).where(
                EventLog.created_at >= one_hour_ago
            )
        )
        stats["events_last_hour"] = result.scalar() or 0

    return stats


async def get_event_history(
    event_type: str = None, limit: int = 50, offset: int = 0
) -> list[dict]:
    """Get event history with optional filtering."""
    async with async_session() as db:
        query = select(EventLog).order_by(EventLog.created_at.desc())
        if event_type:
            query = query.where(EventLog.event_type == event_type)
        query = query.offset(offset).limit(limit)

        result = await db.execute(query)
        events = result.scalars().all()

        return [
            {
                "id": e.id,
                "event_type": e.event_type,
                "journey_id": e.journey_id,
                "user_id": e.user_id,
                "origin": e.origin,
                "destination": e.destination,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]


async def start_consumer(broker: MessageBroker):
    """Start consuming ALL journey events for analytics."""
    await broker.subscribe(
        queue_name=ANALYTICS_QUEUE,
        routing_keys=[
            "journey.*",   # All journey events
            "user.*",      # All user events
        ],
        callback=handle_event,
    )
    logger.info("Analytics consumer started")
