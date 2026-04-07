"""
Network Partition Detection, Handling, and Merge Reconciliation.

This module provides partition-aware behaviour for the microservices:

1. **Detection**: Each service periodically probes its dependencies (DB, Redis,
   RabbitMQ, peer services). When a dependency becomes unreachable, the service
   enters a PARTITIONED state for that dependency.

2. **Handling during partition**:
   - Writes are queued locally in a partition log (SQLite / in-memory).
   - Read requests are served from local state (cache/replica) with a
     staleness warning header.
   - The service continues to accept requests in degraded mode rather than
     failing entirely.

3. **Merge / Reconciliation on heal**:
   - When connectivity is restored, queued writes are replayed.
   - Conflict resolution uses last-writer-wins (LWW) with logical timestamps.
   - The analytics audit chain is re-verified after merge.

4. **N partitions without majority**:
   - Since the Conflict Service is a single authority, a partition that isolates
     it causes all bookings to be rejected (safe default — no split-brain).
   - Enforcement falls back to cached data with a "STALE" flag.
   - Notification delivery is deferred until RabbitMQ is reachable.

This is designed for an academic distributed systems context, not a production
Raft/Paxos consensus system.
"""

import asyncio
import logging
import time
from enum import Enum
from typing import Optional, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class PartitionState(str, Enum):
    CONNECTED = "CONNECTED"
    SUSPECTED = "SUSPECTED"      # probe failed once — may be transient
    PARTITIONED = "PARTITIONED"  # multiple probes failed — confirmed partition
    MERGING = "MERGING"          # connectivity restored, replaying queued ops


@dataclass
class DependencyStatus:
    name: str
    state: PartitionState = PartitionState.CONNECTED
    last_seen: float = 0.0
    consecutive_failures: int = 0
    partition_start: Optional[float] = None
    queued_operations: list = field(default_factory=list)


