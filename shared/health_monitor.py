"""
Peer Health Monitor — ported and adapted from Archive/services/health_monitor.py

Tracks the liveness of peer services using the same three-state machine as the
Archive's HealthMonitorService:

    ALIVE  →  SUSPECT  (after SUSPECT_THRESHOLD consecutive missed pings)
           →  DEAD     (after DEAD_THRESHOLD consecutive missed pings)
    SUSPECT/DEAD → ALIVE  (when ping responds again, triggers on_recovery)

This complements the PartitionManager (which tracks dependency connectivity)
by providing a named, per-service liveness view that maps onto the Archive's
mental model. The health state is exposed via journey-service's /health/nodes
endpoint and surfaced in the frontend dashboard.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Coroutine, Optional

import httpx

logger = logging.getLogger(__name__)


class NodeStatus(str, Enum):
    ALIVE = "ALIVE"
    SUSPECT = "SUSPECT"
    DEAD = "DEAD"


@dataclass
class PeerNode:
    name: str
    ping_url: str
    status: NodeStatus = NodeStatus.ALIVE
    consecutive_failures: int = 0
    last_seen: float = field(default_factory=time.monotonic)
    # Optional async callback invoked when a SUSPECT/DEAD node recovers
    on_recovery: Optional[Callable[["PeerNode"], Coroutine]] = field(
        default=None, repr=False
    )


class PeerHealthMonitor:
    """
    Async background task that periodically pings each registered service.

    Usage::

        monitor = PeerHealthMonitor("journey-service")
        monitor.register("conflict-service", "http://conflict-service:8000/health")
        monitor.register("user-service",     "http://user-service:8000/health")
        await monitor.start()            # launches background loop
        ...
        status = monitor.get_status()    # returns dict for /health/nodes
        await monitor.stop()
    """

    SUSPECT_THRESHOLD = 3   # consecutive failures before SUSPECT
    DEAD_THRESHOLD = 6      # consecutive failures before DEAD
    HEARTBEAT_INTERVAL = 10  # seconds between sweeps

    def __init__(self, service_name: str):
        self.service_name = service_name
        self._peers: dict[str, PeerNode] = {}
        self._running = False
        self._local_only_mode = False  # mirrors Archive's graceful-degradation flag

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        ping_url: str,
        on_recovery: Optional[Callable[[PeerNode], Coroutine]] = None,
    ):
        """Register a peer service to monitor."""
        self._peers[name] = PeerNode(name=name, ping_url=ping_url, on_recovery=on_recovery)
        logger.info(f"[HealthMonitor:{self.service_name}] registered peer '{name}' → {ping_url}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        self._running = True
        asyncio.create_task(self._loop(), name=f"health-monitor-{self.service_name}")
        logger.info(
            f"[HealthMonitor:{self.service_name}] started "
            f"(interval={self.HEARTBEAT_INTERVAL}s, "
            f"suspect_at={self.SUSPECT_THRESHOLD}, dead_at={self.DEAD_THRESHOLD})"
        )

    async def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def is_alive(self, name: str) -> bool:
        peer = self._peers.get(name)
        return peer is not None and peer.status == NodeStatus.ALIVE

    def is_local_only(self) -> bool:
        return self._local_only_mode

    def get_status(self) -> dict:
        """Return a serialisable snapshot — used by /health/nodes endpoint."""
        return {
            "monitor": self.service_name,
            "local_only_mode": self._local_only_mode,
            "peers": {
                name: {
                    "status": peer.status.value,
                    "consecutive_failures": peer.consecutive_failures,
                    "last_seen_s_ago": round(time.monotonic() - peer.last_seen, 1),
                    "ping_url": peer.ping_url,  # included so frontend can derive API base
                }
                for name, peer in self._peers.items()
            },
        }

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _loop(self):
        while self._running:
            await self._sweep()
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)

    async def _sweep(self):
        if not self._peers:
            return

        alive_count = 0

        for name, peer in self._peers.items():
            reachable = await self._ping(peer.ping_url)

            if reachable:
                alive_count += 1
                prev_status = peer.status
                if prev_status in (NodeStatus.SUSPECT, NodeStatus.DEAD):
                    logger.info(
                        f"[HealthMonitor] '{name}' recovered (was {prev_status.value})"
                    )
                    peer.status = NodeStatus.ALIVE
                    peer.consecutive_failures = 0
                    peer.last_seen = time.monotonic()
                    if peer.on_recovery:
                        try:
                            await peer.on_recovery(peer)
                        except Exception as exc:
                            logger.warning(
                                f"[HealthMonitor] recovery callback for '{name}' raised: {exc}"
                            )
                else:
                    peer.consecutive_failures = 0
                    peer.last_seen = time.monotonic()
            else:
                peer.consecutive_failures += 1
                prev_status = peer.status

                if peer.consecutive_failures >= self.DEAD_THRESHOLD:
                    new_status = NodeStatus.DEAD
                elif peer.consecutive_failures >= self.SUSPECT_THRESHOLD:
                    new_status = NodeStatus.SUSPECT
                else:
                    new_status = prev_status

                if new_status != prev_status:
                    peer.status = new_status
                    icon = "DEAD" if new_status == NodeStatus.DEAD else "SUSPECT"
                    logger.warning(
                        f"[HealthMonitor] '{name}' → {icon} "
                        f"(failures={peer.consecutive_failures})"
                    )

        # Graceful-degradation check (mirrors Archive health_monitor.py)
        total = len(self._peers)
        if total > 0:
            ratio = alive_count / total
            if ratio < 0.5 and not self._local_only_mode:
                self._local_only_mode = True
                logger.error(
                    f"[HealthMonitor] GRACEFUL DEGRADATION: "
                    f"only {alive_count}/{total} peers reachable → LOCAL ONLY mode"
                )
            elif ratio >= 0.5 and self._local_only_mode:
                self._local_only_mode = False
                logger.info(
                    f"[HealthMonitor] enough peers back ({alive_count}/{total}) → "
                    "exiting LOCAL ONLY mode"
                )

    @staticmethod
    async def _ping(url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(url)
                return resp.status_code == 200
        except Exception:
            return False
