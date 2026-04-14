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
import uuid
from datetime import datetime
from typing import Optional

from .database import Journey, OutboxEvent
from .conflict_client import resilient_conflict_check, resilient_conflict_cancel
from shared.schemas import (
    ConflictCheckRequest,
    ConflictCheckResponse,
    JourneyStatus,
    EventType,
    VehicleType,
)
from shared.tracing import get_correlation_id

logger = logging.getLogger(__name__)


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
        journey: Journey, db, user_name: str = ""
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
        conflict_result, prepare_url = await TwoPhaseCoordinator._try_phase(journey, txn_id)

        if conflict_result is None:
            logger.error(f"[2PC] TXN={txn_id} PREPARE failed — all conflict nodes unreachable")
            return JourneyStatus.REJECTED, "Conflict check service unavailable. Please retry."

        if conflict_result.is_conflict:
            # Capacity NOT reserved — no compensation needed
            reason = conflict_result.conflict_details or f"Conflict: {conflict_result.conflict_type}"
            logger.warning(f"[2PC] TXN={txn_id} ABORT — conflict: {reason}")
            return JourneyStatus.REJECTED, reason

        logger.info(f"[2PC] TXN={txn_id} PREPARE OK on {prepare_url} — proceeding to CONFIRM")

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
                "user_name": user_name,
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
            # Cancel on the same node that did PREPARE first, then try others
            await TwoPhaseCoordinator._cancel_phase(journey.id, txn_id, prepare_url)
            return JourneyStatus.REJECTED, "Booking failed during commit. Capacity released."

    # ── Phase helpers ───────────────────────────────────────────────────────

    @staticmethod
    async def _try_phase(
        journey: Journey, txn_id: str
    ) -> tuple[Optional[ConflictCheckResponse], Optional[str]]:
        """
        PREPARE: call conflict-service (with peer failover) to check AND reserve capacity.
        Returns (ConflictCheckResponse, used_url) on success, (None, None) on total failure.
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
        result, used_url = await resilient_conflict_check(
            request, extra_headers={"X-2PC-Transaction-ID": txn_id}
        )
        if result is None:
            logger.error(f"[2PC] TXN={txn_id} PREPARE failed — all conflict nodes unreachable")
        return result, used_url

    @staticmethod
    async def _cancel_phase(journey_id: str, txn_id: str, preferred_url: Optional[str] = None):
        """
        CANCEL (compensating transaction): release capacity held in conflict-service.
        Tries the node that handled PREPARE first, then falls back to all others.
        Best-effort — logs on failure but does not raise.
        """
        ok = await resilient_conflict_cancel(
            journey_id,
            preferred_url=preferred_url,
            extra_headers={"X-2PC-Transaction-ID": txn_id},
        )
        if not ok:
            logger.error(
                f"[2PC] TXN={txn_id} CANCEL failed on all nodes for journey={journey_id} "
                f"— capacity may leak, manual cleanup required"
            )
