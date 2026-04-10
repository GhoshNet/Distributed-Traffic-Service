"""
User Service - FastAPI application entry point.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .database import init_db, async_session
from .routes import router
from .internal_routes import internal_router
from .replication import load_peers, get_peers, sync_from_peer, start_periodic_sync
from shared.config import setup_logging
from shared.schemas import HealthResponse
from shared.messaging import get_broker, close_broker
from shared.tracing import CorrelationIDMiddleware

setup_logging("user-service")
logger = logging.getLogger(__name__)

# Node failure simulation flag — set via /admin/simulate/fail.
# When True all non-health endpoints return 503, making this whole node
# appear dead to clients (not just the journey-service).
_node_failed = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: startup and shutdown."""
    logger.info("User Service starting up...")
    await init_db()
    logger.info("Database tables created/verified")

    try:
        broker = await get_broker()
        logger.info("Connected to RabbitMQ")
    except Exception as e:
        logger.warning(f"Could not connect to RabbitMQ: {e}")

    # Load peer URLs from env and perform startup catch-up sync
    peers = load_peers()
    if peers:
        for peer in peers:
            async def _sync(p=peer):
                import asyncio
                await asyncio.sleep(5)  # wait for own DB to be fully ready
                await sync_from_peer(p, async_session)
            asyncio.create_task(_sync())
        # Periodic re-sync every 5 minutes (fills gaps from missed pushes)
        start_periodic_sync(300, async_session)
        logger.info(f"User replication started — peers: {peers}")

    yield

    logger.info("User Service shutting down...")
    await close_broker()


app = FastAPI(
    title="Journey Booking - User Service",
    description="Handles user registration, authentication, and profiles",
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


@app.middleware("http")
async def node_failure_middleware(request: Request, call_next):
    """Return 503 for all non-health endpoints when node failure is simulated."""
    safe_paths = {"/health", "/admin/simulate/recover", "/admin/simulate/fail"}
    if _node_failed and request.url.path not in safe_paths:
        return JSONResponse(
            status_code=503,
            content={"detail": "Node is in simulated failure state"},
        )
    return await call_next(request)


@app.get("/admin/logs")
async def get_logs(limit: int = 200):
    """Cross-node log aggregation — returns recent log entries from this node's ring buffer."""
    from shared.config import get_recent_logs
    return {
        "node": os.environ.get("HOSTNAME", "user-service"),
        "service": "user-service",
        "entries": get_recent_logs(limit),
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    if _node_failed:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="Node is in simulated failure state")
    return HealthResponse(
        status="healthy",
        service="user-service",
        timestamp=datetime.utcnow(),
    )


@app.post("/admin/simulate/fail")
async def simulate_fail():
    global _node_failed
    _node_failed = True
    logger.error("[SIMULATION] User-service failure simulated — all endpoints now return 503")
    return {"status": "failed", "service": "user-service"}


@app.post("/admin/simulate/recover")
async def simulate_recover():
    global _node_failed
    _node_failed = False
    logger.info("[SIMULATION] User-service recovered")
    return {"status": "recovered", "service": "user-service"}
