"""
Journey Lifecycle Scheduler — Feature 3.

Background task that continuously monitors journey statuses and transitions them:
- CONFIRMED → IN_PROGRESS at `departure_time`
- IN_PROGRESS → COMPLETED at `estimated_arrival_time`

Also publishes corresponding events (`journey.started`, `journey.completed`).
"""

import asyncio
import logging
from datetime import datetime

from sqlalchemy import select, and_

from .database import Journey, init_db, async_session
from .saga import BookingSaga
from shared.schemas import JourneyStatus, EventType

logger = logging.getLogger(__name__)


async def transition_journeys():
    """Poll for journeys that need state transitions."""
    while True:
        try:
            await _run_transitions()
        except Exception as e:
            logger.error(f"Error in journey lifecycle scheduler: {e}", exc_info=True)
        
        # Sleep for 60 seconds before next poll
        await asyncio.sleep(60)


async def _run_transitions():
    async with async_session() as db:
        now = datetime.utcnow()

        # 1. Start journeys: CONFIRMED -> IN_PROGRESS
        started_query = select(Journey).where(
            and_(
                Journey.status == JourneyStatus.CONFIRMED.value,
                Journey.departure_time <= now
            )
        )
        started_result = await db.execute(started_query)
        to_start = started_result.scalars().all()

        for j in to_start:
            j.status = JourneyStatus.IN_PROGRESS.value
            logger.info(f"Transitioning journey {j.id} to IN_PROGRESS")
            await BookingSaga.publish_journey_event(j, EventType.JOURNEY_STARTED)

        # 2. Complete journeys: IN_PROGRESS -> COMPLETED
        completed_query = select(Journey).where(
            and_(
                Journey.status == JourneyStatus.IN_PROGRESS.value,
                Journey.estimated_arrival_time <= now
            )
        )
        completed_result = await db.execute(completed_query)
        to_complete = completed_result.scalars().all()

        for j in to_complete:
            j.status = JourneyStatus.COMPLETED.value
            logger.info(f"Transitioning journey {j.id} to COMPLETED")
            await BookingSaga.publish_journey_event(j, EventType.JOURNEY_COMPLETED)

            # Award points for completing a journey
            try:
                from .points import PointsService, POINTS_PER_COMPLETED_JOURNEY
                await PointsService.earn_points(
                    db, j.user_id, POINTS_PER_COMPLETED_JOURNEY,
                    "JOURNEY_COMPLETED", j.id
                )
            except Exception as e:
                logger.warning(f"Failed to award completion points for {j.id}: {e}")

        if to_start or to_complete:
            await db.commit()
