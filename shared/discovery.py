"""
UDP Peer Discovery — broadcasts this node's region info on LAN.

Each node broadcasts {region_name, api_host, api_port} on UDP_DISCOVERY_PORT
every BROADCAST_INTERVAL seconds. On receipt of a broadcast from a new peer,
calls the on_peer_discovered callback so the peer can be registered with the
health monitor and peer registry.
"""

import asyncio
import json
import logging
import os
import select
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

UDP_DISCOVERY_PORT = int(os.getenv("UDP_DISCOVERY_PORT", "5001"))
BROADCAST_INTERVAL = 5  # seconds per plan


@dataclass
class RegionPeer:
    region_name: str
    api_host: str
    api_port: int
    journey_service_url: str
    last_seen: float = field(default_factory=time.monotonic)
    graph_summary: Dict = field(default_factory=dict)

    @property
    def health_url(self) -> str:
        return f"{self.journey_service_url}/health"


class UDPDiscovery:
    """
    UDP broadcast peer discovery.

    Broadcasts this node's region info every BROADCAST_INTERVAL seconds and
    listens for broadcasts from other region nodes on the same LAN.

    Key events:
      on_peer_discovered(peer)  — called when a new region is first seen
      on_peer_lost(peer)        — called when a peer hasn't broadcast for STALE_TIMEOUT s
    """

    STALE_TIMEOUT = 30  # seconds

    def __init__(
        self,
        region_name: str,
        api_host: str,
        api_port: int,
        discovery_port: int = UDP_DISCOVERY_PORT,
        on_peer_discovered: Optional[Callable[["RegionPeer"], Any]] = None,
        on_peer_lost: Optional[Callable[["RegionPeer"], Any]] = None,
    ):
        self.region_name = region_name
        self.api_port = api_port
        self.discovery_port = discovery_port
        self.on_peer_discovered = on_peer_discovered
        self.on_peer_lost = on_peer_lost
        self._peers: Dict[str, RegionPeer] = {}
        self._running = False
        self._api_host = api_host or self._get_local_ip()

    @staticmethod
    def _get_local_ip() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"

    @property
    def journey_service_url(self) -> str:
        return f"http://{self._api_host}:{self.api_port}"

    def get_peers(self) -> Dict[str, "RegionPeer"]:
        return dict(self._peers)

    def _broadcast_payload(self) -> bytes:
        payload = {
            "region_name": self.region_name,
            "api_host": self._api_host,
            "api_port": self.api_port,
            "journey_service_url": self.journey_service_url,
            "graph_summary": {"type": "road_network", "region": self.region_name},
        }
        return json.dumps(payload).encode()

    async def start(self):
        self._running = True
        asyncio.create_task(self._broadcast_loop(), name=f"udp-broadcast-{self.region_name}")
        asyncio.create_task(self._listen_loop(), name=f"udp-listen-{self.region_name}")
        asyncio.create_task(self._stale_cleanup_loop(), name=f"udp-cleanup-{self.region_name}")
        logger.info(
            f"[Discovery] Started for region '{self.region_name}' "
            f"api={self.journey_service_url} udp_port={self.discovery_port}"
        )

    async def stop(self):
        self._running = False

    async def _broadcast_loop(self):
        payload = self._broadcast_payload()
        while self._running:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._send_broadcast, payload)
                logger.debug(f"[Discovery] Broadcast sent by '{self.region_name}'")
            except Exception as exc:
                logger.debug(f"[Discovery] Broadcast error: {exc}")
            await asyncio.sleep(BROADCAST_INTERVAL)

    def _send_broadcast(self, payload: bytes):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(2)
            sock.sendto(payload, ("<broadcast>", self.discovery_port))

    async def _listen_loop(self):
        loop = asyncio.get_event_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass
        sock.bind(("", self.discovery_port))
        sock.setblocking(False)
        logger.info(f"[Discovery] Listening on UDP port {self.discovery_port}")

        try:
            while self._running:
                try:
                    ready, _, _ = await loop.run_in_executor(
                        None, lambda: select.select([sock], [], [], 1.0)
                    )
                    if ready:
                        data, addr = sock.recvfrom(4096)
                        await self._handle_broadcast(data, addr)
                except Exception:
                    pass
                await asyncio.sleep(0.05)
        finally:
            sock.close()

    async def _handle_broadcast(self, data: bytes, addr):
        try:
            payload = json.loads(data.decode())
            region_name = payload.get("region_name", "")
            if not region_name or region_name == self.region_name:
                return  # ignore self

            api_host = payload.get("api_host") or addr[0]
            api_port = int(payload.get("api_port", self.api_port))
            journey_url = payload.get("journey_service_url", f"http://{api_host}:{api_port}")

            is_new = region_name not in self._peers
            peer = RegionPeer(
                region_name=region_name,
                api_host=api_host,
                api_port=api_port,
                journey_service_url=journey_url,
                last_seen=time.monotonic(),
                graph_summary=payload.get("graph_summary", {}),
            )
            self._peers[region_name] = peer

            if is_new:
                logger.info(
                    f"[Discovery] New peer: '{region_name}' at {journey_url} "
                    f"(inter-region edge created automatically)"
                )
                if self.on_peer_discovered:
                    result = self.on_peer_discovered(peer)
                    if asyncio.iscoroutine(result):
                        await result
            else:
                self._peers[region_name].last_seen = time.monotonic()

        except Exception as exc:
            logger.debug(f"[Discovery] Bad broadcast from {addr}: {exc}")

    async def _stale_cleanup_loop(self):
        while self._running:
            await asyncio.sleep(10)
            now = time.monotonic()
            stale = [
                name for name, peer in list(self._peers.items())
                if now - peer.last_seen > self.STALE_TIMEOUT
            ]
            for name in stale:
                peer = self._peers.pop(name, None)
                if peer:
                    logger.warning(f"[Discovery] Peer '{name}' stale — removed from peer registry")
                    if self.on_peer_lost:
                        result = self.on_peer_lost(peer)
                        if asyncio.iscoroutine(result):
                            await result
