"""
Analytics & Monitoring Service - FastAPI application.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Query, Depends
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db
from .consumer import start_consumer, get_system_stats, get_event_history
from shared.config import setup_logging
from shared.schemas import HealthResponse
from shared.messaging import get_broker, close_broker, get_dlq_messages, replay_dlq
from shared.tracing import CorrelationIDMiddleware
from shared.auth import require_role
from shared.partition import PartitionManager, make_postgres_probe, make_rabbitmq_probe

setup_logging("analytics-service")
logger = logging.getLogger(__name__)

partition_mgr = PartitionManager("analytics-service")


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

    from .database import engine
    partition_mgr.register_dependency("postgres", make_postgres_probe(engine))
    partition_mgr.register_dependency("rabbitmq", make_rabbitmq_probe(get_broker))
    await partition_mgr.start()
    logger.info("Partition manager started")

    yield

    logger.info("Analytics & Monitoring Service shutting down...")
    await partition_mgr.stop()
    await close_broker()


app = FastAPI(
    title="Journey Booking - Analytics & Monitoring Service",
    description="System-wide analytics dashboard: event logging, real-time statistics, and historical data",
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


@app.get(
    "/api/analytics/dlq",
    dependencies=[Depends(require_role("ADMIN"))]
)
async def inspect_dlq(limit: int = Query(10, ge=1, le=100)):
    """Inspect dead-letter queue messages (Admin only)."""
    messages = await get_dlq_messages(limit=limit)
    return {"messages": messages, "count": len(messages)}


@app.post(
    "/api/analytics/dlq/replay",
    dependencies=[Depends(require_role("ADMIN"))]
)
async def replay_dead_letters():
    """Replay all dead-lettered messages back to the main exchange (Admin only)."""
    count = await replay_dlq()
    return {"status": "success", "replayed_count": count}


@app.get("/health/partitions")
async def partition_status():
    """Check partition status for all dependencies."""
    return partition_mgr.get_status()


@app.get("/api/analytics/events/verify")
async def verify_event_chain():
    """Verify the cryptographic audit chain of events."""
    import hmac
    import hashlib
    import os
    from .database import async_session, EventLog
    from sqlalchemy import select

    secret = os.getenv("AUDIT_HMAC_SECRET", os.getenv("JWT_SECRET", "secret")).encode()
    is_valid = True
    corrupted_event_id = None

    async with async_session() as db:
        query = select(EventLog).order_by(EventLog.created_at.asc())
        result = await db.execute(query)
        events = result.scalars().all()

        expected_prev_hash = "0" * 64
        for e in events:
            if e.prev_hash != expected_prev_hash:
                is_valid = False
                corrupted_event_id = e.id
                break
            
            payload = f"{e.id}|{e.event_type}|{e.prev_hash}|{e.metadata_json}".encode()
            computed_hash = hmac.new(secret, payload, hashlib.sha256).hexdigest()
            
            if computed_hash != e.event_hash:
                is_valid = False
                corrupted_event_id = e.id
                break
                
            expected_prev_hash = e.event_hash

    return {
        "chain_valid": is_valid,
        "total_events_checked": len(events),
        "corrupted_event_id": corrupted_event_id
    }


@app.post(
    "/api/analytics/recovery/verify",
    dependencies=[Depends(require_role("ADMIN"))]
)
async def verify_recovery():
    """
    Run post-recovery consistency verification.
    Checks the HMAC audit chain for gaps or corruption that may
    indicate data loss during a failure or partition.
    """
    import os
    from shared.recovery import verify_data_consistency
    from .database import async_session
    secret = os.getenv("AUDIT_HMAC_SECRET", os.getenv("JWT_SECRET", "secret")).encode()
    report = await verify_data_consistency(async_session, secret)
    return report


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
