"""
Correlation IDs for distributed tracing — Feature 2.

Provides middleware to intercept X-Request-ID (or generate a new UUID),
stores it in a context variable, and injects it into outgoing HTTP and RabbitMQ calls.
"""

import uuid
from contextvars import ContextVar
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Context variable to hold the correlation ID per async request
_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="UNKNOWN")

def get_correlation_id() -> str:
    """Retrieve the correlation ID for the current context."""
    return _correlation_id.get()

def set_correlation_id(cid: str):
    """Set the correlation ID for the current context."""
    _correlation_id.set(cid)

def generate_correlation_id() -> str:
    """Generate a new UUID as correlation ID."""
    return str(uuid.uuid4())

class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """
    Middleware that reads X-Request-ID or X-Correlation-ID from the request headers,
    sets it in the ContextVar, and adds it to the response headers.
    """
    async def dispatch(self, request: Request, call_next) -> Response:
        # Check standard gateway headers
        correlation_id = (
            request.headers.get("X-Request-ID") or
            request.headers.get("X-Correlation-ID") or
            generate_correlation_id()
        )
        
        # Set for the current async task
        token = _correlation_id.set(correlation_id)
        
        try:
            response = await call_next(request)
            # Make sure it's in the response so client can track
            response.headers["X-Correlation-ID"] = correlation_id
            return response
        finally:
            _correlation_id.reset(token)

import logging
class CorrelationIDFilter(logging.Filter):
    """Logging filter to automatically inject correlation_id into log records."""
    def filter(self, record):
        record.correlation_id = get_correlation_id()
        return True
