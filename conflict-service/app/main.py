"""
Conflict Detection Service - FastAPI application entry point.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import init_db
from .routes import router
from .consumer import start_consumer
from shared.config import setup_logging
from shared.schemas import HealthResponse
from shared.messaging import get_broker, close_broker

setup_logging("conflict-service")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Conflict Detection Service starting up...")
    await init_db()
    logger.info("Database tables created/verified")

    try:
        broker = await get_broker()
        await start_consumer(broker)
        logger.info("Connected to RabbitMQ and started consumer")
    except Exception as e:
        logger.warning(f"Could not connect to RabbitMQ: {e}")

    yield

    logger.info("Conflict Detection Service shutting down...")
    await close_broker()


app = FastAPI(
    title="Journey Booking - Conflict Detection Service",
    description="Detects scheduling conflicts: time overlaps and road capacity limits",
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

app.include_router(router)


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return HealthResponse(
        status="healthy",
        service="conflict-service",
        timestamp=datetime.utcnow(),
    )
