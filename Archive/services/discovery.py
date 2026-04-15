# ============================================================
# services/discovery.py — SERVICE 1: UDP peer discovery
# ============================================================
import json
import socket
import threading
import time

import config
from utils.logger import log


class DiscoveryService:
    """
    Broadcasts this node's presence on the LAN every DISCOVERY_INTERVAL
    seconds and listens for announcements from other nodes.
    On receiving a new peer announcement it:
      1. Registers the peer in the database
      2. Adds an inter-region road edge in the local road graph
    """

    def __init__(self, node_state):
        self.state = node_state
        self._running = False
        self._broadcast_sock: socket.socket = None
        self._listen_sock: socket.socket = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self):
        self._running = True
        self._broadcast_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._broadcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._broadcast_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        threading.Thread(target=self._broadcast_loop, daemon=True, name="discovery-broadcast").start()
        threading.Thread(target=self._listen_loop,    daemon=True, name="discovery-listen").start()
        log("DISCOVERY", f"UDP discovery started on port {config.DISCOVERY_PORT}")

    def stop(self):
        self._running = False

    def _get_local_ip(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _build_message(self) -> bytes:
        msg = {
            "type": "ANNOUNCE",
            "region_name": self.state.region_name,
            "host": self.state.host or self._get_local_ip(),
            "api_port": self.state.api_port,
            "cities": self.state.road_network.cities,
            "gateway_city": self.state.road_network.gateway_city(),
        }
        return json.dumps(msg).encode()

    # ------------------------------------------------------------------
    # Broadcast loop
    # ------------------------------------------------------------------
    def _broadcast_loop(self):
        while self._running:
            try:
                if not self.state.failure_simulated:
                    self._broadcast_sock.sendto(
                        self._build_message(),
                        ("<broadcast>", config.DISCOVERY_PORT),
                    )
            except Exception as e:
                log("DISCOVERY", f"Broadcast error: {e}", "WARN")
            time.sleep(config.DISCOVERY_INTERVAL)

    # ------------------------------------------------------------------
    # Listen loop
    # ------------------------------------------------------------------
    def _listen_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", config.DISCOVERY_PORT))
        sock.settimeout(1.0)

        while self._running:
            try:
                data, addr = sock.recvfrom(4096)
                msg = json.loads(data.decode())
                if msg.get("region_name") != self.state.region_name:
                    self._handle_announcement(msg, addr[0])
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    log("DISCOVERY", f"Listen error: {e}", "WARN")

    # ------------------------------------------------------------------
    # Handle incoming announcement
    # ------------------------------------------------------------------
    def _handle_announcement(self, msg: dict, sender_ip: str):
        region_name  = msg.get("region_name")
        host         = msg.get("host", sender_ip)
        api_port     = msg.get("api_port")
        cities       = msg.get("cities", [])
        gateway_city = msg.get("gateway_city", cities[0] if cities else "UNKNOWN")

        db = self.state.db
        existing = db.get_peer(region_name)

        db.upsert_peer(region_name, host, api_port, cities, gateway_city)

        if existing is None:
            # ---- new peer! ----
            my_gw = self.state.road_network.gateway_city()
            dist = self.state.road_network.add_inter_region_edge(
                my_gw, gateway_city, region_name
            )
            log(
                "DISCOVERY",
                f"✨ New peer: [{region_name}] @ {host}:{api_port}  "
                f"cities={cities}",
                "SUCCESS",
            )
            log(
                "DISCOVERY",
                f"   Inter-region road added: {my_gw} ←→ {gateway_city} ({dist} km)",
            )
            db.log_event(
                "PEER_DISCOVERED",
                {"region": region_name, "host": host, "port": api_port},
            )
        else:
            # known peer — silently refresh
            db.update_peer_last_seen(region_name)
