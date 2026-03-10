"""
Notification Service - RabbitMQ consumer and notification delivery.

Consumes journey events and:
1. Logs notifications (simulates email/SMS/push delivery)
2. Pushes real-time updates to connected WebSocket clients
3. Stores notification history in Redis for client retrieval
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

import redis.asyncio as redis_async

from shared.messaging import MessageBroker, NOTIFICATION_QUEUE
from shared.schemas import EventType

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/3")
redis_client = redis_async.from_url(REDIS_URL, decode_responses=True)

# WebSocket connections tracked by user_id
_ws_connections: dict[str, list] = {}


def register_ws(user_id: str, ws):
    """Register a WebSocket connection for a user."""
    if user_id not in _ws_connections:
        _ws_connections[user_id] = []
    _ws_connections[user_id].append(ws)
    logger.info(f"WebSocket registered for user {user_id} (total: {len(_ws_connections[user_id])})")


def unregister_ws(user_id: str, ws):
    """Unregister a WebSocket connection."""
    if user_id in _ws_connections:
        _ws_connections[user_id] = [w for w in _ws_connections[user_id] if w != ws]
        if not _ws_connections[user_id]:
            del _ws_connections[user_id]


EVENT_MESSAGES = {
    EventType.JOURNEY_CONFIRMED.value: {
        "title": "Journey Confirmed ✅",
        "template": "Your journey from {origin} to {destination} at {departure_time} has been confirmed.",
    },
    EventType.JOURNEY_REJECTED.value: {
        "title": "Journey Rejected ❌",
        "template": "Your journey from {origin} to {destination} was rejected. Reason: {rejection_reason}",
    },
    EventType.JOURNEY_CANCELLED.value: {
        "title": "Journey Cancelled 🚫",
        "template": "Your journey from {origin} to {destination} at {departure_time} has been cancelled.",
    },
    EventType.JOURNEY_STARTED.value: {
        "title": "Journey Started 🚗",
        "template": "Your journey from {origin} to {destination} has started. Drive safely!",
    },
    EventType.JOURNEY_COMPLETED.value: {
        "title": "Journey Completed 🏁",
        "template": "Your journey from {origin} to {destination} is complete.",
    },
}


async def handle_event(data: dict, routing_key: str):
    """Process an incoming journey event and create/deliver a notification."""
    user_id = data.get("user_id")
    if not user_id:
        logger.warning(f"Event {routing_key} has no user_id, skipping")
        return

    # Get notification template
    msg_template = EVENT_MESSAGES.get(routing_key)
    if not msg_template:
        logger.debug(f"No notification template for {routing_key}")
        return

    title = msg_template["title"]
    message = msg_template["template"].format(
        origin=data.get("origin", "Unknown"),
        destination=data.get("destination", "Unknown"),
        departure_time=data.get("departure_time", "Unknown"),
        rejection_reason=data.get("rejection_reason", "N/A"),
    )

    notification = {
        "event_type": routing_key,
        "title": title,
        "message": message,
        "journey_id": data.get("journey_id"),
        "timestamp": datetime.utcnow().isoformat(),
    }

    # 1. Log the notification (simulated delivery)
    logger.info(f"📧 NOTIFICATION for user {user_id}: {title} — {message}")

    # 2. Store in Redis for later retrieval (list, max 50 per user)
    try:
        key = f"notifications:{user_id}"
        await redis_client.lpush(key, json.dumps(notification))
        await redis_client.ltrim(key, 0, 49)
        await redis_client.expire(key, 86400 * 7)  # 7 days TTL
    except Exception as e:
        logger.warning(f"Failed to store notification in Redis: {e}")

    # 3. Push to WebSocket if user is connected
    if user_id in _ws_connections:
        disconnected = []
        for ws in _ws_connections[user_id]:
            try:
                await ws.send_json(notification)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            unregister_ws(user_id, ws)


async def get_notifications(user_id: str, limit: int = 20) -> list[dict]:
    """Retrieve stored notifications for a user."""
    try:
        key = f"notifications:{user_id}"
        raw = await redis_client.lrange(key, 0, limit - 1)
        return [json.loads(n) for n in raw]
    except Exception as e:
        logger.warning(f"Failed to retrieve notifications from Redis: {e}")
        return []


async def start_consumer(broker: MessageBroker):
    """Start consuming notification-relevant events."""
    await broker.subscribe(
        queue_name=NOTIFICATION_QUEUE,
        routing_keys=[
            EventType.JOURNEY_CONFIRMED.value,
            EventType.JOURNEY_REJECTED.value,
            EventType.JOURNEY_CANCELLED.value,
            EventType.JOURNEY_STARTED.value,
            EventType.JOURNEY_COMPLETED.value,
        ],
        callback=handle_event,
    )
    logger.info("Notification consumer started")
