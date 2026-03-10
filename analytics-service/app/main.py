"""
Analytics & Monitoring Service - FastAPI application.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db
from .consumer import start_consumer, get_system_stats, get_event_history
from shared.config import setup_logging
from shared.schemas import HealthResponse
from shared.messaging import get_broker, close_broker

setup_logging("analytics-service")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Analytics & Monitoring Service starting up...")
    await init_db()
    logger.info("Database tables created/verified")

    try:
        broker = await get_broker()
        await start_consumer(broker)
        logger.info("Connected to RabbitMQ and started consumer")
    except Exception as e:
        logger.warning(f"Could not connect to RabbitMQ: {e}")

    yield

    logger.info("Analytics & Monitoring Service shutting down...")
    await close_broker()


app = FastAPI(
    title="Journey Booking - Analytics & Monitoring Service",
    description="System-wide analytics dashboard: event logging, real-time statistics, and historical data",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return HealthResponse(
        status="healthy",
        service="analytics-service",
        timestamp=datetime.utcnow(),
    )


@app.get("/api/analytics/stats")
async def system_statistics():
    """Get real-time system statistics (events today, confirmations, rejections, etc.)."""
    return await get_system_stats()


@app.get("/api/analytics/events")
async def event_history(
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Get event history with optional filtering."""
    events = await get_event_history(event_type, limit, offset)
    return {"events": events, "count": len(events)}


@app.get("/api/analytics/health/services")
async def service_health():
    """Check health of all services (aggregated health dashboard)."""
    import httpx
    import os

    # Support both Docker (hostname-based) and local (port-based) deployment
    base = os.getenv("SERVICES_BASE_URL", "")
    if base:
        # Docker: all services on port 8000 of their Docker hostname
        services = {
            "user-service": f"http://user-service:8000/health",
            "journey-service": f"http://journey-service:8000/health",
            "conflict-service": f"http://conflict-service:8000/health",
            "notification-service": f"http://notification-service:8000/health",
            "enforcement-service": f"http://enforcement-service:8000/health",
            "analytics-service": f"http://localhost:8000/health",
        }
    else:
        # Local: each service on its own port
        services = {
            "user-service": "http://localhost:8001/health",
            "journey-service": "http://localhost:8002/health",
            "conflict-service": "http://localhost:8003/health",
            "notification-service": "http://localhost:8004/health",
            "enforcement-service": "http://localhost:8005/health",
            "analytics-service": "http://localhost:8006/health",
        }

    results = {}
    async with httpx.AsyncClient(timeout=5) as client:
        for name, url in services.items():
            try:
                resp = await client.get(url)
                results[name] = {
                    "status": "healthy" if resp.status_code == 200 else "unhealthy",
                    "response_time_ms": resp.elapsed.total_seconds() * 1000,
                }
            except Exception as e:
                results[name] = {
                    "status": "unreachable",
                    "error": str(e),
                }

    all_healthy = all(r["status"] == "healthy" for r in results.values())
    return {
        "overall_status": "healthy" if all_healthy else "degraded",
        "services": results,
        "checked_at": datetime.utcnow().isoformat(),
    }
