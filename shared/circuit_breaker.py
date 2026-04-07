"""
Circuit Breaker Pattern — Feature 1.

Prevents cascading failures by fast-failing calls to a degraded dependency.

State machine:
  CLOSED   → calls pass through normally; N consecutive failures → OPEN
  OPEN     → calls immediately raise CircuitBreakerOpenError (no network hit)
             After RESET_TIMEOUT seconds, transitions to HALF-OPEN
  HALF-OPEN → one probe call is allowed; success → CLOSED, failure → OPEN again

Usage:
    cb = CircuitBreaker("conflict-service", failure_threshold=3, reset_timeout=30)

    try:
        result = await cb.call(some_async_fn, arg1, arg2)
    except CircuitBreakerOpenError:
        # Handle fast-fail (circuit is open)
        ...
    except Exception as e:
        # Handle actual call failure
        ...
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Callable, Any

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenError(Exception):
    """Raised when a call is attempted while the circuit is OPEN."""
    def __init__(self, service_name: str, retry_after: float):
        self.service_name = service_name
        self.retry_after = retry_after
        super().__init__(
            f"Circuit breaker OPEN for '{service_name}'. "
            f"Retry after {retry_after:.1f}s."
        )


class CircuitBreaker:
    """
    Async-safe circuit breaker for a single downstream dependency.

    Parameters
    ----------
    name : str
        Human-readable name of the dependency (used in logs).
    failure_threshold : int
        Number of consecutive failures before opening the circuit.
    reset_timeout : float
        Seconds to wait in OPEN state before transitioning to HALF-OPEN.
    success_threshold : int
        Number of consecutive successes in HALF-OPEN to close the circuit.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        reset_timeout: float = 30.0,
        success_threshold: int = 1,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.success_threshold = success_threshold

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, fn: Callable, *args, **kwargs) -> Any:
        """
        Execute `fn(*args, **kwargs)` with circuit-breaker protection.

        Raises CircuitBreakerOpenError if the circuit is open.
        Re-raises any exception from `fn` after recording the failure.
        """
        async with self._lock:
            await self._maybe_transition_to_half_open()

            if self._state == CircuitState.OPEN:
                retry_after = self.reset_timeout - (time.monotonic() - self._opened_at)
                raise CircuitBreakerOpenError(self.name, max(retry_after, 0))

        # --- Execute the call outside the lock ---
        try:
            result = await fn(*args, **kwargs)
            await self._on_success()
            return result
        except Exception as exc:
            await self._on_failure(exc)
            raise

    def get_status(self) -> dict:
        """Return a serialisable status snapshot for health endpoints."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "failure_threshold": self.failure_threshold,
            "reset_timeout_s": self.reset_timeout,
            "seconds_until_retry": (
                max(0, self.reset_timeout - (time.monotonic() - self._opened_at))
                if self._state == CircuitState.OPEN else None
            ),
        }

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    async def _maybe_transition_to_half_open(self):
        if (
            self._state == CircuitState.OPEN
            and (time.monotonic() - self._opened_at) >= self.reset_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            self._success_count = 0
            logger.info(f"[CircuitBreaker:{self.name}] → HALF-OPEN (probe call allowed)")

    async def _on_success(self):
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self.success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info(f"[CircuitBreaker:{self.name}] → CLOSED (recovered)")
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0  # reset on any success

    async def _on_failure(self, exc: Exception):
        async with self._lock:
            self._failure_count += 1
            logger.warning(
                f"[CircuitBreaker:{self.name}] failure #{self._failure_count}: {exc}"
            )
            if self._state == CircuitState.HALF_OPEN:
                # Probe failed — reopen immediately
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.error(f"[CircuitBreaker:{self.name}] → OPEN (probe failed)")
            elif (
                self._state == CircuitState.CLOSED
                and self._failure_count >= self.failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.error(
                    f"[CircuitBreaker:{self.name}] → OPEN "
                    f"(threshold {self.failure_threshold} reached)"
                )


# ---------------------------------------------------------------------------
# Global registry — one circuit breaker per downstream dependency
# ---------------------------------------------------------------------------

_registry: dict[str, CircuitBreaker] = {}


def get_circuit_breaker(
    name: str,
    failure_threshold: int = 3,
    reset_timeout: float = 30.0,
) -> CircuitBreaker:
    """Get-or-create a named circuit breaker from the global registry."""
    if name not in _registry:
        _registry[name] = CircuitBreaker(
            name=name,
            failure_threshold=failure_threshold,
            reset_timeout=reset_timeout,
        )
    return _registry[name]


def get_all_circuit_breaker_statuses() -> list[dict]:
    """Return status snapshots for every registered circuit breaker."""
    return [cb.get_status() for cb in _registry.values()]
