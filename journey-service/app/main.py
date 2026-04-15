"""
Journey Service - FastAPI application entry point.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db
from .routes import router
from .internal_routes import internal_router
from shared.config import setup_logging
import asyncio
import os
from shared.schemas import HealthResponse
from shared.messaging import get_broker, close_broker
from shared.tracing import CorrelationIDMiddleware
from .scheduler import transition_journeys
from .outbox_publisher import run_outbox_publisher
from shared.partition import (
    PartitionManager, make_postgres_probe,
    make_rabbitmq_probe, make_http_probe,
)
from shared.health_monitor import PeerHealthMonitor

setup_logging("journey-service")
logger = logging.getLogger(__name__)

# Global partition manager instance
partition_mgr = PartitionManager("journey-service")

# Internal microservice health monitor (conflict-service, user-service, etc.)
health_monitor = PeerHealthMonitor("journey-service")

# Laptop peer health monitor — tracks dynamically-registered remote nodes
# (other physical machines / teammates' laptops on the same network)
laptop_monitor = PeerHealthMonitor("journey-service-laptops")

# Node failure simulation flag — mirrors Archive's state.failure_simulated
# When True: /health returns 503 so peers detect this node as DEAD
_node_failed = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Journey Service starting up...")
    await init_db()
    logger.info("Database tables created/verified")

    # Load peer URLs and trigger catch-up sync from all peers
    from .replication import load_peers, sync_from_peer, start_periodic_sync
    from .database import async_session as _async_session
    peers = load_peers()
    for peer in peers:
        asyncio.create_task(sync_from_peer(peer, _async_session))
        logger.info(f"[journey-replication] catch-up sync scheduled from {peer}")
    start_periodic_sync(300, _async_session)  # re-sync every 5 minutes

    try:
        broker = await get_broker()
        logger.info("Connected to RabbitMQ")

        # Start the background task for journey lifecycle transitions
        asyncio.create_task(transition_journeys())
        logger.info("Journey lifecycle scheduler started")

        # Start the outbox publisher (transactional outbox pattern)
        asyncio.create_task(run_outbox_publisher())
        logger.info("Outbox publisher started")
    except Exception as e:
        logger.warning(f"Could not connect to RabbitMQ: {e}")

    # Register dependencies for partition detection
    from .database import engine
    partition_mgr.register_dependency("postgres", make_postgres_probe(engine))
    partition_mgr.register_dependency("rabbitmq", make_rabbitmq_probe(get_broker))
    conflict_url = os.getenv("CONFLICT_SERVICE_URL", "http://conflict-service:8000")
    partition_mgr.register_dependency(
        "conflict-service",
        make_http_probe(conflict_url + "/health"),
    )
    await partition_mgr.start()
    logger.info("Partition manager started")

    # Register internal microservices with the service health monitor
    health_monitor.register(
        "conflict-service", conflict_url + "/health"
    )
    health_monitor.register(
        "user-service",
        os.getenv("USER_SERVICE_URL", "http://user-service:8000") + "/health",
    )
    health_monitor.register(
        "notification-service",
        os.getenv("NOTIFICATION_SERVICE_URL", "http://notification-service:8000") + "/health",
    )
    health_monitor.register(
        "enforcement-service",
        os.getenv("ENFORCEMENT_SERVICE_URL", "http://enforcement-service:8000") + "/health",
    )
    health_monitor.register(
        "analytics-service",
        os.getenv("ANALYTICS_SERVICE_URL", "http://analytics-service:8000") + "/health",
    )
    await health_monitor.start()
    logger.info("Internal service health monitor started")

    # Start the laptop peer monitor (starts empty; peers added via /admin/peers/register)
    await laptop_monitor.start()
    logger.info("Laptop peer health monitor started")

    yield

    logger.info("Journey Service shutting down...")
    await partition_mgr.stop()
    await health_monitor.stop()
    await laptop_monitor.stop()
    await close_broker()


app = FastAPI(
    title="Journey Booking - Journey Service",
    description="Core booking service: create, list, cancel journeys with saga-based conflict resolution",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(CorrelationIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(internal_router)


@app.get("/health")
async def health_check():
    from fastapi import HTTPException
    if _node_failed:
        raise HTTPException(status_code=503, detail="Node is in simulated failure state")
    return HealthResponse(
        status="healthy",
        service="journey-service",
        timestamp=datetime.utcnow(),
    )


@app.get("/health/partitions")
async def partition_status():
    """Check partition status for all dependencies."""
    return partition_mgr.get_status()


@app.get("/health/nodes")
async def node_health():
    """
    Per-peer liveness status using Archive's ALIVE/SUSPECT/DEAD model.
    Returns two separate groups:
      - 'services': internal microservices (conflict-, user-, notification-, enforcement-, analytics-service)
      - 'laptop_peers': dynamically-registered remote laptops (other machines on the same LAN)
      - 'local_only_mode': True when too many internal services are unreachable
    """
    svc_status    = health_monitor.get_status()
    laptop_status = laptop_monitor.get_status()
    return {
        "monitor": svc_status["monitor"],
        "local_only_mode": svc_status["local_only_mode"],
        # Keep the legacy 'peers' key pointing at services so existing callers don't break
        "peers": svc_status["peers"],
        # New key: real remote-machine peers (laptops / nodes on the LAN)
        "laptop_peers": laptop_status["peers"],
    }


@app.post("/admin/simulate/fail")
async def simulate_node_fail():
    """
    Simulate a full node crash.
    - journey-service: /health → 503, all booking endpoints → 503
    - user-service: cascaded via internal call → login/register → 503
    Peer health monitors will transition this node ALIVE → SUSPECT → DEAD.
    """
    global _node_failed
    _node_failed = True
    logger.error("[SIMULATION] Node failure simulated — cascading to user-service")

    # Cascade to user-service on the same node so login also returns 503.
    import httpx
    user_svc = os.getenv("USER_SERVICE_URL", "http://user-service:8000")
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{user_svc}/admin/simulate/fail", timeout=2.0)
        logger.error("[SIMULATION] user-service failure cascaded")
    except Exception as e:
        logger.warning(f"[SIMULATION] Could not cascade to user-service: {e}")

    return {"status": "failed", "message": "Node crash simulated (journey + user services). Peers detect SUSPECT in ~30s, DEAD in ~60s."}


@app.post("/admin/simulate/recover")
async def simulate_node_recover():
    """
    Recover from simulated failure — restores all services on this node.
    """
    global _node_failed
    _node_failed = False
    logger.info("[SIMULATION] Node recovery — cascading to user-service")

    import httpx
    user_svc = os.getenv("USER_SERVICE_URL", "http://user-service:8000")
    try:
        async with httpx.AsyncClient() as client:
            await client.post(f"{user_svc}/admin/simulate/recover", timeout=2.0)
        logger.info("[SIMULATION] user-service recovery cascaded")
    except Exception as e:
        logger.warning(f"[SIMULATION] Could not cascade recovery to user-service: {e}")

    return {"status": "recovered", "message": "Node recovered. Peers will detect ALIVE on next heartbeat (~10s)."}


@app.get("/admin/simulate/status")
async def simulate_status():
    """Return current simulation state of this node."""
    svc_status    = health_monitor.get_status()
    laptop_status = laptop_monitor.get_status()
    # Count both internal services AND laptop peers for summary stats
    all_peers = {**svc_status["peers"], **laptop_status["peers"]}
    alive = sum(1 for p in all_peers.values() if p["status"] == "ALIVE")
    total = len(all_peers)
    return {
        "node_failed": _node_failed,
        "local_only_mode": svc_status["local_only_mode"],
        "alive_peers": alive,
        "total_peers": total,
        # Separate keys so the frontend can show laptop peers vs services
        "peers": laptop_status["peers"],
        "services": svc_status["peers"],
    }


@app.post("/admin/peers/register")
async def register_peer(payload: dict):
    """
    Dynamically register a remote LAPTOP/NODE to monitor.

    Useful when teammates run the stack on their own machines on the same
    LAN/hotspot. POST the peer's /health URL and it will appear in
    /health/nodes under 'laptop_peers' within one heartbeat cycle (~10 s).

    Body: {"name": "alice-laptop", "health_url": "http://192.168.1.42:8080/health"}
    """
    name = payload.get("name")
    health_url = payload.get("health_url")
    if not name or not health_url:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Both 'name' and 'health_url' are required")
    # Register into the LAPTOP monitor, NOT the internal-services monitor
    laptop_monitor.register(name, health_url)
    return {"registered": name, "health_url": health_url,
            "note": "Will appear in /health/nodes laptop_peers within 10 seconds"}


@app.delete("/admin/peers/{name}")
async def unregister_peer(name: str):
    """Remove a dynamically registered laptop peer from health monitoring."""
    if name not in laptop_monitor._peers:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Laptop peer '{name}' not found")
    del laptop_monitor._peers[name]
    return {"unregistered": name}


@app.post("/admin/2pc/demo")
async def run_2pc_demo():
    """
    Trigger a test 2PC booking to demonstrate the protocol.
    Shows PREPARE → COMMIT/ABORT flow in logs.
    Returns which coordinator path was taken.
    """
    return {
        "message": "2PC coordinator is active. Book a journey normally — "
                   "use ?mode=2pc query param to force 2PC path.",
        "endpoints": {
            "book_with_2pc": "POST /api/journeys/?mode=2pc",
            "node_health":   "GET  /health/nodes",
            "partitions":    "GET  /health/partitions",
        },
    }


@app.post("/admin/recovery/drain-outbox")
async def drain_outbox():
    """
    Force-drain all unpublished outbox events after recovery.
    Used after total failure to immediately restore eventual consistency.
    """
    from shared.recovery import drain_outbox_backlog
    from .database import async_session
    broker = await get_broker()
    count = await drain_outbox_backlog(async_session, broker)
    return {"status": "success", "events_drained": count}


@app.get("/admin/logs")
async def get_logs(limit: int = 200):
    """
    Return recent log entries from this node's ring buffer.
    Used by the frontend to aggregate logs from all nodes into a unified activity feed.
    Always returns 200 — if node is failed, logs are still readable for diagnostics.
    """
    from shared.config import get_recent_logs
    entries = get_recent_logs(limit)
    return {
        "node": os.environ.get("HOSTNAME", "journey-service"),
        "service": "journey-service",
        "entries": entries,
        "count": len(entries),
    }


@app.post("/admin/recovery/rebuild-enforcement-cache")
async def rebuild_cache():
    """
    Rebuild the enforcement Redis cache from the journeys database.
    Used after Redis data loss or enforcement service recovery.
    """
    import redis.asyncio as redis_async
    import os
    from shared.recovery import rebuild_enforcement_cache
    from .database import async_session
    enforcement_redis = redis_async.from_url(
        os.getenv("REDIS_URL", "redis://redis:6379/4").replace("/1", "/4"),
        decode_responses=True,
    )
    count = await rebuild_enforcement_cache(enforcement_redis, async_session)
    await enforcement_redis.aclose()
    return {"status": "success", "journeys_cached": count}
