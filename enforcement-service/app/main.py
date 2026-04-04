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

setup_logging("enforcement-service")
logger = logging.getLogger(__name__)


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

    yield

    logger.info("Enforcement Service shutting down...")
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
    """
    return await EnforcementService.verify_by_vehicle(vehicle_registration)


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
    return await EnforcementService.verify_by_license(license_number)
