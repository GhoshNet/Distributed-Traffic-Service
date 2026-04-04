"""
Journey Service - Business logic layer.
"""

import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional

import redis.asyncio as redis_async
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, and_, or_, func

from .database import Journey, IdempotencyRecord
from .saga import BookingSaga
from shared.schemas import (
    JourneyCreateRequest,
    JourneyResponse,
    JourneyListResponse,
    JourneyStatus,
    EventType,
)

logger = logging.getLogger(__name__)

# Redis for caching active journeys
import os
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/1")
redis_client = redis_async.from_url(REDIS_URL, decode_responses=True)


class JourneyService:
    """Handles journey CRUD and orchestrates the booking saga."""

    @staticmethod
    async def create_journey(
        db: AsyncSession, user_id: str, request: JourneyCreateRequest
    ) -> JourneyResponse:
        """Create a new journey booking (triggers the booking saga)."""

        # Idempotency check
        if request.idempotency_key:
            existing = await db.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.key == request.idempotency_key
                )
            )
            record = existing.scalar_one_or_none()
            if record:
                # Return existing journey
                return await JourneyService.get_journey(db, record.journey_id, user_id)

        journey_id = str(uuid.uuid4())
        departure_naive = request.departure_time.replace(tzinfo=None)
        estimated_arrival = departure_naive + timedelta(
            minutes=request.estimated_duration_minutes
        )

        # Create journey with PENDING status
        journey = Journey(
            id=journey_id,
            user_id=user_id,
            origin=request.origin,
            destination=request.destination,
            origin_lat=request.origin_lat,
            origin_lng=request.origin_lng,
            destination_lat=request.destination_lat,
            destination_lng=request.destination_lng,
            departure_time=departure_naive,
            estimated_duration_minutes=request.estimated_duration_minutes,
            estimated_arrival_time=estimated_arrival,
            vehicle_registration=request.vehicle_registration,
            vehicle_type=request.vehicle_type.value,
            status=JourneyStatus.PENDING.value,
            idempotency_key=request.idempotency_key,
        )

        db.add(journey)
        await db.commit()
        await db.refresh(journey)

        # Save idempotency record
        if request.idempotency_key:
            db.add(IdempotencyRecord(key=request.idempotency_key, journey_id=journey_id))
            await db.commit()

        logger.info(f"Journey {journey_id} created as PENDING for user {user_id}")

        # Execute the booking saga
        final_status, rejection_reason = await BookingSaga.execute(journey)

        # Update journey with saga result
        journey.status = final_status.value
        journey.rejection_reason = rejection_reason
        await db.commit()
        await db.refresh(journey)

        # Determine event type
        if final_status == JourneyStatus.CONFIRMED:
            event_type = EventType.JOURNEY_CONFIRMED
            # Cache in Redis for enforcement service
            await JourneyService._cache_active_journey(journey)
        else:
            event_type = EventType.JOURNEY_REJECTED

        # Publish event asynchronously
        await BookingSaga.publish_journey_event(journey, event_type)

        logger.info(f"Journey {journey_id} final status: {final_status.value}")

        return JourneyService._to_response(journey)

    @staticmethod
    async def get_journey(db: AsyncSession, journey_id: str, user_id: str) -> JourneyResponse:
        """Get a specific journey by ID (must belong to user)."""
        result = await db.execute(
            select(Journey).where(
                and_(Journey.id == journey_id, Journey.user_id == user_id)
            )
        )
        journey = result.scalar_one_or_none()
        if not journey:
            raise ValueError("Journey not found")
        return JourneyService._to_response(journey)

    @staticmethod
    async def list_journeys(
        db: AsyncSession,
        user_id: str,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> JourneyListResponse:
        """List journeys for a user with optional status filter and pagination."""
        query = select(Journey).where(Journey.user_id == user_id)

        if status:
            query = query.where(Journey.status == status)

        # Count total
        count_query = select(func.count()).select_from(
            query.subquery()
        )
        total = (await db.execute(count_query)).scalar()

        # Paginate
        query = (
            query.order_by(Journey.departure_time.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )

        result = await db.execute(query)
        journeys = result.scalars().all()

        return JourneyListResponse(
            journeys=[JourneyService._to_response(j) for j in journeys],
            total=total,
            page=page,
            page_size=page_size,
        )

    @staticmethod
    async def cancel_journey(db: AsyncSession, journey_id: str, user_id: str) -> JourneyResponse:
        """Cancel a journey. Only CONFIRMED or PENDING journeys can be cancelled."""
        result = await db.execute(
            select(Journey).where(
                and_(Journey.id == journey_id, Journey.user_id == user_id)
            )
        )
        journey = result.scalar_one_or_none()
        if not journey:
            raise ValueError("Journey not found")

        if journey.status not in (JourneyStatus.CONFIRMED.value, JourneyStatus.PENDING.value):
            raise ValueError(f"Cannot cancel journey with status {journey.status}")

        journey.status = JourneyStatus.CANCELLED.value
        await db.commit()
        await db.refresh(journey)

        # Remove from Redis cache
        await JourneyService._remove_cached_journey(journey)

        # Publish cancellation event
        await BookingSaga.publish_journey_event(journey, EventType.JOURNEY_CANCELLED)

        logger.info(f"Journey {journey_id} cancelled by user {user_id}")
        return JourneyService._to_response(journey)

    @staticmethod
    async def get_active_journeys_for_vehicle(
        db: AsyncSession, vehicle_registration: str
    ) -> list[JourneyResponse]:
        """Get active (CONFIRMED/IN_PROGRESS) journeys for a vehicle. Used by enforcement."""
        now = datetime.utcnow()
        result = await db.execute(
            select(Journey).where(
                and_(
                    Journey.vehicle_registration == vehicle_registration,
                    Journey.status.in_([
                        JourneyStatus.CONFIRMED.value,
                        JourneyStatus.IN_PROGRESS.value,
                    ]),
                    Journey.departure_time <= now + timedelta(minutes=30),  # Allow 30min early
                    Journey.estimated_arrival_time >= now,
                )
            )
        )
        journeys = result.scalars().all()
        return [JourneyService._to_response(j) for j in journeys]

    @staticmethod
    async def get_active_journeys_for_user(
        db: AsyncSession, user_id: str
    ) -> list[JourneyResponse]:
        """Get active (CONFIRMED/IN_PROGRESS) journeys for a user. Used by enforcement."""
        now = datetime.utcnow()
        result = await db.execute(
            select(Journey).where(
                and_(
                    Journey.user_id == user_id,
                    Journey.status.in_([
                        JourneyStatus.CONFIRMED.value,
                        JourneyStatus.IN_PROGRESS.value,
                    ]),
                    Journey.departure_time <= now + timedelta(minutes=30),
                    Journey.estimated_arrival_time >= now,
                )
            )
        )
        journeys = result.scalars().all()
        return [JourneyService._to_response(j) for j in journeys]

    # ==========================================
    # Redis Caching for Enforcement
    # ==========================================

    @staticmethod
    async def _cache_active_journey(journey: Journey):
        """Cache confirmed journey in Redis for fast enforcement lookups."""
        try:
            import json
            key = f"active_journey:vehicle:{journey.vehicle_registration}"
            ttl = int((journey.estimated_arrival_time - datetime.utcnow()).total_seconds()) + 3600
            if ttl > 0:
                data = {
                    "journey_id": journey.id,
                    "user_id": journey.user_id,
                    "origin": journey.origin,
                    "destination": journey.destination,
                    "departure_time": journey.departure_time.isoformat(),
                    "estimated_arrival_time": journey.estimated_arrival_time.isoformat(),
                    "vehicle_registration": journey.vehicle_registration,
                    "status": journey.status,
                }
                await redis_client.setex(key, ttl, json.dumps(data))
                # Also cache by user_id
                user_key = f"active_journey:user:{journey.user_id}"
                await redis_client.setex(user_key, ttl, json.dumps(data))
                logger.debug(f"Cached active journey {journey.id} in Redis (TTL={ttl}s)")
        except Exception as e:
            logger.warning(f"Failed to cache journey in Redis: {e}")

    @staticmethod
    async def _remove_cached_journey(journey: Journey):
        """Remove a journey from Redis cache."""
        try:
            await redis_client.delete(
                f"active_journey:vehicle:{journey.vehicle_registration}",
                f"active_journey:user:{journey.user_id}",
            )
            logger.debug(f"Removed journey {journey.id} from Redis cache")
        except Exception as e:
            logger.warning(f"Failed to remove journey from Redis: {e}")

    # ==========================================
    # Helpers
    # ==========================================

    @staticmethod
    def _to_response(journey: Journey) -> JourneyResponse:
        return JourneyResponse(
            id=journey.id,
            user_id=journey.user_id,
            origin=journey.origin,
            destination=journey.destination,
            origin_lat=journey.origin_lat,
            origin_lng=journey.origin_lng,
            destination_lat=journey.destination_lat,
            destination_lng=journey.destination_lng,
            departure_time=journey.departure_time,
            estimated_duration_minutes=journey.estimated_duration_minutes,
            estimated_arrival_time=journey.estimated_arrival_time,
            vehicle_registration=journey.vehicle_registration,
            vehicle_type=journey.vehicle_type,
            status=JourneyStatus(journey.status),
            rejection_reason=journey.rejection_reason,
            created_at=journey.created_at,
            updated_at=journey.updated_at,
        )
