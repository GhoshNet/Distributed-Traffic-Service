"""
Notification Service - FastAPI application with WebSocket support.
"""

import logging
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, Query
from fastapi.middleware.cors import CORSMiddleware

from .consumer import start_consumer, register_ws, unregister_ws, get_notifications
from shared.config import setup_logging
from shared.schemas import HealthResponse
from shared.auth import decode_token
from shared.messaging import get_broker, close_broker

setup_logging("notification-service")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Notification Service starting up...")

    try:
        broker = await get_broker()
        await start_consumer(broker)
        logger.info("Connected to RabbitMQ and started consumer")
    except Exception as e:
        logger.warning(f"Could not connect to RabbitMQ: {e}")

    yield

    logger.info("Notification Service shutting down...")
    await close_broker()


app = FastAPI(
    title="Journey Booking - Notification Service",
    description="Delivers booking notifications via WebSocket, email (simulated), and REST API",
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
        service="notification-service",
        timestamp=datetime.utcnow(),
    )


@app.get("/api/notifications/")
async def list_notifications(
    token: str = Query(..., description="JWT access token"),
    limit: int = Query(20, ge=1, le=50),
):
    """Get recent notifications for the authenticated user."""
    payload = decode_token(token)
    user_id = payload["sub"]
    notifications = await get_notifications(user_id, limit)
    return {"notifications": notifications, "count": len(notifications)}


@app.websocket("/ws/notifications/")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    """
    WebSocket endpoint for real-time notifications.
    Connect with: ws://host/ws/notifications/?token=<JWT>
    """
    try:
        payload = decode_token(token)
        user_id = payload["sub"]
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()
    register_ws(user_id, websocket)
    logger.info(f"WebSocket connected for user {user_id}")

    try:
        while True:
            # Keep connection alive, handle pings
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        unregister_ws(user_id, websocket)
        logger.info(f"WebSocket disconnected for user {user_id}")
    except Exception as e:
        unregister_ws(user_id, websocket)
        logger.warning(f"WebSocket error for user {user_id}: {e}")
