# ============================================================
# node_state.py — Shared mutable state for a running GDTS node
# ============================================================
import threading


class NodeState:
    """
    Single shared-state object passed to all services.
    Acts as the glue between services without tight coupling.
    """

    def __init__(self):
        # Identity
        self.region_name: str = None
        self.host: str = None
        self.api_port: int = None
        self.started_at: str = None

        # Core components (set during start-up)
        self.road_network = None       # models.road_network.RoadNetwork
        self.db = None                 # database.db.Database

        # Service references (set after services are created)
        self.booking_service = None
        self.coordinator = None
        self.replication_service = None
        self.gateway = None
        self.region_service = None

        # ---- Simulatable flags ----
        self.network_delay_ms: int = 0          # inject latency on all outgoing calls
        self.failure_simulated: bool = False    # node acts as if crashed
        self.local_only_mode: bool = False      # refuse cross-region comms
        self.concurrent_storm_active: bool = False

        self._lock = threading.Lock()

    def is_ready(self) -> bool:
        return all([self.region_name, self.api_port, self.road_network, self.db])

    def to_dict(self) -> dict:
        return {
            "region_name": self.region_name,
            "host": self.host,
            "api_port": self.api_port,
            "cities": self.road_network.cities if self.road_network else [],
            "started_at": self.started_at,
            "network_delay_ms": self.network_delay_ms,
            "failure_simulated": self.failure_simulated,
            "local_only_mode": self.local_only_mode,
        }