class PartitionManager:
    """
    Monitors connectivity to dependencies and manages partition state.

    Each service creates one PartitionManager and registers its dependencies
    (e.g., postgres, redis, rabbitmq, conflict-service). A background task
    probes each dependency periodically.
    """

    SUSPECT_THRESHOLD = 1     # failures before SUSPECTED
    PARTITION_THRESHOLD = 3   # failures before PARTITIONED
    PROBE_INTERVAL = 5.0      # seconds between probes
    MAX_QUEUED_OPS = 1000     # max queued operations before dropping oldest

    def __init__(self, service_name: str):
        self.service_name = service_name
        self.dependencies: dict[str, DependencyStatus] = {}
        self._probes: dict[str, Callable] = {}
        self._merge_handlers: dict[str, Callable] = {}
        self._running = False

    def register_dependency(
        self,
        name: str,
        probe_fn: Callable,
        merge_fn: Optional[Callable] = None,
    ):
        """Register a dependency with its health probe and optional merge handler."""
        self.dependencies[name] = DependencyStatus(name=name, last_seen=time.monotonic())
        self._probes[name] = probe_fn
        if merge_fn:
            self._merge_handlers[name] = merge_fn

    async def start(self):
        """Start the background partition detector."""
        self._running = True
        asyncio.create_task(self._probe_loop())
        logger.info(f"[PartitionManager:{self.service_name}] started monitoring {list(self.dependencies.keys())}")

    async def stop(self):
        self._running = False

    def is_connected(self, dep_name: str) -> bool:
        dep = self.dependencies.get(dep_name)
        return dep is not None and dep.state == PartitionState.CONNECTED

    def is_partitioned(self, dep_name: str) -> bool:
        dep = self.dependencies.get(dep_name)
        return dep is not None and dep.state == PartitionState.PARTITIONED

    def queue_operation(self, dep_name: str, operation: dict):
        """Queue an operation to be replayed when partition heals."""
        dep = self.dependencies.get(dep_name)
        if dep and dep.state in (PartitionState.PARTITIONED, PartitionState.SUSPECTED):
            if len(dep.queued_operations) >= self.MAX_QUEUED_OPS:
                dep.queued_operations.pop(0)  # drop oldest
            operation["queued_at"] = time.monotonic()
            dep.queued_operations.append(operation)
            logger.debug(f"Queued operation for {dep_name} (total: {len(dep.queued_operations)})")

    def get_status(self) -> dict:
        """Return partition status for all dependencies."""
        return {
            "service": self.service_name,
            "dependencies": {
                name: {
                    "state": dep.state.value,
                    "consecutive_failures": dep.consecutive_failures,
                    "partition_duration_s": (
                        round(time.monotonic() - dep.partition_start, 1)
                        if dep.partition_start else None
                    ),
                    "queued_operations": len(dep.queued_operations),
                }
                for name, dep in self.dependencies.items()
            },
        }

    # ------------------------------------------------------------------
    # Background probe loop
    # ------------------------------------------------------------------

    async def _probe_loop(self):
        while self._running:
            for name, probe_fn in self._probes.items():
                try:
                    await asyncio.wait_for(probe_fn(), timeout=3.0)
                    await self._on_probe_success(name)
                except Exception as e:
                    await self._on_probe_failure(name, e)
            await asyncio.sleep(self.PROBE_INTERVAL)

    async def _on_probe_success(self, name: str):
        dep = self.dependencies[name]
        old_state = dep.state

        if old_state in (PartitionState.PARTITIONED, PartitionState.SUSPECTED):
            dep.state = PartitionState.MERGING
            logger.info(f"[PartitionManager] {name}: {old_state.value} → MERGING (connectivity restored)")
            await self._run_merge(name)

        dep.state = PartitionState.CONNECTED
        dep.consecutive_failures = 0
        dep.last_seen = time.monotonic()
        dep.partition_start = None

        if old_state != PartitionState.CONNECTED:
            logger.info(f"[PartitionManager] {name}: → CONNECTED")

    async def _on_probe_failure(self, name: str, error: Exception):
        dep = self.dependencies[name]
        dep.consecutive_failures += 1

        if dep.consecutive_failures >= self.PARTITION_THRESHOLD:
            if dep.state != PartitionState.PARTITIONED:
                dep.state = PartitionState.PARTITIONED
                dep.partition_start = time.monotonic()
                logger.error(
                    f"[PartitionManager] {name}: → PARTITIONED "
                    f"(after {dep.consecutive_failures} failures: {error})"
                )
        elif dep.consecutive_failures >= self.SUSPECT_THRESHOLD:
            if dep.state == PartitionState.CONNECTED:
                dep.state = PartitionState.SUSPECTED
                logger.warning(f"[PartitionManager] {name}: → SUSPECTED ({error})")

    async def _run_merge(self, name: str):
        """Replay queued operations and run merge handler on partition heal."""
        dep = self.dependencies[name]
        queued = dep.queued_operations.copy()
        dep.queued_operations.clear()

        if queued:
            logger.info(f"[PartitionManager] Replaying {len(queued)} queued operations for {name}")

        merge_fn = self._merge_handlers.get(name)
        if merge_fn:
            try:
                await merge_fn(queued)
                logger.info(f"[PartitionManager] Merge completed for {name}")
            except Exception as e:
                logger.error(f"[PartitionManager] Merge failed for {name}: {e}")
                # Re-queue operations that failed to merge
                dep.queued_operations.extend(queued)


# ---------------------------------------------------------------------------
# Probe factories for common dependencies
# ---------------------------------------------------------------------------

def make_postgres_probe(engine):
    """Create a probe function that tests PostgreSQL connectivity."""
    async def probe():
        from sqlalchemy import text
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    return probe


def make_redis_probe(redis_client):
    """Create a probe function that tests Redis connectivity."""
    async def probe():
        await redis_client.ping()
    return probe


def make_rabbitmq_probe(get_broker_fn):
    """Create a probe function that tests RabbitMQ connectivity."""
    async def probe():
        broker = await get_broker_fn()
        if not broker.is_connected:
            raise ConnectionError("RabbitMQ not connected")
    return probe


def make_http_probe(url: str):
    """Create a probe function that tests HTTP service connectivity."""
    async def probe():
        import httpx
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    return probe
