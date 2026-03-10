"""
Enforcement Service - Verifies that drivers have valid journey bookings.

Uses a layered lookup strategy for fast verification:
1. Redis cache (sub-ms) — populated by Journey Service on booking confirmation
2. Journey Service API fallback — when cache misses
3. Updates cache on fallback hit for future lookups
"""

import json
import os
import logging
from datetime import datetime
from typing import Optional

import httpx
import redis.asyncio as redis_async

from shared.schemas import (
    VerificationResponse,
    JourneyStatus,
)

logger = logging.getLogger(__name__)

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/1")
JOURNEY_SERVICE_URL = os.getenv("JOURNEY_SERVICE_URL", "http://journey-service:8000")

redis_client = redis_async.from_url(REDIS_URL, decode_responses=True)


class EnforcementService:
    """Provides fast journey verification for enforcement agents."""

    @staticmethod
    async def verify_by_vehicle(vehicle_registration: str) -> VerificationResponse:
        """
        Verify if a vehicle has an active journey booking.
        Used at roadside checks by enforcement personnel.
        """
        now = datetime.utcnow()

        # Layer 1: Check Redis cache
        cached = await EnforcementService._check_cache(
            f"active_journey:vehicle:{vehicle_registration}"
        )
        if cached:
            departure = datetime.fromisoformat(cached["departure_time"])
            arrival = datetime.fromisoformat(cached["estimated_arrival_time"])

            # Check if journey is currently valid (with 30min buffer)
            from datetime import timedelta
            if departure <= now + timedelta(minutes=30) and arrival >= now:
                return VerificationResponse(
                    is_valid=True,
                    driver_id=cached.get("user_id"),
                    journey_id=cached.get("journey_id"),
                    journey_status=JourneyStatus.CONFIRMED,
                    origin=cached.get("origin"),
                    destination=cached.get("destination"),
                    departure_time=departure,
                    estimated_arrival_time=arrival,
                    checked_at=now,
                )

        # Layer 2: Fall back to Journey Service API
        journey_data = await EnforcementService._query_journey_service(
            vehicle_registration
        )
        if journey_data:
            return VerificationResponse(
                is_valid=True,
                driver_id=journey_data.get("user_id"),
                journey_id=journey_data.get("id"),
                journey_status=JourneyStatus(journey_data.get("status", "CONFIRMED")),
                origin=journey_data.get("origin"),
                destination=journey_data.get("destination"),
                departure_time=datetime.fromisoformat(journey_data["departure_time"]),
                estimated_arrival_time=datetime.fromisoformat(
                    journey_data["estimated_arrival_time"]
                ),
                checked_at=now,
            )

        # No valid journey found
        return VerificationResponse(
            is_valid=False,
            checked_at=now,
        )

    @staticmethod
    async def verify_by_license(license_number: str) -> VerificationResponse:
        """Verify by driver's license number (requires user lookup)."""
        from datetime import timedelta
        now = datetime.utcnow()

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # First look up the user by license number
                user_resp = await client.get(
                    f"http://user-service:8000/api/users/license/{license_number}"
                )
                if user_resp.status_code != 200:
                    return VerificationResponse(is_valid=False, checked_at=now)

                user_data = user_resp.json()
                user_id = user_data["id"]

                # Layer 1: Check Redis cache by user_id
                cached = await EnforcementService._check_cache(
                    f"active_journey:user:{user_id}"
                )
                if cached:
                    departure = datetime.fromisoformat(cached["departure_time"])
                    arrival = datetime.fromisoformat(cached["estimated_arrival_time"])
                    if departure <= now + timedelta(minutes=30) and arrival >= now:
                        return VerificationResponse(
                            is_valid=True,
                            driver_id=user_id,
                            journey_id=cached.get("journey_id"),
                            journey_status=JourneyStatus.CONFIRMED,
                            origin=cached.get("origin"),
                            destination=cached.get("destination"),
                            departure_time=departure,
                            estimated_arrival_time=arrival,
                            checked_at=now,
                        )

                # Layer 2: Fall back to Journey Service API
                journey_resp = await client.get(
                    f"{JOURNEY_SERVICE_URL}/api/journeys/user/{user_id}/active"
                )
                if journey_resp.status_code == 200:
                    journeys = journey_resp.json()
                    if journeys:
                        j = journeys[0]
                        return VerificationResponse(
                            is_valid=True,
                            driver_id=user_id,
                            journey_id=j.get("id"),
                            journey_status=JourneyStatus(j.get("status", "CONFIRMED")),
                            origin=j.get("origin"),
                            destination=j.get("destination"),
                            departure_time=datetime.fromisoformat(j["departure_time"]),
                            estimated_arrival_time=datetime.fromisoformat(
                                j["estimated_arrival_time"]
                            ),
                            checked_at=now,
                        )

        except Exception as e:
            logger.warning(f"License verification failed: {e}")

        return VerificationResponse(is_valid=False, checked_at=now)

    @staticmethod
    async def _check_cache(key: str) -> Optional[dict]:
        """Check Redis cache for an active journey."""
        try:
            data = await redis_client.get(key)
            if data:
                return json.loads(data)
        except Exception as e:
            logger.warning(f"Redis cache check failed: {e}")
        return None

    @staticmethod
    async def _query_journey_service(vehicle_registration: str) -> Optional[dict]:
        """Query the Journey Service for active journeys for a vehicle."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{JOURNEY_SERVICE_URL}/api/journeys/vehicle/{vehicle_registration}/active"
                )
                if response.status_code == 200:
                    journeys = response.json()
                    if journeys:
                        # Return the first active journey
                        return journeys[0]
        except Exception as e:
            logger.warning(f"Journey Service query failed: {e}")
        return None
