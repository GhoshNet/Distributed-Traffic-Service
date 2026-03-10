"""
Conflict Detection Service - Core conflict detection logic.

Checks for two types of conflicts:
1. TIME_OVERLAP: Driver or vehicle already has a journey during the requested time window
2. ROAD_CAPACITY: Geographic region exceeds booking capacity during the time window
"""

import uuid
import logging
import math
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_, func

from .database import BookedSlot, RoadSegmentCapacity
from shared.schemas import (
    ConflictCheckRequest,
    ConflictCheckResponse,
    ConflictType,
    EventType,
)

logger = logging.getLogger(__name__)

# Grid resolution for road capacity checks (~1km grid cells)
GRID_RESOLUTION = 0.01  # ~1km in lat/lng
# Time slot duration for capacity tracking
CAPACITY_SLOT_MINUTES = 30
# Default max capacity per grid cell per time slot
DEFAULT_MAX_CAPACITY = 100
# Buffer time between journeys (minutes)
JOURNEY_BUFFER_MINUTES = 5


class ConflictDetectionService:
    """Detects scheduling conflicts for journey bookings."""

    @staticmethod
    async def check_conflicts(
        db: AsyncSession, request: ConflictCheckRequest
    ) -> ConflictCheckResponse:
        """
        Run all conflict checks for a journey booking request.
        Returns the first conflict found, or no conflict if all checks pass.
        """
        arrival_time = request.departure_time + timedelta(
            minutes=request.estimated_duration_minutes
        )

        # Buffer zone around departure/arrival for safety
        buffered_departure = request.departure_time - timedelta(minutes=JOURNEY_BUFFER_MINUTES)
        buffered_arrival = arrival_time + timedelta(minutes=JOURNEY_BUFFER_MINUTES)

        # Check 1: Time overlap for the same driver
        driver_conflict = await ConflictDetectionService._check_driver_overlap(
            db, request.user_id, buffered_departure, buffered_arrival, request.journey_id
        )
        if driver_conflict:
            return ConflictCheckResponse(
                journey_id=request.journey_id,
                is_conflict=True,
                conflict_type=ConflictType.TIME_OVERLAP,
                conflict_details=f"Driver already has a journey booked from "
                                 f"{driver_conflict.departure_time.isoformat()} to "
                                 f"{driver_conflict.arrival_time.isoformat()}",
                checked_at=datetime.utcnow(),
            )

        # Check 2: Time overlap for the same vehicle
        vehicle_conflict = await ConflictDetectionService._check_vehicle_overlap(
            db, request.vehicle_registration, buffered_departure, buffered_arrival,
            request.journey_id,
        )
        if vehicle_conflict:
            return ConflictCheckResponse(
                journey_id=request.journey_id,
                is_conflict=True,
                conflict_type=ConflictType.TIME_OVERLAP,
                conflict_details=f"Vehicle {request.vehicle_registration} already has a journey booked from "
                                 f"{vehicle_conflict.departure_time.isoformat()} to "
                                 f"{vehicle_conflict.arrival_time.isoformat()}",
                checked_at=datetime.utcnow(),
            )

        # Check 3: Road capacity at origin and destination
        capacity_conflict = await ConflictDetectionService._check_road_capacity(
            db, request, arrival_time,
        )
        if capacity_conflict:
            return ConflictCheckResponse(
                journey_id=request.journey_id,
                is_conflict=True,
                conflict_type=ConflictType.ROAD_CAPACITY,
                conflict_details=capacity_conflict,
                checked_at=datetime.utcnow(),
            )

        # No conflicts — record the booking slot for future checks
        await ConflictDetectionService._record_booking_slot(db, request, arrival_time)

        return ConflictCheckResponse(
            journey_id=request.journey_id,
            is_conflict=False,
            checked_at=datetime.utcnow(),
        )

    @staticmethod
    async def _check_driver_overlap(
        db: AsyncSession,
        user_id: str,
        departure: datetime,
        arrival: datetime,
        exclude_journey_id: str,
    ) -> BookedSlot | None:
        """Check if driver has any overlapping booked slots."""
        result = await db.execute(
            select(BookedSlot).where(
                and_(
                    BookedSlot.user_id == user_id,
                    BookedSlot.is_active == True,
                    BookedSlot.journey_id != exclude_journey_id,
                    # Overlap condition: existing.start < new.end AND existing.end > new.start
                    BookedSlot.departure_time < arrival,
                    BookedSlot.arrival_time > departure,
                )
            ).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _check_vehicle_overlap(
        db: AsyncSession,
        vehicle_registration: str,
        departure: datetime,
        arrival: datetime,
        exclude_journey_id: str,
    ) -> BookedSlot | None:
        """Check if vehicle has any overlapping booked slots."""
        result = await db.execute(
            select(BookedSlot).where(
                and_(
                    BookedSlot.vehicle_registration == vehicle_registration,
                    BookedSlot.is_active == True,
                    BookedSlot.journey_id != exclude_journey_id,
                    BookedSlot.departure_time < arrival,
                    BookedSlot.arrival_time > departure,
                )
            ).limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _check_road_capacity(
        db: AsyncSession,
        request: ConflictCheckRequest,
        arrival_time: datetime,
    ) -> str | None:
        """Check if road capacity is exceeded in the origin/destination grid cells."""
        # Check capacity at origin
        origin_grid_lat = round(request.origin_lat / GRID_RESOLUTION) * GRID_RESOLUTION
        origin_grid_lng = round(request.origin_lng / GRID_RESOLUTION) * GRID_RESOLUTION

        origin_result = await db.execute(
            select(RoadSegmentCapacity).where(
                and_(
                    RoadSegmentCapacity.grid_lat == origin_grid_lat,
                    RoadSegmentCapacity.grid_lng == origin_grid_lng,
                    RoadSegmentCapacity.time_slot_start <= request.departure_time,
                    RoadSegmentCapacity.time_slot_end > request.departure_time,
                    RoadSegmentCapacity.current_bookings >= RoadSegmentCapacity.max_capacity,
                )
            ).limit(1)
        )
        if origin_result.scalar_one_or_none():
            return f"Road capacity exceeded at origin area ({origin_grid_lat:.2f}, {origin_grid_lng:.2f})"

        # Check capacity at destination
        dest_grid_lat = round(request.destination_lat / GRID_RESOLUTION) * GRID_RESOLUTION
        dest_grid_lng = round(request.destination_lng / GRID_RESOLUTION) * GRID_RESOLUTION

        dest_result = await db.execute(
            select(RoadSegmentCapacity).where(
                and_(
                    RoadSegmentCapacity.grid_lat == dest_grid_lat,
                    RoadSegmentCapacity.grid_lng == dest_grid_lng,
                    RoadSegmentCapacity.time_slot_start <= arrival_time,
                    RoadSegmentCapacity.time_slot_end > arrival_time,
                    RoadSegmentCapacity.current_bookings >= RoadSegmentCapacity.max_capacity,
                )
            ).limit(1)
        )
        if dest_result.scalar_one_or_none():
            return f"Road capacity exceeded at destination area ({dest_grid_lat:.2f}, {dest_grid_lng:.2f})"

        return None

    @staticmethod
    async def _record_booking_slot(
        db: AsyncSession,
        request: ConflictCheckRequest,
        arrival_time: datetime,
    ):
        """Record a booking slot so future conflict checks can detect it."""
        slot = BookedSlot(
            id=str(uuid.uuid4()),
            journey_id=request.journey_id,
            user_id=request.user_id,
            vehicle_registration=request.vehicle_registration,
            departure_time=request.departure_time,
            arrival_time=arrival_time,
            origin_lat=request.origin_lat,
            origin_lng=request.origin_lng,
            destination_lat=request.destination_lat,
            destination_lng=request.destination_lng,
            is_active=True,
        )
        db.add(slot)

        # Update road capacity counters
        await ConflictDetectionService._increment_capacity(
            db, request.origin_lat, request.origin_lng, request.departure_time
        )
        await ConflictDetectionService._increment_capacity(
            db, request.destination_lat, request.destination_lng, arrival_time
        )

        await db.commit()

    @staticmethod
    async def _increment_capacity(
        db: AsyncSession, lat: float, lng: float, time: datetime
    ):
        """Increment the booking count for a grid cell at the given time."""
        grid_lat = round(lat / GRID_RESOLUTION) * GRID_RESOLUTION
        grid_lng = round(lng / GRID_RESOLUTION) * GRID_RESOLUTION

        # Find the time slot
        slot_start = time.replace(
            minute=(time.minute // CAPACITY_SLOT_MINUTES) * CAPACITY_SLOT_MINUTES,
            second=0, microsecond=0,
        )
        slot_end = slot_start + timedelta(minutes=CAPACITY_SLOT_MINUTES)

        # Upsert capacity record
        result = await db.execute(
            select(RoadSegmentCapacity).where(
                and_(
                    RoadSegmentCapacity.grid_lat == grid_lat,
                    RoadSegmentCapacity.grid_lng == grid_lng,
                    RoadSegmentCapacity.time_slot_start == slot_start,
                )
            )
        )
        capacity = result.scalar_one_or_none()

        if capacity:
            capacity.current_bookings += 1
        else:
            capacity = RoadSegmentCapacity(
                id=str(uuid.uuid4()),
                grid_lat=grid_lat,
                grid_lng=grid_lng,
                time_slot_start=slot_start,
                time_slot_end=slot_end,
                current_bookings=1,
                max_capacity=DEFAULT_MAX_CAPACITY,
            )
            db.add(capacity)

    @staticmethod
    async def cancel_booking_slot(db: AsyncSession, journey_id: str):
        """Deactivate a booking slot when a journey is cancelled."""
        result = await db.execute(
            select(BookedSlot).where(
                and_(BookedSlot.journey_id == journey_id, BookedSlot.is_active == True)
            )
        )
        slot = result.scalar_one_or_none()
        if slot:
            slot.is_active = False
            await db.commit()
            logger.info(f"Deactivated booking slot for journey {journey_id}")
