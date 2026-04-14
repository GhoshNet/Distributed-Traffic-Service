"""
Journey Service - Business logic layer.
"""

import os
import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func

from .database import Journey, IdempotencyRecord
from .saga import BookingSaga
from .coordinator import TwoPhaseCoordinator
from shared.schemas import (
    JourneyCreateRequest,
    JourneyResponse,
    JourneyListResponse,
    JourneyStatus,
    EventType,
)

logger = logging.getLogger(__name__)

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user-service:8000")


class JourneyService:
    """Handles journey CRUD and orchestrates the booking saga."""

    @staticmethod
    async def create_journey(
        db: AsyncSession, user_id: str, request: JourneyCreateRequest,
        use_2pc: bool = False,
        user_name: str = "",
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

        # Vehicle ownership verification
        await JourneyService._verify_vehicle_ownership(
            user_id, request.vehicle_registration
        )

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
            route_id=request.route_id,
            idempotency_key=request.idempotency_key,
        )

        db.add(journey)
        await db.commit()
        await db.refresh(journey)

        # Save idempotency record
        if request.idempotency_key:
            db.add(IdempotencyRecord(key=request.idempotency_key, journey_id=journey_id))
            await db.commit()

        logger.info(
            f"Journey {journey_id} created as PENDING for user {user_id} "
            f"(protocol={'2PC' if use_2pc else 'Saga'})"
        )

        # Execute the booking saga OR Two-Phase Commit coordinator
        if use_2pc:
            final_status, rejection_reason = await TwoPhaseCoordinator.execute(journey, db, user_name=user_name)
        else:
            final_status, rejection_reason = await BookingSaga.execute(journey)

        # Determine event type
        if final_status == JourneyStatus.CONFIRMED:
            event_type = EventType.JOURNEY_CONFIRMED
        else:
            event_type = EventType.JOURNEY_REJECTED

        # Update journey status AND write outbox event in the SAME transaction
        # This is the transactional outbox pattern — guarantees the event is
        # never lost even if RabbitMQ is temporarily unavailable.
        journey.status = final_status.value
        journey.rejection_reason = rejection_reason
        await BookingSaga.save_outbox_event(db, journey, event_type, user_name=user_name)
        await db.commit()
        await db.refresh(journey)

        logger.info(f"Journey {journey_id} final status: {final_status.value}")

        # Award points for confirmed bookings (immediate access)
        if final_status == JourneyStatus.CONFIRMED:
            try:
                from .points import PointsService, POINTS_PER_BOOKING
                await PointsService.earn_points(
                    db, user_id, POINTS_PER_BOOKING,
                    "BOOKING_CONFIRMED", journey_id
                )
            except Exception as e:
                logger.warning(f"Failed to award booking points: {e}")

        # Replicate to peers (fire-and-forget)
        import asyncio
        from .replication import replicate_journey
        asyncio.create_task(replicate_journey(JourneyService._to_dict(journey)))

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

        # Update status and write outbox event in the same transaction
        journey.status = JourneyStatus.CANCELLED.value
        await BookingSaga.save_outbox_event(db, journey, EventType.JOURNEY_CANCELLED)
        await db.commit()
        await db.refresh(journey)

        # Deduct points for cancellation
        try:
            from .points import PointsService, POINTS_DEDUCTED_LATE_CANCEL
            await PointsService.spend_points(
                db, user_id, POINTS_DEDUCTED_LATE_CANCEL,
                "LATE_CANCELLATION", journey_id
            )
        except Exception as e:
            logger.warning(f"Could not deduct cancellation points: {e}")

        logger.info(f"Journey {journey_id} cancelled by user {user_id}")

        # Replicate cancellation to peers
        import asyncio
        from .replication import replicate_journey
        asyncio.create_task(replicate_journey(JourneyService._to_dict(journey)))

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
    # Helpers
    # ==========================================

    @staticmethod
    async def _verify_vehicle_ownership(user_id: str, vehicle_registration: str):
        """Verify that the vehicle registration belongs to the user via user-service."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{USER_SERVICE_URL}/api/users/vehicles/verify/{vehicle_registration}",
                    params={"user_id": user_id},
                )
                if response.status_code == 200:
                    data = response.json()
                    if not data.get("is_owner"):
                        raise ValueError(
                            f"Vehicle {vehicle_registration.upper()} is not registered to your account. "
                            f"Please register it first in 'My Vehicles'."
                        )
                else:
                    logger.warning(f"Vehicle verification returned {response.status_code}")
                    raise ValueError("Could not verify vehicle ownership. Please try again.")
        except httpx.ConnectError:
            logger.error("Cannot connect to user-service for vehicle verification")
            raise ValueError("Vehicle verification service unavailable. Please try again.")
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Vehicle verification error: {e}")
            raise ValueError("Vehicle verification failed. Please try again.")

    @staticmethod
    def _to_dict(journey: Journey) -> dict:
        """Serialize a Journey to a plain dict for cross-peer replication."""
        return {
            "id": journey.id,
            "user_id": journey.user_id,
            "origin": journey.origin,
            "destination": journey.destination,
            "origin_lat": journey.origin_lat,
            "origin_lng": journey.origin_lng,
            "destination_lat": journey.destination_lat,
            "destination_lng": journey.destination_lng,
            "departure_time": journey.departure_time.isoformat() if journey.departure_time else None,
            "estimated_duration_minutes": journey.estimated_duration_minutes,
            "estimated_arrival_time": journey.estimated_arrival_time.isoformat() if journey.estimated_arrival_time else None,
            "vehicle_registration": journey.vehicle_registration,
            "vehicle_type": journey.vehicle_type,
            "status": journey.status,
            "rejection_reason": journey.rejection_reason,
            "route_id": journey.route_id,
            "idempotency_key": journey.idempotency_key,
        }

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
