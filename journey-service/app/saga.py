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

CONFLICT_SERVICE_URL = os.getenv("CONFLICT_SERVICE_URL", "http://conflict-service-ie:8000")
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

        Single-region journeys use the existing direct conflict-check flow.
        Multi-region journeys (e.g. Dublin→Belfast crossing IE and NI) run a
        2-phase distributed saga: Phase 1 holds road segments on all regions,
        Phase 2 commits all if all held, otherwise rolls back all.
        """
        try:
            from .registry import get_regions_for_route
            route_id = getattr(journey, "route_id", None)
            regions = get_regions_for_route(route_id)

            if len(regions) <= 1:
                # ── Single-region fast path (unchanged) ──────────────────────
                conflict_result = await BookingSaga._check_conflicts(journey)

                if conflict_result is None:
                    return JourneyStatus.REJECTED, "Conflict check service unavailable. Please retry."

                if conflict_result.is_conflict:
                    reason = conflict_result.conflict_details or f"Conflict: {conflict_result.conflict_type}"
                    return JourneyStatus.REJECTED, reason

                return JourneyStatus.CONFIRMED, None
            else:
                # ── Multi-region 2-phase saga ─────────────────────────────────
                logger.info(
                    "Journey %s crosses %d regions: %s",
                    journey.id,
                    len(regions),
                    [r[0] for r in regions],
                )
                return await BookingSaga._execute_multi_region(journey, regions)

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
    async def _execute_multi_region(
        journey: Journey,
        regions: list[tuple[str, str]],
    ) -> tuple[JourneyStatus, Optional[str]]:
        """
        Two-phase distributed saga across multiple regional conflict services.

        Phase 1: POST /api/conflicts/hold on every region — all must succeed.
        Phase 2: POST /api/conflicts/commit/{hold_id} on every region.

        If any Phase-1 hold fails (conflict or network error), roll back all
        previously acquired holds and return REJECTED.
        """
        holds: dict[str, tuple[str, str]] = {}  # region_id -> (hold_id, region_url)

        # ── Phase 1: Hold on all regions ─────────────────────────────────────
        for region_id, region_url in regions:
            hold_result = await BookingSaga._request_hold(journey, region_url)

            if hold_result is None:
                # Network / timeout failure
                logger.error(
                    "Phase-1 hold failed for region %s (network error) — rolling back",
                    region_id,
                )
                for held_region_id, (hold_id, held_url) in holds.items():
                    await BookingSaga._rollback_hold(hold_id, held_url)
                    logger.info("Rolled back hold %s on region %s", hold_id, held_region_id)
                return JourneyStatus.REJECTED, f"Region {region_id} unavailable — cross-border booking failed"

            if hold_result.get("is_conflict"):
                # Conflict detected on this region
                logger.info(
                    "Phase-1 conflict on region %s: %s",
                    region_id,
                    hold_result.get("conflict_details", ""),
                )
                for held_region_id, (hold_id, held_url) in holds.items():
                    await BookingSaga._rollback_hold(hold_id, held_url)
                    logger.info("Rolled back hold %s on region %s", hold_id, held_region_id)
                conflict_details = hold_result.get("conflict_details") or "Cross-region conflict detected"
                return JourneyStatus.REJECTED, conflict_details

            hold_id = hold_result.get("hold_id")
            if not hold_id:
                logger.error("Region %s returned hold response without hold_id", region_id)
                for held_region_id, (hid, held_url) in holds.items():
                    await BookingSaga._rollback_hold(hid, held_url)
                return JourneyStatus.REJECTED, f"Unexpected response from region {region_id}"

            holds[region_id] = (hold_id, region_url)
            logger.info("Phase-1 hold acquired: region=%s hold_id=%s", region_id, hold_id)

        # ── Phase 2: Commit all ───────────────────────────────────────────────
        for region_id, (hold_id, region_url) in holds.items():
            committed = await BookingSaga._commit_hold(hold_id, region_url)
            if not committed:
                # Commit failure — partial commit; log but continue
                # (already-committed holds can't be rolled back safely)
                logger.error(
                    "Phase-2 commit FAILED for region %s hold %s — partial commit state",
                    region_id, hold_id,
                )
            else:
                logger.info("Phase-2 committed: region=%s hold_id=%s", region_id, hold_id)

        return JourneyStatus.CONFIRMED, None

    @staticmethod
    async def _request_hold(journey: Journey, region_url: str) -> Optional[dict]:
        """
        POST /api/conflicts/hold to a regional conflict service.
        Returns the parsed JSON response dict, or None on failure.
        """
        route_id = getattr(journey, "route_id", None)
        payload = {
            "journey_id": journey.id,
            "user_id": journey.user_id,
            "origin_lat": journey.origin_lat,
            "origin_lng": journey.origin_lng,
            "destination_lat": journey.destination_lat,
            "destination_lng": journey.destination_lng,
            "departure_time": journey.departure_time.isoformat(),
            "estimated_duration_minutes": journey.estimated_duration_minutes,
            "vehicle_registration": journey.vehicle_registration,
        }
        if route_id:
            payload["route_id"] = route_id

        try:
            async with httpx.AsyncClient(timeout=SAGA_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    f"{region_url}/api/conflicts/hold",
                    json=payload,
                    headers={"X-Correlation-ID": get_correlation_id()},
                )
                # 200 = held, 409 = conflict — both are valid JSON responses
                if resp.status_code in (200, 409):
                    return resp.json()
                logger.error("Hold request to %s returned HTTP %d", region_url, resp.status_code)
                return None
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.error("Hold request to %s failed: %s", region_url, e)
            return None
        except Exception as e:
            logger.error("Unexpected error in hold request to %s: %s", region_url, e)
            return None

    @staticmethod
    async def _commit_hold(hold_id: str, region_url: str) -> bool:
        """POST /api/conflicts/commit/{hold_id}. Returns True on success."""
        try:
            async with httpx.AsyncClient(timeout=SAGA_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    f"{region_url}/api/conflicts/commit/{hold_id}",
                    headers={"X-Correlation-ID": get_correlation_id()},
                )
                if resp.status_code == 204:
                    return True
                logger.error(
                    "Commit hold %s at %s returned HTTP %d: %s",
                    hold_id, region_url, resp.status_code, resp.text,
                )
                return False
        except Exception as e:
            logger.error("Commit hold %s at %s failed: %s", hold_id, region_url, e)
            return False

    @staticmethod
    async def _rollback_hold(hold_id: str, region_url: str) -> bool:
        """POST /api/conflicts/rollback/{hold_id}. Returns True on success."""
        try:
            async with httpx.AsyncClient(timeout=SAGA_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    f"{region_url}/api/conflicts/rollback/{hold_id}",
                    headers={"X-Correlation-ID": get_correlation_id()},
                )
                if resp.status_code == 204:
                    return True
                logger.warning(
                    "Rollback hold %s at %s returned HTTP %d: %s",
                    hold_id, region_url, resp.status_code, resp.text,
                )
                return False
        except Exception as e:
            logger.error("Rollback hold %s at %s failed: %s", hold_id, region_url, e)
            return False

    @staticmethod
    async def _check_conflicts(journey: Journey) -> Optional[ConflictCheckResponse]:
        """
        Call the primary Conflict Detection Service (single-region fast path).
        Used when the route only crosses one region.
        """
        from .registry import get_regions_for_route
        route_id = getattr(journey, "route_id", None)
        regions = get_regions_for_route(route_id)
        # Use the first (and only) region URL for single-region check
        conflict_service_url = regions[0][1] if regions else CONFLICT_SERVICE_URL

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
            route_id=route_id,
        )

        cb = get_circuit_breaker("conflict-service", failure_threshold=3, reset_timeout=30.0)

        async def _make_request():
            async with httpx.AsyncClient(timeout=SAGA_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    f"{conflict_service_url}/api/conflicts/check",
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
    def build_event_payload(journey: Journey, event_type: EventType) -> dict:
        """Build the event payload dict for a journey event."""
        return {
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
        }

    @staticmethod
    async def save_outbox_event(db, journey: Journey, event_type: EventType):
        """
        Write an event to the outbox table within the current DB transaction.
        A background publisher will drain unpublished events to RabbitMQ,
        guaranteeing at-least-once delivery (transactional outbox pattern).
        """
        import json
        import uuid
        from .database import OutboxEvent

        payload = BookingSaga.build_event_payload(journey, event_type)

        def json_serializer(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            raise TypeError(f"Type {type(obj)} not serializable")

        outbox = OutboxEvent(
            id=str(uuid.uuid4()),
            routing_key=event_type.value,
            payload=json.dumps(payload, default=json_serializer),
            published=False,
        )
        db.add(outbox)

    @staticmethod
    async def publish_journey_event(journey: Journey, event_type: EventType):
        """Publish a journey event directly to RabbitMQ (used by scheduler)."""
        try:
            broker = await get_broker()
            payload = BookingSaga.build_event_payload(journey, event_type)
            await broker.publish(routing_key=event_type.value, data=payload)
            logger.info(f"Published {event_type.value} for journey {journey.id}")
        except Exception as e:
            logger.error(f"Failed to publish event {event_type.value}: {e}")
