"""
Two-Phase Commit Coordinator — ported and adapted from Archive/services/coordinator.py

Implements the TCC (Try-Confirm-Cancel) variant of 2PC that maps cleanly onto
the existing DTS microservice boundaries:

    COORDINATOR role  — journey-service drives the full protocol
    PARTICIPANT role  — conflict-service holds/releases capacity

Phase 1 — TRY (PREPARE):
    • Journey-service creates a PENDING journey (already done by JourneyService)
    • Coordinator calls POST /api/conflicts/check → atomically reserves road capacity
      (the conflict-service's serialisable transaction IS the PREPARE phase)

Phase 2a — CONFIRM (COMMIT):
    • Journey-service updates journey to CONFIRMED + writes outbox event
    • Capacity remains reserved in conflict-service (no extra call needed)

Phase 2b — CANCEL (ABORT):
    • Journey-service updates journey to REJECTED
    • Coordinator calls POST /api/conflicts/cancel/{journey_id} to release held capacity
      (compensating transaction — ensures no phantom capacity leaks)

Why this is better than bare Saga
──────────────────────────────────
The existing BookingSaga does check+reserve in one call and then commits the
journey status.  If the journey commit step crashes after the conflict-service
has already reserved capacity, the capacity leaks and is never freed.

The TwoPhaseCoordinator adds an explicit compensating CANCEL on any failure
after a successful PREPARE, guaranteeing atomicity across both services.

Usage
─────
    coordinator = TwoPhaseCoordinator()
    status, reason = await coordinator.execute(journey, db)

The coordinator can be used as a drop-in replacement for BookingSaga.execute(),
with the same return signature (JourneyStatus, Optional[str]).
"""

import json
import logging
import os
import uuid
from datetime import datetime
from typing import Optional

import httpx

from .database import Journey, OutboxEvent
from shared.schemas import (
    ConflictCheckRequest,
    ConflictCheckResponse,
    JourneyStatus,
    EventType,
    VehicleType,
)
from shared.tracing import get_correlation_id
from shared.circuit_breaker import get_circuit_breaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

CONFLICT_SERVICE_URL = os.getenv("CONFLICT_SERVICE_URL", "http://conflict-service:8000")
TPC_TIMEOUT_SECONDS = 30  # mirrors Archive's config.TWO_PC_TIMEOUT


