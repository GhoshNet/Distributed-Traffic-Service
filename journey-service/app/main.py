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

setup_logging("journey-service")
logger = logging.getLogger(__name__)


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
    except Exception as e:
        logger.warning(f"Could not connect to RabbitMQ: {e}")

    yield

    logger.info("Journey Service shutting down...")
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
