"""
Resilient Conflict Service client.

Tries the local conflict-service first. If it is unreachable or returns a
5xx, tries each peer conflict-service URL in order. This means booking keeps
working even when the local conflict-service container is down, as long as at
least one peer conflict-service is reachable.

Both saga.py and coordinator.py import from here — the retry logic lives in
one place.
"""

import logging
import os
from typing import Optional

import httpx

from shared.schemas import ConflictCheckRequest, ConflictCheckResponse
from shared.circuit_breaker import get_circuit_breaker, CircuitBreakerOpenError
from shared.tracing import get_correlation_id

logger = logging.getLogger(__name__)

# Primary conflict-service URL (same container network)
_PRIMARY_URL: str = os.getenv("CONFLICT_SERVICE_URL", "http://conflict-service:8000")

# Peer conflict-service URLs (direct port 8003, set via PEER_CONFLICT_URLS env)
_PEER_URLS: list[str] = [
    u.strip().rstrip("/")
    for u in os.getenv("PEER_CONFLICT_URLS", "").split(",")
    if u.strip()
]

TIMEOUT_SECONDS = 30


def register_peer_url(url: str) -> None:
    """
    Register a conflict-service peer URL at runtime.
    Called when a new node joins via /internal/journeys/peers/register so that
    booking failover works for dynamically-discovered peers, not just peers
    that were set in PEER_CONFLICT_URLS at container start.
    """
    url = url.rstrip("/")
    if url not in _PEER_URLS:
        _PEER_URLS.append(url)
        logger.info(f"[conflict-client] registered dynamic peer {url}")


def _all_urls() -> list[str]:
    """Return [primary] + peers, deduped, primary always first."""
    seen: set[str] = set()
    out: list[str] = []
    for u in [_PRIMARY_URL] + _PEER_URLS:
        u = u.rstrip("/")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def resilient_conflict_check(
    request: ConflictCheckRequest,
    extra_headers: dict | None = None,
) -> tuple[Optional[ConflictCheckResponse], Optional[str]]:
    """
    POST /api/conflicts/check to the first reachable conflict-service.

    Returns (ConflictCheckResponse, used_url) on success.
    Returns (None, None) when all nodes are unreachable.
    """
    headers = {"X-Correlation-ID": get_correlation_id(), **(extra_headers or {})}
    body = request.model_dump(mode="json")
    urls = _all_urls()

    for url in urls:
        cb_key = f"conflict-service:{url}"
        cb = get_circuit_breaker(cb_key, failure_threshold=3, reset_timeout=30.0)
        try:
            async def _call(u=url):
                async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
                    resp = await client.post(
                        f"{u}/api/conflicts/check",
                        json=body,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    return ConflictCheckResponse(**resp.json())

            result = await cb.call(_call)
            if url != _PRIMARY_URL:
                logger.warning(
                    f"[conflict-client] primary unreachable — served by PEER {url}"
                )
            return result, url

        except CircuitBreakerOpenError:
            logger.warning(f"[conflict-client] circuit breaker OPEN for {url} — trying next")
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            logger.warning(f"[conflict-client] {url} unreachable: {e} — trying next")
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                logger.warning(f"[conflict-client] {url} returned {e.response.status_code} — trying next")
            else:
                # 4xx is an application-level response (conflict found etc.) — return it
                return ConflictCheckResponse(**e.response.json()), url
        except Exception as e:
            logger.error(f"[conflict-client] {url} unexpected error: {e} — trying next")

    logger.error("[conflict-client] all conflict-service nodes unreachable")
    return None, None


async def resilient_conflict_cancel(
    journey_id: str,
    preferred_url: Optional[str] = None,
    extra_headers: dict | None = None,
) -> bool:
    """
    POST /api/conflicts/cancel/{journey_id} to release held capacity.

    Tries preferred_url first (the node that did the PREPARE), then falls back
    to all other known nodes. Returns True if any node accepted the cancel.
    """
    headers = {"X-Correlation-ID": get_correlation_id(), **(extra_headers or {})}
    urls = _all_urls()
    # Move preferred URL to front if specified
    if preferred_url and preferred_url in urls:
        urls = [preferred_url] + [u for u in urls if u != preferred_url]

    for url in urls:
        cb = get_circuit_breaker(f"conflict-cancel:{url}", failure_threshold=3, reset_timeout=30.0)
        try:
            async def _cancel(u=url):
                async with httpx.AsyncClient(timeout=10.0) as client:
                    return await client.post(
                        f"{u}/api/conflicts/cancel/{journey_id}",
                        headers=headers,
                    )

            resp = await cb.call(_cancel)
            if resp.status_code in (204, 404):
                logger.info(
                    f"[conflict-client] CANCEL journey={journey_id} "
                    f"accepted by {url} (status={resp.status_code})"
                )
                return True
            logger.warning(
                f"[conflict-client] CANCEL at {url} returned {resp.status_code}"
            )
        except CircuitBreakerOpenError:
            logger.warning(f"[conflict-client] circuit OPEN for {url} — skipping cancel, trying next")
        except Exception as e:
            logger.warning(f"[conflict-client] CANCEL at {url} failed: {e} — trying next")

    logger.error(f"[conflict-client] CANCEL failed on all nodes for journey={journey_id}")
    return False
