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

# Global peer health monitor (Archive-style ALIVE/SUSPECT/DEAD)
health_monitor = PeerHealthMonitor("journey-service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Journey Service starting up...")
    await init_db()
    logger.info("Database tables created/verified")

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

    # Register peer services with health monitor (Archive ALIVE/SUSPECT/DEAD model)
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
    logger.info("Peer health monitor started")

    yield

    logger.info("Journey Service shutting down...")
    await partition_mgr.stop()
    await health_monitor.stop()
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


@app.get("/health", response_model=HealthResponse)
async def health_check():
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
    Surfaces the health monitor state for the frontend dashboard.
    """
    return health_monitor.get_status()


@app.post("/admin/peers/register")
async def register_peer(payload: dict):
    """
    Dynamically register a remote peer node to monitor.

    Useful when teammates run the stack on their own machines on the same
    LAN/hotspot. POST the peer's journey-service /health URL and it will
    appear in /health/nodes within one heartbeat cycle (~10 s).

    Body: {"name": "peer-alice", "health_url": "http://192.168.1.42:8080/health/nodes"}
    """
    name = payload.get("name")
    health_url = payload.get("health_url")
    if not name or not health_url:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Both 'name' and 'health_url' are required")
    health_monitor.register(name, health_url)
    return {"registered": name, "health_url": health_url,
            "note": "Will appear in /health/nodes within 10 seconds"}


@app.delete("/admin/peers/{name}")
async def unregister_peer(name: str):
    """Remove a dynamically registered peer from health monitoring."""
    if name not in health_monitor._peers:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Peer '{name}' not found")
    del health_monitor._peers[name]
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
