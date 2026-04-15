"""
User Service — Cross-node replication, distributed locking, and consistent-hash sharding.

DS concepts demonstrated:
  - Active-active replication  : every write is pushed to all peers immediately
  - Distributed lock (Redlock-style) : Redis SETNX on local node + peer coordination
    prevents the same email being registered on two nodes simultaneously
  - Consistent-hash sharding   : email hash → deterministic home-node assignment;
    logged on every registration so the Activity Feed shows shard ownership
  - Catch-up state sync        : late-joining nodes pull full user+vehicle state from peers
  - Periodic re-sync (5 min)   : fills gaps from missed pushes while a node was down
"""

import asyncio
import hashlib
import logging
import os
import time
from typing import List, Optional, Tuple

import httpx
import redis.asyncio as aioredis
from shared.circuit_breaker import get_circuit_breaker, CircuitBreakerOpenError


def _peer_cb(peer_url: str):
    """Get-or-create a circuit breaker for a specific peer URL."""
    return get_circuit_breaker(f"peer:{peer_url}", failure_threshold=3, reset_timeout=30.0)

logger = logging.getLogger(__name__)

# ── Peer management ────────────────────────────────────────────────────────────

_peers: List[str] = []
_node_addr: str = os.environ.get("HOSTNAME", "user-service")
_my_url: str = os.environ.get("MY_USER_URL", "").rstrip("/")


def load_peers() -> List[str]:
    global _peers
    raw = os.environ.get("PEER_USER_URLS", "")
    _peers = [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]
    if _peers:
        logger.info(f"[user-replication] peers configured: {_peers}")
    else:
        logger.info("[user-replication] no peers configured — running standalone")
    return _peers


def get_peers() -> List[str]:
    return list(_peers)


def add_peer(peer_url: str) -> bool:
    """Add a peer. Returns True if newly added, False if already known."""
    url = peer_url.rstrip("/")
    if url == _my_url:
        return False  # never add self
    if url not in _peers:
        _peers.append(url)
        logger.info(f"[user-replication] peer added at runtime: {url}")
        return True
    return False


async def announce_self_to(peer_url: str):
    """Tell a peer about our own URL so they add us without manual config."""
    if not _my_url:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{peer_url}/internal/users/peers/register",
                json={"peer_url": _my_url},
            )
        logger.info(f"[user-replication] announced self ({_my_url}) to {peer_url}")
    except Exception as exc:
        logger.warning(f"[user-replication] could not announce self to {peer_url}: {exc}")


async def gossip_new_peer(new_peer_url: str):
    """Tell all existing peers about a newly joined peer, and vice versa."""
    existing = [p for p in get_peers() if p != new_peer_url]
    async with httpx.AsyncClient(timeout=3.0) as client:
        # Tell existing peers about the new one
        for peer in existing:
            try:
                await client.post(
                    f"{peer}/internal/users/peers/register",
                    json={"peer_url": new_peer_url},
                )
                logger.info(f"[gossip] told {peer} about new peer {new_peer_url}")
            except Exception as exc:
                logger.warning(f"[gossip] could not reach {peer}: {exc}")
        # Tell the new peer about all existing ones
        if _my_url:
            for peer in existing:
                try:
                    await client.post(
                        f"{new_peer_url}/internal/users/peers/register",
                        json={"peer_url": peer},
                    )
                    logger.info(f"[gossip] told {new_peer_url} about existing peer {peer}")
                except Exception as exc:
                    logger.warning(f"[gossip] could not reach {new_peer_url}: {exc}")


# ── Consistent-hash sharding ───────────────────────────────────────────────────

def shard_for_email(email: str) -> Tuple[int, str]:
    """
    Consistent-hash shard assignment for a user email.

    With N nodes (1 local + len(peers) remotes), maps email → shard_id in [0, N).
    shard_id=0 means this node is the authoritative (home) writer for this user.
    All nodes still replicate all data; sharding determines write ownership only.

    Returns (shard_id, home_node_label).
    """
    all_nodes = [_node_addr] + _peers
    n = max(len(all_nodes), 1)
    h = int(hashlib.md5(email.lower().encode()).hexdigest(), 16)
    shard_id = h % n
    home = all_nodes[shard_id] if shard_id < len(all_nodes) else _node_addr
    return shard_id, home


def is_home_shard(email: str) -> bool:
    """Returns True if THIS node is the home shard for the given email."""
    shard_id, _ = shard_for_email(email)
    return shard_id == 0