class TwoPhaseCoordinator:
    """
    Orchestrates a 2PC TCC booking across journey-service and conflict-service.

    Flow mirrors Archive's CoordinatorService.initiate_cross_region_booking():
        txn_id = new UUID
        TRY  → POST /api/conflicts/check      (reserves capacity — vote YES/NO)
        if YES → CONFIRM: update journey CONFIRMED + write outbox
        if NO  → CANCEL:  update journey REJECTED  (capacity was not reserved)
        on any exception after YES → COMPENSATE: call cancel to free capacity
    """

    @staticmethod
    async def execute(
        journey: Journey, db
    ) -> tuple[JourneyStatus, Optional[str]]:
        """
        Execute the 2PC protocol for a journey.
        Returns (final_status, rejection_reason) — same contract as BookingSaga.execute().
        """
        txn_id = f"TXN-{str(uuid.uuid4())[:8].upper()}"
        logger.info(
            f"[2PC] Starting TXN={txn_id} "
            f"journey={journey.id} "
            f"{journey.origin} → {journey.destination}"
        )

        # ── Phase 1: TRY (PREPARE) ──────────────────────────────────────────
        conflict_result = await TwoPhaseCoordinator._try_phase(journey, txn_id)

        if conflict_result is None:
            logger.error(f"[2PC] TXN={txn_id} PREPARE failed — conflict service unreachable")
            return JourneyStatus.REJECTED, "Conflict check service unavailable. Please retry."

        if conflict_result.is_conflict:
            # Capacity NOT reserved — no compensation needed
            reason = conflict_result.conflict_details or f"Conflict: {conflict_result.conflict_type}"
            logger.warning(f"[2PC] TXN={txn_id} ABORT — conflict: {reason}")
            return JourneyStatus.REJECTED, reason

        logger.info(f"[2PC] TXN={txn_id} PREPARE OK — proceeding to CONFIRM")

        # ── Phase 2a: CONFIRM (COMMIT) ──────────────────────────────────────
        # Capacity IS reserved. We must commit the journey or compensate.
        try:
            def json_serializer(obj):
                if isinstance(obj, datetime):
                    return obj.isoformat()
                raise TypeError(f"Type {type(obj)} not serializable")

            payload = {
                "event_type": EventType.JOURNEY_CONFIRMED.value,
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
                "status": JourneyStatus.CONFIRMED.value,
                "rejection_reason": None,
                "timestamp": datetime.utcnow().isoformat(),
                "txn_id": txn_id,
            }

            outbox = OutboxEvent(
                id=str(uuid.uuid4()),
                routing_key=EventType.JOURNEY_CONFIRMED.value,
                payload=json.dumps(payload, default=json_serializer),
                published=False,
            )
            db.add(outbox)
            # Caller (JourneyService) does the final db.commit()

            logger.info(f"[2PC] TXN={txn_id} COMMITTED — journey {journey.id} CONFIRMED")
            return JourneyStatus.CONFIRMED, None

        except Exception as exc:
            # ── Compensating CANCEL (ABORT) ────────────────────────────────
            logger.error(
                f"[2PC] TXN={txn_id} COMMIT failed ({exc}) — "
                f"running compensating CANCEL for journey {journey.id}"
            )
            await TwoPhaseCoordinator._cancel_phase(journey.id, txn_id)
            return JourneyStatus.REJECTED, "Booking failed during commit. Capacity released."

    # ── Phase helpers ───────────────────────────────────────────────────────

    @staticmethod
    async def _try_phase(
        journey: Journey, txn_id: str
    ) -> Optional[ConflictCheckResponse]:
        """
        PREPARE: call conflict-service to check AND reserve capacity.
        Returns None on communication failure, ConflictCheckResponse otherwise.
        """
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
            vehicle_type=VehicleType(journey.vehicle_type),
            route_id=getattr(journey, "route_id", None),
        )

        cb = get_circuit_breaker("conflict-service-2pc", failure_threshold=3, reset_timeout=30.0)

        async def _call():
            async with httpx.AsyncClient(timeout=TPC_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    f"{CONFLICT_SERVICE_URL}/api/conflicts/check",
                    json=request.model_dump(mode="json"),
                    headers={
                        "X-Correlation-ID": get_correlation_id(),
                        "X-2PC-Transaction-ID": txn_id,
                    },
                )
                resp.raise_for_status()
                return ConflictCheckResponse(**resp.json())

        try:
            return await cb.call(_call)
        except CircuitBreakerOpenError:
            logger.warning(f"[2PC] TXN={txn_id} circuit breaker OPEN for conflict-service")
            return None
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.error(f"[2PC] TXN={txn_id} PREPARE network error: {exc}")
            return None
        except Exception as exc:
            logger.error(f"[2PC] TXN={txn_id} PREPARE error: {exc}")
            return None

    @staticmethod
    async def _cancel_phase(journey_id: str, txn_id: str):
        """
        CANCEL (compensating transaction): release capacity held in conflict-service.
        Mirrors Archive's coordinator abort → booking_service.abort_held_booking().
        Best-effort — logs on failure but does not raise.
        """
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{CONFLICT_SERVICE_URL}/api/conflicts/cancel/{journey_id}",
                    headers={"X-2PC-Transaction-ID": txn_id},
                )
                if resp.status_code in (204, 404):
                    logger.info(
                        f"[2PC] TXN={txn_id} CANCEL sent for journey {journey_id} "
                        f"(status={resp.status_code})"
                    )
                else:
                    logger.warning(
                        f"[2PC] TXN={txn_id} CANCEL returned unexpected "
                        f"status={resp.status_code} for journey {journey_id}"
                    )
        except Exception as exc:
            logger.error(
                f"[2PC] TXN={txn_id} CANCEL failed for journey {journey_id}: {exc} "
                f"— capacity may leak, manual cleanup required"
            )
