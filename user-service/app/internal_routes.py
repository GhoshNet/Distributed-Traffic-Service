"""
User Service — Internal endpoints for cross-node replication and distributed locking.
These routes are NOT exposed to end-users; they are called node-to-node only.
Accessible via the nginx gateway at /internal/users/ and /internal/vehicles/.
"""

import asyncio
import logging
import os
import time

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from .database import User, Vehicle, get_db
from .replication import LOCK_TTL, get_peers, add_peer, sync_from_peer

_LOCK_DB = 3  # same as replication.py — Redis DB 3 for user locks

logger = logging.getLogger(__name__)
internal_router = APIRouter(prefix="/internal", tags=["Internal — Node Replication"])

_node_addr = os.environ.get("HOSTNAME", "user-service")


async def _lock_redis() -> aioredis.Redis:
    base = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    url = base.rsplit("/", 1)[0] + f"/{_LOCK_DB}"
    return aioredis.from_url(url, decode_responses=True)


# ── Distributed lock endpoints ─────────────────────────────────────────────────

@internal_router.post("/users/lock")
async def acquire_lock(payload: dict, db: AsyncSession = Depends(get_db)):
    """
    Called by a peer before it registers a user.
    Checks email uniqueness locally AND acquires a Redis lock.
    Returns {"acquired": true} only if both succeed.
    """
    email = payload.get("email", "").lower()
    ttl = int(payload.get("ttl", LOCK_TTL))
    if not email:
        raise HTTPException(status_code=400, detail="email required")

    # Guard: if email already exists locally, reject immediately
    existing = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if existing:
        logger.info(f"[dist-lock] lock REJECTED for {email} — email already exists on {_node_addr}")
        return {"acquired": False, "reason": "email_exists"}

    lock_key = f"user_email_lock:{email}"
    r = await _lock_redis()
    try:
        result = await r.set(lock_key, f"peer:{time.time()}", nx=True, ex=ttl)
        acquired = result is not None
        if acquired:
            logger.info(f"[dist-lock] lock ACQUIRED on behalf of peer for {email}")
        else:
            logger.warning(f"[dist-lock] lock CONTENTION for {email} on {_node_addr}")
        return {"acquired": acquired, "reason": None if acquired else "contention"}
    finally:
        await r.aclose()


@internal_router.post("/users/unlock")
async def release_lock(payload: dict):
    """Release the distributed email lock — called after commit or on rollback."""
    email = payload.get("email", "").lower()
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    lock_key = f"user_email_lock:{email}"
    r = await _lock_redis()
    try:
        await r.delete(lock_key)
        logger.info(f"[dist-lock] lock RELEASED (peer request) for {email} on {_node_addr}")
        return {"released": True}
    finally:
        await r.aclose()


# ── Replication receive endpoints ──────────────────────────────────────────────

@internal_router.post("/users/replicate")
async def receive_user(payload: dict, db: AsyncSession = Depends(get_db)):
    """
    Receive a replicated user from a peer.
    Idempotent: no-op if user already exists by id or email.
    """
    user_id = payload.get("id")
    email = (payload.get("email") or "").lower()
    if not user_id or not email:
        raise HTTPException(status_code=400, detail="id and email required")

    if (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none():
        logger.debug(f"[user-replication] RECV user={user_id} — already present, skip")
        return {"applied": False, "reason": "already_exists"}

    if (await db.execute(select(User).where(User.email == email))).scalar_one_or_none():
        logger.warning(f"[user-replication] RECV user={user_id} — email {email} conflict, skip")
        return {"applied": False, "reason": "email_conflict"}

    try:
        db.add(User(
            id=user_id,
            email=email,
            password_hash=payload["password_hash"],
            full_name=payload["full_name"],
            license_number=payload["license_number"],
            role=payload.get("role", "DRIVER"),
            is_active=payload.get("is_active", True),
        ))
        await db.commit()
        logger.info(
            f"[user-replication] RECV applied user={user_id} email={email} "
            f"shard={payload.get('_shard_id', '?')} home={payload.get('_home_node', '?')}"
        )
        return {"applied": True}
    except Exception as exc:
        await db.rollback()
        logger.error(f"[user-replication] RECV failed for user={user_id}: {exc}")
        return {"applied": False, "reason": str(exc)}


@internal_router.post("/vehicles/replicate")
async def receive_vehicle(payload: dict, db: AsyncSession = Depends(get_db)):
    """
    Receive a replicated vehicle from a peer.
    Idempotent: no-op if vehicle already exists by id or registration.
    """
    vehicle_id = payload.get("id")
    registration = (payload.get("registration") or "").upper()
    if not vehicle_id or not registration:
        raise HTTPException(status_code=400, detail="id and registration required")

    if (await db.execute(select(Vehicle).where(Vehicle.id == vehicle_id))).scalar_one_or_none():
        logger.debug(f"[user-replication] RECV vehicle={registration} — already present, skip")
        return {"applied": False, "reason": "already_exists"}

    if (await db.execute(select(Vehicle).where(Vehicle.registration == registration))).scalar_one_or_none():
        logger.warning(f"[user-replication] RECV vehicle={registration} — registration conflict, skip")
        return {"applied": False, "reason": "registration_conflict"}

    try:
        db.add(Vehicle(
            id=vehicle_id,
            user_id=payload["user_id"],
            registration=registration,
            vehicle_type=payload.get("vehicle_type", "CAR"),
        ))
        await db.commit()
        logger.info(
            f"[user-replication] RECV applied vehicle={registration} "
            f"user={payload.get('user_id')}"
        )
        return {"applied": True}
    except Exception as exc:
        await db.rollback()
        logger.error(f"[user-replication] RECV failed for vehicle={registration}: {exc}")
        return {"applied": False, "reason": str(exc)}


# ── State snapshot for catch-up sync ──────────────────────────────────────────

@internal_router.get("/users/all")
async def get_all_users_and_vehicles(db: AsyncSession = Depends(get_db)):
    """
    Return all users and vehicles from this node.
    Peers call this on startup (or after rejoin) to pull full state.
    Password hashes are included so peers can authenticate the same credentials.
    """
    users = (await db.execute(select(User))).scalars().all()
    vehicles = (await db.execute(select(Vehicle))).scalars().all()

    return {
        "node": _node_addr,
        "users": [
            {
                "id": u.id,
                "email": u.email,
                "password_hash": u.password_hash,
                "full_name": u.full_name,
                "license_number": u.license_number,
                "role": u.role,
                "is_active": u.is_active,
            }
            for u in users
        ],
        "vehicles": [
            {
                "id": v.id,
                "user_id": v.user_id,
                "registration": v.registration,
                "vehicle_type": v.vehicle_type,
            }
            for v in vehicles
        ],
        "user_count": len(users),
        "vehicle_count": len(vehicles),
    }


# ── Runtime peer registration ──────────────────────────────────────────────────

@internal_router.post("/peers/register")
async def register_peer(payload: dict):
    """Register a peer user-service URL at runtime and trigger immediate catch-up sync."""
    peer_url = (payload.get("peer_url") or "").rstrip("/")
    if not peer_url:
        raise HTTPException(status_code=400, detail="peer_url required")
    add_peer(peer_url)
    from .database import async_session
    asyncio.create_task(sync_from_peer(peer_url, async_session))
    return {"registered": peer_url, "peers": get_peers(), "note": "Catch-up sync started in background"}
