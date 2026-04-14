"""
Journey Service — Internal endpoints for cross-node journey replication.
These routes are NOT exposed to end-users; they are called node-to-node only.
Accessible via the nginx gateway at /internal/journeys/.
"""

import logging
import os
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .database import Journey, get_db
from .replication import add_peer, get_peers, sync_from_peer

logger = logging.getLogger(__name__)
internal_router = APIRouter(prefix="/internal/journeys", tags=["Internal — Journey Replication"])

_node_addr = os.environ.get("HOSTNAME", "journey-service")


def _parse_dt(v: Optional[str]) -> Optional[datetime]:
    if v is None:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


# ── Replication receive endpoint ───────────────────────────────────────────────

@internal_router.post("/replicate")
async def receive_journey(payload: dict, db: AsyncSession = Depends(get_db)):
    """
    Receive a replicated journey from a peer.
    Idempotent: inserts new, updates status on existing.
    """
    journey_id = payload.get("id")
    if not journey_id:
        return {"applied": False, "reason": "id required"}

    existing = (await db.execute(
        select(Journey).where(Journey.id == journey_id)
    )).scalar_one_or_none()

    if existing is None:
        try:
            db.add(Journey(
                id=journey_id,
                user_id=payload["user_id"],
                origin=payload["origin"],
                destination=payload["destination"],
                origin_lat=payload["origin_lat"],
                origin_lng=payload["origin_lng"],
                destination_lat=payload["destination_lat"],
                destination_lng=payload["destination_lng"],
                departure_time=_parse_dt(payload.get("departure_time")),
                estimated_duration_minutes=payload["estimated_duration_minutes"],
                estimated_arrival_time=_parse_dt(payload.get("estimated_arrival_time")),
                vehicle_registration=payload["vehicle_registration"],
                vehicle_type=payload.get("vehicle_type", "CAR"),
                status=payload["status"],
                rejection_reason=payload.get("rejection_reason"),
                route_id=payload.get("route_id"),
                idempotency_key=payload.get("idempotency_key"),
            ))
            await db.commit()
            logger.info(
                f"[journey-replication] RECV inserted journey={journey_id} "
                f"user={payload.get('user_id')} status={payload.get('status')}"
            )
            return {"applied": True, "action": "inserted"}
        except Exception as exc:
            await db.rollback()
            logger.error(f"[journey-replication] RECV insert failed for {journey_id}: {exc}")
            return {"applied": False, "reason": str(exc)}
    else:
        # Update status if it changed (e.g. CANCELLED propagation)
        incoming_status = payload.get("status", existing.status)
        if existing.status != incoming_status:
            existing.status = incoming_status
            existing.rejection_reason = payload.get("rejection_reason")
            await db.commit()
            logger.info(
                f"[journey-replication] RECV updated journey={journey_id} "
                f"status {existing.status} → {incoming_status}"
            )
            return {"applied": True, "action": "updated"}
        logger.debug(f"[journey-replication] RECV journey={journey_id} — already up to date, skip")
        return {"applied": False, "reason": "already_exists"}


# ── State snapshot for catch-up sync ──────────────────────────────────────────

@internal_router.get("/all")
async def get_all_journeys(db: AsyncSession = Depends(get_db)):
    """
    Return all journeys from this node.
    Peers call this on startup (or after rejoin) to pull full state.
    """
    journeys = (await db.execute(select(Journey))).scalars().all()

    return {
        "node": _node_addr,
        "journeys": [
            {
                "id": j.id,
                "user_id": j.user_id,
                "origin": j.origin,
                "destination": j.destination,
                "origin_lat": j.origin_lat,
                "origin_lng": j.origin_lng,
                "destination_lat": j.destination_lat,
                "destination_lng": j.destination_lng,
                "departure_time": j.departure_time.isoformat() if j.departure_time else None,
                "estimated_duration_minutes": j.estimated_duration_minutes,
                "estimated_arrival_time": j.estimated_arrival_time.isoformat() if j.estimated_arrival_time else None,
                "vehicle_registration": j.vehicle_registration,
                "vehicle_type": j.vehicle_type,
                "status": j.status,
                "rejection_reason": j.rejection_reason,
                "route_id": j.route_id,
                "idempotency_key": j.idempotency_key,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in journeys
        ],
        "journey_count": len(journeys),
    }


# ── Runtime peer registration ──────────────────────────────────────────────────

@internal_router.post("/peers/register")
async def register_peer(payload: dict):
    """Register a peer journey-service URL at runtime and trigger immediate catch-up sync."""
    import asyncio
    peer_url = (payload.get("peer_url") or "").rstrip("/")
    if not peer_url:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="peer_url required")
    is_new = add_peer(peer_url)
    from .database import async_session
    asyncio.create_task(sync_from_peer(peer_url, async_session))
    return {
        "registered": peer_url,
        "peers": get_peers(),
        "note": "Catch-up sync started in background",
        "is_new": is_new,
    }