# ── Redis distributed lock ─────────────────────────────────────────────────────
# Uses Redis DB 3 (dedicated, away from app caches on DB 0/1/2/4).
#
# Protocol:
#   1. SETNX lock on local Redis with TTL
#   2. POST /internal/users/lock to each peer → peer checks email + acquires its own lock
#   3. If ANY peer rejects → release all acquired locks, return failure
#   4. Caller registers user locally, replicates async, then calls release_distributed_lock
#
# Availability-biased: unreachable peers are skipped (prefer availability over strict
# consistency for an academic demo — the catch-up sync reconciles on recovery).

LOCK_TTL = 15  # seconds
_LOCK_DB = 3


async def _lock_redis() -> aioredis.Redis:
    base = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    url = base.rsplit("/", 1)[0] + f"/{_LOCK_DB}"
    return aioredis.from_url(url, decode_responses=True)


async def acquire_distributed_lock(email: str) -> Tuple[bool, Optional[aioredis.Redis]]:
    """
    Acquire distributed lock for email registration.
    Returns (True, redis_conn) on success — caller MUST call release_distributed_lock().
    Returns (False, None) on failure (contention or email already exists on a peer).
    """
    lock_key = f"user_email_lock:{email.lower()}"
    peers = get_peers()
    shard_id, home = shard_for_email(email)

    logger.info(
        f"[dist-lock] acquiring lock for email={email} "
        f"shard={shard_id} home={home} peers={peers}"
    )

    r = await _lock_redis()
    acquired = await r.set(lock_key, f"{_node_addr}:{time.time()}", nx=True, ex=LOCK_TTL)
    if not acquired:
        logger.warning(f"[dist-lock] LOCAL lock BUSY for {email} — another registration in progress")
        await r.aclose()
        return False, None

    logger.info(f"[dist-lock] LOCAL lock ACQUIRED for {email}")

    # Phase 2: acquire lock on all reachable peers
    locked_peers: List[str] = []
    failed = False
    async with httpx.AsyncClient(timeout=3.0) as client:
        for peer in peers:
            cb = _peer_cb(peer)
            try:
                resp = await cb.call(
                    client.post,
                    f"{peer}/internal/users/lock",
                    json={"email": email, "ttl": LOCK_TTL},
                )
                data = resp.json()
                if data.get("acquired"):
                    locked_peers.append(peer)
                    logger.info(f"[dist-lock] peer {peer} ACQUIRED lock for {email}")
                else:
                    reason = data.get("reason", "unknown")
                    logger.warning(f"[dist-lock] peer {peer} REJECTED lock for {email}: {reason}")
                    failed = True
                    break
            except CircuitBreakerOpenError:
                logger.warning(f"[dist-lock] circuit OPEN for {peer} — skipping lock phase")
            except Exception as exc:
                logger.warning(f"[dist-lock] peer {peer} unreachable during lock phase: {exc} — proceeding without it")

    if failed:
        # Rollback: release local + all peer locks
        await r.delete(lock_key)
        async with httpx.AsyncClient(timeout=2.0) as client:
            for peer in locked_peers:
                cb = _peer_cb(peer)
                try:
                    await cb.call(client.post, f"{peer}/internal/users/unlock", json={"email": email})
                except CircuitBreakerOpenError:
                    logger.warning(f"[dist-lock] circuit OPEN for {peer} — skipping rollback unlock")
                except Exception:
                    pass
        await r.aclose()
        logger.warning(f"[dist-lock] distributed lock FAILED for {email} — rolled back")
        return False, None

    logger.info(
        f"[dist-lock] distributed lock ACQUIRED for {email} "
        f"(local + {len(locked_peers)} peer(s))"
    )
    return True, r


async def release_distributed_lock(email: str, r: aioredis.Redis):
    """Release distributed lock on local Redis and all peers."""
    lock_key = f"user_email_lock:{email.lower()}"
    await r.delete(lock_key)
    await r.aclose()
    logger.info(f"[dist-lock] local lock RELEASED for {email}")

    peers = get_peers()
    if not peers:
        return
    async with httpx.AsyncClient(timeout=2.0) as client:
        for peer in peers:
            cb = _peer_cb(peer)
            try:
                await cb.call(client.post, f"{peer}/internal/users/unlock", json={"email": email})
            except CircuitBreakerOpenError:
                logger.warning(f"[dist-lock] circuit OPEN for {peer} — skipping unlock")
            except Exception:
                pass


# ── Forward replication (push on write) ───────────────────────────────────────

