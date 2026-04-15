"""
Journey Service — Cross-node replication.

DS concepts demonstrated:
  - Active-active replication  : every confirmed/cancelled journey is pushed to all peers
  - Catch-up state sync        : late-joining nodes pull full journey state from peers on startup
  - Periodic re-sync (5 min)   : fills gaps from missed pushes while a node was down
  - Idempotent apply           : keyed on journey.id; updates status if record already exists
"""

import asyncio
import logging
import os
from typing import List

import httpx

from shared.circuit_breaker import get_circuit_breaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)


def _peer_cb(peer_url: str):
    """Get-or-create a circuit breaker for a specific peer URL."""
    return get_circuit_breaker(f"journey-peer:{peer_url}", failure_threshold=3, reset_timeout=30.0)

# ── Peer management ────────────────────────────────────────────────────────────

_peers: List[str] = []


def load_peers() -> List[str]:
    global _peers
    raw = os.environ.get("PEER_JOURNEY_URLS", "")
    _peers = [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]
    if _peers:
        logger.info(f"[journey-replication] peers configured: {_peers}")
    else:
        logger.info("[journey-replication] no peers configured — running standalone")
    return _peers


def get_peers() -> List[str]:
    return list(_peers)


def add_peer(peer_url: str) -> bool:
    """Add a peer at runtime. Returns True if newly added."""
    url = peer_url.rstrip("/")
    if url not in _peers:
        _peers.append(url)
        logger.info(f"[journey-replication] peer added at runtime: {url}")
        return True
    return False


# ── Forward replication (push on write) ───────────────────────────────────────

async def replicate_journey(journey_data: dict):
    """Push a journey record to all peers (fire-and-forget)."""
    peers = get_peers()
    if not peers:
        return
    async with httpx.AsyncClient(timeout=5.0) as client:
        for peer in peers:
            cb = _peer_cb(peer)
            try:
                r = await cb.call(
                    client.post,
                    f"{peer}/internal/journeys/replicate",
                    json=journey_data,
                )
                logger.info(
                    f"[journey-replication] PUSH journey={journey_data.get('id')} "
                    f"status={journey_data.get('status')} → {peer} (HTTP {r.status_code})"
                )
            except CircuitBreakerOpenError:
                logger.warning(f"[journey-replication] circuit OPEN for {peer} — skipping push")
            except Exception as exc:
                logger.warning(
                    f"[journey-replication] peer {peer} unreachable on push: {exc}"
                )


# ── Catch-up sync (pull from peer) ────────────────────────────────────────────

async def sync_from_peer(peer_url: str, db_session_factory):
    """
    Pull all journeys from peer_url and apply any missing/outdated locally.
    Fully idempotent — inserts new records, updates status on existing ones.
    """
    from .database import Journey
    from sqlalchemy import select

    try:
        cb = _peer_cb(peer_url)
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await cb.call(client.get, f"{peer_url}/internal/journeys/all")
            except CircuitBreakerOpenError:
                logger.warning(f"[journey-sync] circuit OPEN for {peer_url} — skipping catch-up sync")
                return
            if resp.status_code != 200:
                logger.warning(
                    f"[journey-sync] peer {peer_url} returned HTTP {resp.status_code}"
                )
                return
            data = resp.json()

        journeys = data.get("journeys", [])
        applied = updated = 0

        async with db_session_factory() as db:
            async with db.begin():
                for j in journeys:
                    existing = (await db.execute(
                        select(Journey).where(Journey.id == j["id"])
                    )).scalar_one_or_none()

                    if existing is None:
                        # New journey — insert
                        from datetime import datetime

                        def _parse(v):
                            if v is None:
                                return None
                            try:
                                return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
                            except Exception:
                                return None

                        db.add(Journey(
                            id=j["id"],
                            user_id=j["user_id"],
                            origin=j["origin"],
                            destination=j["destination"],
                            origin_lat=j["origin_lat"],
                            origin_lng=j["origin_lng"],
                            destination_lat=j["destination_lat"],
                            destination_lng=j["destination_lng"],
                            departure_time=_parse(j["departure_time"]),
                            estimated_duration_minutes=j["estimated_duration_minutes"],
                            estimated_arrival_time=_parse(j["estimated_arrival_time"]),
                            vehicle_registration=j["vehicle_registration"],
                            vehicle_type=j.get("vehicle_type", "CAR"),
                            status=j["status"],
                            rejection_reason=j.get("rejection_reason"),
                            route_id=j.get("route_id"),
                            idempotency_key=j.get("idempotency_key"),
                        ))
                        applied += 1
                    else:
                        # Existing journey — update status if it changed (e.g. CANCELLED)
                        if existing.status != j["status"]:
                            existing.status = j["status"]
                            existing.rejection_reason = j.get("rejection_reason")
                            updated += 1

        logger.info(
            f"[journey-sync] CATCH-UP from {peer_url}: "
            f"inserted {applied}, updated {updated} / {len(journeys)} total"
        )

    except Exception as exc:
        logger.warning(f"[journey-sync] catch-up from {peer_url} failed: {exc}")


def start_periodic_sync(interval_seconds: int, db_session_factory):
    """Background task: re-sync from all peers every interval_seconds."""
    async def _loop():
        await asyncio.sleep(interval_seconds)
        while True:
            for peer in get_peers():
                asyncio.create_task(sync_from_peer(peer, db_session_factory))
            await asyncio.sleep(interval_seconds)
    asyncio.create_task(_loop())
    logger.info(f"[journey-sync] periodic re-sync every {interval_seconds}s started")
