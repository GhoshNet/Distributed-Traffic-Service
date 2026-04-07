"""
Enforcement Service - FastAPI application.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware

from .service import EnforcementService
from shared.config import setup_logging
from shared.schemas import HealthResponse, VerificationResponse, ErrorResponse
from shared.messaging import get_broker, close_broker
from shared.tracing import CorrelationIDMiddleware
from shared.auth import require_role
from shared.partition import (
    PartitionManager, make_redis_probe,
    make_rabbitmq_probe, make_http_probe,
)

setup_logging("enforcement-service")
logger = logging.getLogger(__name__)

partition_mgr = PartitionManager("enforcement-service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Enforcement Service starting up...")

    try:
        broker = await get_broker()
        logger.info("Connected to RabbitMQ")

        # Start consuming journey events to populate local enforcement cache
        from .consumer import start_consumer
        await start_consumer(broker)
        logger.info("Enforcement event consumer started")
    except Exception as e:
        logger.warning(f"Could not connect to RabbitMQ: {e}")

    # Register dependencies for partition detection
    import os
    from .service import redis_client
    partition_mgr.register_dependency("redis", make_redis_probe(redis_client))
    partition_mgr.register_dependency("rabbitmq", make_rabbitmq_probe(get_broker))
    partition_mgr.register_dependency(
        "journey-service",
        make_http_probe(os.getenv("JOURNEY_SERVICE_URL", "http://journey-service:8000") + "/health"),
    )
    await partition_mgr.start()
    logger.info("Partition manager started")

    yield

    logger.info("Enforcement Service shutting down...")
    await partition_mgr.stop()
    await close_broker()


app = FastAPI(
    title="Journey Booking - Enforcement Service",
    description="Fast journey verification for roadside enforcement: Redis cache + DB fallback",
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
        service="enforcement-service",
        timestamp=datetime.utcnow(),
    )


@app.get(
    "/api/enforcement/verify/vehicle/{vehicle_registration}",
    response_model=VerificationResponse,
    dependencies=[Depends(require_role("ENFORCEMENT_AGENT"))],
)
async def verify_vehicle(vehicle_registration: str):
    """
    Verify if a vehicle has a valid active journey booking.
    Optimized for speed (< 500ms p95) using Redis-first lookup.
    During network partitions, returns cached data with a staleness warning.
    """
    from starlette.responses import JSONResponse
    result = await EnforcementService.verify_by_vehicle(vehicle_registration)
    # If Journey Service is partitioned, flag response as potentially stale
    if partition_mgr.is_partitioned("journey-service"):
        response = JSONResponse(content=result.model_dump(mode="json"))
        response.headers["X-Data-Staleness"] = "STALE"
        response.headers["X-Partition-Status"] = "journey-service:PARTITIONED"
        return response
    return result


@app.get(
    "/api/enforcement/verify/license/{license_number}",
    response_model=VerificationResponse,
    dependencies=[Depends(require_role("ENFORCEMENT_AGENT"))],
)
async def verify_license(license_number: str):
    """
    Verify if a driver (by license number) has a valid active journey booking.
    Falls back to User Service + Journey Service if not cached.
    """
    result = await EnforcementService.verify_by_license(license_number)
    if partition_mgr.is_partitioned("journey-service"):
        from starlette.responses import JSONResponse
        response = JSONResponse(content=result.model_dump(mode="json"))
        response.headers["X-Data-Staleness"] = "STALE"
        response.headers["X-Partition-Status"] = "journey-service:PARTITIONED"
        return response
    return result


@app.get("/health/partitions")
async def partition_status():
    """Check partition status for all dependencies."""
    return partition_mgr.get_status()
