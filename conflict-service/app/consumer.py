"""
Conflict Detection Service - RabbitMQ consumer.

Listens for journey cancellation events to deactivate booking slots.
"""

import logging
from .service import ConflictDetectionService
from .database import async_session
from shared.messaging import MessageBroker, EVENTS_EXCHANGE
from shared.schemas import EventType

logger = logging.getLogger(__name__)

CONFLICT_QUEUE = "conflict_cancellation_events"


async def handle_event(data: dict, routing_key: str):
    """Handle incoming events from RabbitMQ."""
    logger.info(f"Received event: {routing_key}")

    if routing_key == EventType.JOURNEY_CANCELLED.value:
        journey_id = data.get("journey_id")
        if journey_id:
            async with async_session() as db:
                await ConflictDetectionService.cancel_booking_slot(db, journey_id)
                logger.info(f"Processed cancellation for journey {journey_id}")


async def start_consumer(broker: MessageBroker):
    """Start consuming relevant events."""
    await broker.subscribe(
        queue_name=CONFLICT_QUEUE,
        routing_keys=[
            EventType.JOURNEY_CANCELLED.value,
        ],
        callback=handle_event,
    )
    logger.info("Conflict service consumer started")