async def replicate_user(user_data: dict):
    """Push a committed user record to all peers (fire-and-forget)."""
    peers = get_peers()
    if not peers:
        return
    shard_id, home = shard_for_email(user_data.get("email", ""))
    user_data["_shard_id"] = shard_id
    user_data["_home_node"] = home

    async with httpx.AsyncClient(timeout=4.0) as client:
        for peer in peers:
            cb = _peer_cb(peer)
            try:
                r = await cb.call(client.post, f"{peer}/internal/users/replicate", json=user_data)
                logger.info(
                    f"[user-replication] PUSH user={user_data.get('id')} "
                    f"email={user_data.get('email')} shard={shard_id} "
                    f"→ {peer} (HTTP {r.status_code})"
                )
            except CircuitBreakerOpenError:
                logger.warning(f"[user-replication] circuit OPEN for {peer} — skipping user push")
            except Exception as exc:
                logger.warning(f"[user-replication] peer {peer} unreachable on user push: {exc}")


async def replicate_vehicle(vehicle_data: dict):
    """Push a committed vehicle record to all peers (fire-and-forget)."""
    peers = get_peers()
    if not peers:
        return
    async with httpx.AsyncClient(timeout=4.0) as client:
        for peer in peers:
            cb = _peer_cb(peer)
            try:
                r = await cb.call(client.post, f"{peer}/internal/vehicles/replicate", json=vehicle_data)
                logger.info(
                    f"[user-replication] PUSH vehicle={vehicle_data.get('registration')} "
                    f"user={vehicle_data.get('user_id')} → {peer} (HTTP {r.status_code})"
                )
            except CircuitBreakerOpenError:
                logger.warning(f"[user-replication] circuit OPEN for {peer} — skipping vehicle push")
            except Exception as exc:
                logger.warning(f"[user-replication] peer {peer} unreachable on vehicle push: {exc}")


# ── Catch-up sync (pull from peer) ────────────────────────────────────────────

async def sync_from_peer(peer_url: str, db_session_factory):
    """
    Pull all users and vehicles from peer_url and apply any missing locally.
    Fully idempotent — skips records that already exist by id, email, or registration.
    """
    from .database import User, Vehicle
    from sqlalchemy import select

    try:
        cb = _peer_cb(peer_url)
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                resp = await cb.call(client.get, f"{peer_url}/internal/users/all")
            except CircuitBreakerOpenError:
                logger.warning(f"[user-sync] circuit OPEN for {peer_url} — skipping catch-up sync")
                return
            if resp.status_code != 200:
                logger.warning(f"[user-sync] peer {peer_url} returned HTTP {resp.status_code}")
                return
            data = resp.json()

        users = data.get("users", [])
        vehicles = data.get("vehicles", [])
        applied_u = applied_v = 0

        async with db_session_factory() as db:
            async with db.begin():
                for u in users:
                    exists = (await db.execute(
                        select(User).where(User.id == u["id"])
                    )).scalar_one_or_none()
                    if exists:
                        continue
                    email_exists = (await db.execute(
                        select(User).where(User.email == u["email"])
                    )).scalar_one_or_none()
                    if email_exists:
                        continue
                    db.add(User(
                        id=u["id"],
                        email=u["email"],
                        password_hash=u["password_hash"],
                        full_name=u["full_name"],
                        license_number=u["license_number"],
                        role=u.get("role", "DRIVER"),
                        is_active=u.get("is_active", True),
                    ))
                    applied_u += 1

                for v in vehicles:
                    exists = (await db.execute(
                        select(Vehicle).where(Vehicle.id == v["id"])
                    )).scalar_one_or_none()
                    if exists:
                        continue
                    reg_exists = (await db.execute(
                        select(Vehicle).where(Vehicle.registration == v["registration"])
                    )).scalar_one_or_none()
                    if reg_exists:
                        continue
                    db.add(Vehicle(
                        id=v["id"],
                        user_id=v["user_id"],
                        registration=v["registration"],
                        vehicle_type=v.get("vehicle_type", "CAR"),
                    ))
                    applied_v += 1

        logger.info(
            f"[user-sync] CATCH-UP from {peer_url}: "
            f"applied {applied_u}/{len(users)} users, "
            f"{applied_v}/{len(vehicles)} vehicles "
            f"(rest already present)"
        )

    except Exception as exc:
        logger.warning(f"[user-sync] catch-up from {peer_url} failed: {exc}")


def start_periodic_sync(interval_seconds: int, db_session_factory):
    """Background task: re-sync from all peers every interval_seconds."""
    async def _loop():
        await asyncio.sleep(interval_seconds)
        while True:
            for peer in get_peers():
                asyncio.create_task(sync_from_peer(peer, db_session_factory))
            await asyncio.sleep(interval_seconds)
    asyncio.create_task(_loop())
    logger.info(f"[user-sync] periodic re-sync every {interval_seconds}s started")
