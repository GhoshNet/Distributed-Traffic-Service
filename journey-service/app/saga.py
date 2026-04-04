"""
Journey Service - Booking Saga Orchestrator.

Implements the saga pattern for the booking flow:
1. Create PENDING journey
2. Request conflict check from Conflict Detection Service
3. On response: confirm or reject the journey
4. Publish events for downstream services

Includes timeout handling for when the Conflict Detection Service is unavailable.
"""

import os
import asyncio
import logging
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from .database import Journey, async_session
from shared.schemas import (
    ConflictCheckRequest,
    ConflictCheckResponse,
    JourneyStatus,
    EventType,
)
from shared.messaging import get_broker
from shared.tracing import get_correlation_id
from shared.circuit_breaker import get_circuit_breaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

CONFLICT_SERVICE_URL = os.getenv("CONFLICT_SERVICE_URL", "http://conflict-service:8000")
SAGA_TIMEOUT_SECONDS = 30


class BookingSaga:
    """
    Orchestrates the booking flow using the saga pattern.

    Flow:
    1. Journey created as PENDING (already in DB)
    2. Calls Conflict Detection Service synchronously
    3. If approved → CONFIRMED; if conflict → REJECTED
    4. Publishes event to RabbitMQ for Notification + Enforcement + Analytics
    5. On timeout/error → REJECTED with reason
    """

    @staticmethod
    async def execute(journey: Journey) -> tuple[JourneyStatus, Optional[str]]:
        """
        Execute the booking saga for a journey.
        Returns (final_status, rejection_reason).
        """
        try:
            # Step 1: Call Conflict Detection Service
            conflict_result = await BookingSaga._check_conflicts(journey)

            if conflict_result is None:
                # Service unavailable - reject with reason
                return JourneyStatus.REJECTED, "Conflict check service unavailable. Please retry."

            if conflict_result.is_conflict:
                # Conflict found - reject
                reason = conflict_result.conflict_details or f"Conflict: {conflict_result.conflict_type}"
                return JourneyStatus.REJECTED, reason

            # No conflict - confirm
            return JourneyStatus.CONFIRMED, None

        except CircuitBreakerOpenError as e:
            logger.warning(f"Saga aborted: {e}")
            return JourneyStatus.REJECTED, "Conflict check service temporarily unavailable. Please retry later."
        except asyncio.TimeoutError:
            logger.error(f"Saga timeout for journey {journey.id}")
            return JourneyStatus.REJECTED, "Booking timed out. Please retry."
        except Exception as e:
            logger.error(f"Saga error for journey {journey.id}: {e}", exc_info=True)
            return JourneyStatus.REJECTED, f"Internal error during booking. Please retry."

    @staticmethod
    async def _check_conflicts(journey: Journey) -> Optional[ConflictCheckResponse]:
        """Call the Conflict Detection Service."""
        request = ConflictCheckRequest(
            journey_id=journey.id,
            user_id=journey.user_id,
            origin_lat=journey.origin_lat,
            origin_lng=journey.origin_lng,
            destination_lat=journey.destination_lat,
            destination_lng=journey.destination_lng,
            departure_time=journey.departure_time,
            estimated_duration_minutes=journey.estimated_duration_minutes,
            vehicle_registration=journey.vehicle_registration,
            vehicle_type=journey.vehicle_type,
        )

        cb = get_circuit_breaker("conflict-service", failure_threshold=3, reset_timeout=30.0)

        async def _make_request():
            async with httpx.AsyncClient(timeout=SAGA_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{CONFLICT_SERVICE_URL}/api/conflicts/check",
                    json=request.model_dump(mode="json"),
                    headers={"X-Correlation-ID": get_correlation_id()}
                )
                response.raise_for_status()
                return ConflictCheckResponse(**response.json())

        try:
            return await cb.call(_make_request)
        except httpx.TimeoutException:
            logger.error("Conflict Detection Service timed out")
            return None
        except httpx.ConnectError:
            logger.error("Cannot connect to Conflict Detection Service")
            return None
        except Exception as e:
            logger.error(f"Error calling Conflict Detection Service: {e}")
            return None

    @staticmethod
    async def publish_journey_event(journey: Journey, event_type: EventType):
        """Publish a journey event to RabbitMQ."""
        try:
            broker = await get_broker()
            await broker.publish(
                routing_key=event_type.value,
                data={
                    "event_type": event_type.value,
                    "journey_id": journey.id,
                    "user_id": journey.user_id,
                    "origin": journey.origin,
                    "destination": journey.destination,
                    "origin_lat": journey.origin_lat,
                    "origin_lng": journey.origin_lng,
                    "destination_lat": journey.destination_lat,
                    "destination_lng": journey.destination_lng,
                    "departure_time": journey.departure_time.isoformat(),
                    "estimated_arrival_time": journey.estimated_arrival_time.isoformat(),
                    "vehicle_registration": journey.vehicle_registration,
                    "status": journey.status,
                    "rejection_reason": journey.rejection_reason,
                    "timestamp": datetime.utcnow().isoformat(),
                },
            )
            logger.info(f"Published {event_type.value} for journey {journey.id}")
        except Exception as e:
            # Don't fail the booking if event publishing fails
            # Events will be retried or can be replayed from DB
            logger.error(f"Failed to publish event {event_type.value}: {e}")
