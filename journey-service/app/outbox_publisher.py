"""
Outbox Publisher — Background task that drains the outbox table
and publishes events to RabbitMQ.

This is the second half of the transactional outbox pattern.
Events are written to the outbox table atomically with the journey
status update, then this task polls for unpublished events and
publishes them with retry logic.

Guarantees at-least-once delivery. Consumers must be idempotent.
"""

import json
import asyncio
import logging

from sqlalchemy import select, update

from .database import OutboxEvent, async_session
from shared.messaging import get_broker

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2
BATCH_SIZE = 50


async def run_outbox_publisher():
    """Continuously poll the outbox table and publish pending events."""
    while True:
        try:
            await _publish_pending_events()
        except Exception as e:
            logger.error(f"Outbox publisher error: {e}", exc_info=True)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _publish_pending_events():
    """Fetch unpublished outbox events and publish them to RabbitMQ."""
    async with async_session() as db:
        result = await db.execute(
            select(OutboxEvent)
            .where(OutboxEvent.published == False)
            .order_by(OutboxEvent.created_at.asc())
            .limit(BATCH_SIZE)
        )
        events = result.scalars().all()

        if not events:
            return

        broker = await get_broker()

        for event in events:
            try:
                payload = json.loads(event.payload)
                await broker.publish(
                    routing_key=event.routing_key,
                    data=payload,
                )
                event.published = True
                logger.info(f"Outbox published: {event.routing_key} (id={event.id})")
            except Exception as e:
                logger.warning(
                    f"Outbox publish failed for {event.id}: {e} — will retry"
                )
                break

        await db.commit()
