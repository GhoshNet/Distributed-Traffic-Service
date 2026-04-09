"""
Journey Service - FastAPI application entry point.
"""

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

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
from shared.discovery import UDPDiscovery

setup_logging("journey-service")
logger = logging.getLogger(__name__)

# Global partition manager instance
partition_mgr = PartitionManager("journey-service")

# Global peer health monitor (Archive-style ALIVE/SUSPECT/DEAD)
health_monitor = PeerHealthMonitor("journey-service")

# Node failure simulation flag — mirrors Archive's state.failure_simulated
# When True: /health returns 503 so peers detect this node as DEAD
_node_failed = False

# Global UDP discovery instance (set during lifespan startup)
udp_discovery = None

# Network delay simulation (ms); 0 = disabled
_network_delay_ms = 0


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

    # Start UDP peer discovery
    global udp_discovery
    region_name = os.getenv("REGION_NAME", "Dublin")
    api_host = os.getenv("API_HOST", "")
    api_port = int(os.getenv("API_PORT", "8000"))

    def on_peer_discovered(peer):
        # Register discovered peer with health monitor
        health_monitor.register(
            f"region-{peer.region_name}",
            peer.health_url,
        )
        logger.info(f"[Discovery] Auto-registered peer region '{peer.region_name}' with health monitor")

    udp_discovery = UDPDiscovery(
        region_name=region_name,
        api_host=api_host,
        api_port=api_port,
        on_peer_discovered=on_peer_discovered,
    )
    await udp_discovery.start()
    logger.info(f"UDP discovery started for region '{region_name}'")

    yield

    logger.info("Journey Service shutting down...")
    await udp_discovery.stop()
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

@app.middleware("http")
async def network_delay_middleware(request, call_next):
    if _network_delay_ms > 0:
        await asyncio.sleep(_network_delay_ms / 1000.0)
    return await call_next(request)


app.include_router(router)


@app.get("/api/region")
async def get_region_info():
    """Return this node's region info and connected peers."""
    peers = udp_discovery.get_peers() if udp_discovery else {}
    return {
        "region_name": os.getenv("REGION_NAME", "Dublin"),
        "journey_service_url": udp_discovery.journey_service_url if udp_discovery else "",
        "peers": {
            name: {
                "region_name": peer.region_name,
                "journey_service_url": peer.journey_service_url,
                "last_seen_s_ago": round(time.monotonic() - peer.last_seen, 1),
                "graph_summary": peer.graph_summary,
            }
            for name, peer in peers.items()
        },
        "graph_summary": {"type": "road_network", "region": os.getenv("REGION_NAME", "Dublin")},
    }


@app.post("/admin/simulate/delay")
async def set_network_delay(payload: dict):
    """
    Inject artificial network delay on incoming requests.
    Body: {"delay_ms": 200}  — set to 0 to disable.
    """
    global _network_delay_ms
    delay = int(payload.get("delay_ms", 0))
    _network_delay_ms = max(0, delay)
    logger.info(f"[SIMULATION] Network delay set to {_network_delay_ms}ms")
    return {
        "status": "ok",
        "delay_ms": _network_delay_ms,
        "message": f"All incoming requests will be delayed by {_network_delay_ms}ms" if _network_delay_ms else "Delay disabled",
    }


@app.get("/admin/simulate/delay")
async def get_network_delay():
    return {"delay_ms": _network_delay_ms}


@app.get("/health")
async def health_check():
    from fastapi import HTTPException
    if _node_failed:
        raise HTTPException(status_code=503, detail="Node is in simulated failure state")
    return HealthResponse(
        status="healthy",
        service="journey-service",
        timestamp=datetime.now(timezone.utc),
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


@app.post("/admin/simulate/fail")
async def simulate_node_fail():
    """
    Simulate a node crash — mirrors Archive's simulate_node_failure().
    Makes /health return 503 so peer health monitors on other nodes
    will transition this node through ALIVE → SUSPECT → DEAD.
    New booking requests are rejected while failed.
    """
    global _node_failed
    _node_failed = True
    logger.error("[SIMULATION] Node failure simulated — /health now returns 503")
    return {"status": "failed", "message": "Node is now simulating a crash. Peers will detect SUSPECT in ~30s, DEAD in ~60s."}


@app.post("/admin/simulate/recover")
async def simulate_node_recover():
    """
    Recover from simulated failure — mirrors Archive's simulate_node_recovery().
    Restores /health to 200 so peers transition back to ALIVE.
    """
    global _node_failed
    _node_failed = False
    logger.info("[SIMULATION] Node recovery — /health restored to 200")
    return {"status": "recovered", "message": "Node recovered. Peers will detect ALIVE on next heartbeat (~10s)."}


@app.get("/admin/simulate/status")
async def simulate_status():
    """Return current simulation state of this node."""
    peers = health_monitor.get_status()
    alive = sum(1 for p in peers["peers"].values() if p["status"] == "ALIVE")
    total = len(peers["peers"])
    return {
        "node_failed": _node_failed,
        "local_only_mode": peers["local_only_mode"],
        "alive_peers": alive,
        "total_peers": total,
        "peers": peers["peers"],
    }


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
    await enforcement_redis.close()
    return {"status": "success", "journeys_cached": count}
