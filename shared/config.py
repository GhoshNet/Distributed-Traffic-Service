"""
Shared configuration and utilities.
"""

import os
import logging
import threading
from collections import deque
from datetime import datetime, timezone

# ── Cross-node log buffer ─────────────────────────────────────────────────────
# In-memory ring buffer — stores recent log entries for /admin/logs endpoint.
# Exposed so the frontend can aggregate logs from all nodes into one view.

_log_buffer: deque = deque(maxlen=500)
_log_lock = threading.Lock()
_node_id: str = os.environ.get("HOSTNAME", os.environ.get("NODE_ADDR", "unknown"))


class _BufferedHandler(logging.Handler):
    """Logging handler that appends records to the in-memory ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "node": _node_id,
                "service": record.name,
                "level": record.levelname,
                "msg": self.format(record),
            }
            with _log_lock:
                _log_buffer.append(entry)
        except Exception:
            pass  # never let logging errors crash the app


def get_recent_logs(limit: int = 200) -> list:
    """Return the last `limit` log entries (oldest first, newest last)."""
    with _log_lock:
        buf = list(_log_buffer)
    return buf[-limit:] if len(buf) > limit else buf


def setup_logging(service_name: str, level: str = "INFO"):
    """Configure structured logging for a service."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format=f"%(asctime)s [{service_name}] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    # Attach the ring-buffer handler to the root logger so every service's
    # log output is captured (INFO and above only — keeps buffer lean).
    buf_handler = _BufferedHandler()
    buf_handler.setLevel(logging.INFO)
    buf_handler.setFormatter(
        logging.Formatter(
            fmt=f"%(asctime)s [{service_name}] %(levelname)s %(name)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
    )
    logging.getLogger().addHandler(buf_handler)

    # Reduce noise from libraries
    logging.getLogger("aio_pika").setLevel(logging.WARNING)
    logging.getLogger("aiormq").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


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
