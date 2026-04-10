"""
User Service - FastAPI application entry point.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .database import init_db
from .routes import router
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
