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
from shared.schemas import HealthResponse
from shared.messaging import get_broker, close_broker
from shared.tracing import CorrelationIDMiddleware
from .scheduler import transition_journeys
from .outbox_publisher import run_outbox_publisher
from shared.partition import (
    PartitionManager, make_postgres_probe,
    make_rabbitmq_probe, make_http_probe,
)

setup_logging("journey-service")
logger = logging.getLogger(__name__)

# Global partition manager instance
partition_mgr = PartitionManager("journey-service")


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
    import os
    partition_mgr.register_dependency("postgres", make_postgres_probe(engine))
    partition_mgr.register_dependency("rabbitmq", make_rabbitmq_probe(get_broker))
    partition_mgr.register_dependency(
        "conflict-service",
        make_http_probe(os.getenv("CONFLICT_SERVICE_URL", "http://conflict-service:8000") + "/health"),
    )
    await partition_mgr.start()
    logger.info("Partition manager started")

    yield

    logger.info("Journey Service shutting down...")
    await partition_mgr.stop()
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
