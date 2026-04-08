"""
Enforcement Service - Event consumer.

Subscribes to journey events via RabbitMQ and maintains a local
Redis cache (DB 4) of active journeys for fast enforcement lookups.
This ensures the enforcement service owns its own data rather than
reading from another service's Redis namespace.
"""

import json
import logging
import os
from datetime import datetime, timedelta

import redis.asyncio as redis_async
from redis.asyncio.sentinel import Sentinel as AsyncSentinel

from shared.messaging import MessageBroker, ENFORCEMENT_QUEUE

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/4")
_SENTINEL_ADDRS = os.getenv("REDIS_SENTINEL_ADDRS", "")
_MASTER_NAME = os.getenv("REDIS_MASTER_NAME", "mymaster")


def _make_redis_client() -> redis_async.Redis:
    if _SENTINEL_ADDRS:
        hosts = [
            (h.split(":")[0], int(h.split(":")[1]))
            for h in _SENTINEL_ADDRS.split(",")
        ]
        sentinel = AsyncSentinel(hosts)
        return sentinel.master_for(_MASTER_NAME, db=4, decode_responses=True)
    return redis_async.from_url(REDIS_URL, decode_responses=True)


redis_client = _make_redis_client()


async def handle_journey_event(data: dict, routing_key: str):
    """Process journey events to maintain the enforcement cache."""
    logger.info(f"Enforcement received event: {routing_key}")

    journey_id = data.get("journey_id")
    vehicle_reg = data.get("vehicle_registration")
    user_id = data.get("user_id")

    if not journey_id or not vehicle_reg:
        return

    if routing_key in ("journey.confirmed", "journey.started"):
        # Cache the active journey for fast lookup
        arrival_str = data.get("estimated_arrival_time")
        if not arrival_str:
            return

        arrival = datetime.fromisoformat(arrival_str)
        ttl = int((arrival - datetime.utcnow()).total_seconds()) + 3600
        if ttl <= 0:
            return

        cache_data = json.dumps({
            "journey_id": journey_id,
            "user_id": user_id,
            "origin": data.get("origin"),
            "destination": data.get("destination"),
            "departure_time": data.get("departure_time"),
            "estimated_arrival_time": arrival_str,
            "vehicle_registration": vehicle_reg,
            "status": data.get("status"),
        })

        pipe = redis_client.pipeline()
        pipe.setex(f"active_journey:vehicle:{vehicle_reg}", ttl, cache_data)
        if user_id:
            pipe.setex(f"active_journey:user:{user_id}", ttl, cache_data)
        await pipe.execute()

        logger.debug(f"Cached journey {journey_id} in enforcement Redis (TTL={ttl}s)")

    elif routing_key in ("journey.cancelled", "journey.completed"):
        # Remove from cache
        pipe = redis_client.pipeline()
        pipe.delete(f"active_journey:vehicle:{vehicle_reg}")
        if user_id:
            pipe.delete(f"active_journey:user:{user_id}")
        await pipe.execute()

        logger.debug(f"Removed journey {journey_id} from enforcement cache")


async def start_consumer(broker: MessageBroker):
    """Start consuming journey events for enforcement cache."""
    await broker.subscribe(
        queue_name=ENFORCEMENT_QUEUE,
        routing_keys=[
            "journey.confirmed",
            "journey.started",
            "journey.cancelled",
            "journey.completed",
        ],
        callback=handle_journey_event,
    )
    logger.info("Enforcement event consumer started")
