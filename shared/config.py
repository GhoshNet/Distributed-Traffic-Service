"""
Shared configuration and utilities.
"""

import os
import logging


def setup_logging(service_name: str, level: str = "INFO"):
    """Configure structured logging for a service."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format=f"%(asctime)s [{service_name}] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Reduce noise from libraries
    logging.getLogger("aio_pika").setLevel(logging.WARNING)
    logging.getLogger("aiormq").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    # httpx/httpcore log every outgoing request at INFO — too noisy for health pings
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


class Settings:
    """Base settings shared across services."""

    SERVICE_NAME: str = os.getenv("SERVICE_NAME", "unknown-service")
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
    RABBITMQ_URL: str = os.getenv(
        "RABBITMQ_URL",
        "amqp://journey_admin:journey_pass@rabbitmq:5672/journey_vhost",
    )
    JWT_SECRET: str = os.getenv(
        "JWT_SECRET", "super-secret-jwt-key-change-in-production"
    )
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    REGION_NAME: str = os.getenv("REGION_NAME", "Dublin")
    UDP_DISCOVERY_PORT: int = int(os.getenv("UDP_DISCOVERY_PORT", "5001"))
    API_HOST: str = os.getenv("API_HOST", "")
    API_PORT: int = int(os.getenv("API_PORT", "8000"))
