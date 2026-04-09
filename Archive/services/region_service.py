# ============================================================
# services/region_service.py — SERVICE 2: Region management
# ============================================================
from utils.logger import log


class RegionService:
    """
    Owns the road network and exposes region-level info to other services
    and the REST API.
    """

    def __init__(self, node_state):
        self.state = node_state
        log("REGION", f"Region service started for [{node_state.region_name}]")

    def get_info(self) -> dict:
        rn = self.state.road_network
        db = self.state.db
        peers = db.get_all_peers()
        bookings = db.get_all_bookings()
        return {
            "region_name": self.state.region_name,
            "host": self.state.host,
            "api_port": self.state.api_port,
            "cities": rn.cities,
            "gateway_city": rn.gateway_city(),
            "road_count": rn.graph.number_of_edges(),
            "node_count": rn.graph.number_of_nodes(),
            "peer_count": len(peers),
            "booking_count": len(bookings),
            "network_delay_ms": self.state.network_delay_ms,
            "failure_simulated": self.state.failure_simulated,
            "local_only_mode": self.state.local_only_mode,
        }

    def get_network_graph(self) -> dict:
        return self.state.road_network.to_dict()

    def print_region_info(self):
        from rich.console import Console
        from rich.table import Table

        c = Console()
        info = self.get_info()
        c.print(f"\n[bold bright_green]Region: {info['region_name']}[/bold bright_green]")
        c.print(f"  Host:         {info['host']}:{info['api_port']}")
        c.print(f"  Cities:       {', '.join(info['cities'])}")
        c.print(f"  Roads:        {info['road_count']} edges, {info['node_count']} nodes")
        c.print(f"  Peers:        {info['peer_count']}")
        c.print(f"  Bookings:     {info['booking_count']}")
        c.print(f"  Delay (sim):  {info['network_delay_ms']} ms")
        c.print(f"  Failed (sim): {info['failure_simulated']}")
