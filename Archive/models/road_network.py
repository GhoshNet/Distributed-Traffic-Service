# ============================================================
# models/road_network.py — NetworkX-based road graph
# ============================================================
import random
from typing import List, Optional, Tuple
import networkx as nx


class RoadNetwork:
    """
    Represents the road network for one region.
    Internal cities are connected via a random connected graph.
    Inter-region edges are added automatically when peers are discovered.
    """

    def __init__(self, region_name: str, cities: List[str]):
        self.region_name = region_name
        self.cities = cities
        self.graph: nx.Graph = nx.Graph()
        self._build_internal_graph()

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------
    def _build_internal_graph(self):
        for city in self.cities:
            self.graph.add_node(city, region=self.region_name, node_type="city")

        # Spanning tree first — guarantees connectivity
        shuffled = self.cities[:]
        random.shuffle(shuffled)
        for i in range(len(shuffled) - 1):
            dist = random.randint(50, 400)
            self.graph.add_edge(
                shuffled[i], shuffled[i + 1],
                weight=dist, capacity=5, bookings=0, road_type="highway",
            )

        # Extra random edges for realism (30 % chance per pair)
        for i in range(len(self.cities)):
            for j in range(i + 2, len(self.cities)):
                if not self.graph.has_edge(self.cities[i], self.cities[j]):
                    if random.random() < 0.30:
                        dist = random.randint(80, 500)
                        self.graph.add_edge(
                            self.cities[i], self.cities[j],
                            weight=dist, capacity=5, bookings=0, road_type="main_road",
                        )

    def add_inter_region_edge(self, local_city: str, remote_city: str,
                               remote_region: str, distance: int = None) -> int:
        """Connect a local city to a gateway city of a remote region."""
        if distance is None:
            distance = random.randint(300, 2000)

        if remote_city not in self.graph.nodes:
            self.graph.add_node(
                remote_city, region=remote_region, node_type="gateway"
            )

        if not self.graph.has_edge(local_city, remote_city):
            self.graph.add_edge(
                local_city, remote_city,
                weight=distance, capacity=5, bookings=0, road_type="inter_region",
            )
        return distance

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def find_route(self, origin: str, destination: str) -> Optional[List[str]]:
        try:
            if origin not in self.graph or destination not in self.graph:
                return None
            return nx.shortest_path(self.graph, origin, destination, weight="weight")
        except nx.NetworkXNoPath:
            return None

    def get_route_distance(self, path: List[str]) -> int:
        total = 0
        for i in range(len(path) - 1):
            if self.graph.has_edge(path[i], path[i + 1]):
                total += self.graph[path[i]][path[i + 1]].get("weight", 100)
        return total

    # ------------------------------------------------------------------
    # Capacity management
    # ------------------------------------------------------------------
    def check_road_capacity(self, path: List[str]) -> bool:
        """Return True if every segment on the path has spare capacity."""
        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            if self.graph.has_edge(u, v):
                edge = self.graph[u][v]
                if edge.get("bookings", 0) >= edge.get("capacity", 5):
                    return False
        return True

    def reserve_road(self, path: List[str]):
        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            if self.graph.has_edge(u, v):
                self.graph[u][v]["bookings"] = self.graph[u][v].get("bookings", 0) + 1

    def release_road(self, path: List[str]):
        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            if self.graph.has_edge(u, v):
                cur = self.graph[u][v].get("bookings", 0)
                self.graph[u][v]["bookings"] = max(0, cur - 1)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def all_city_names(self) -> List[str]:
        return [n for n, d in self.graph.nodes(data=True) if d.get("node_type") == "city"]

    def gateway_city(self) -> str:
        """Return the first local city used as the inter-region gateway."""
        return self.cities[0] if self.cities else "UNKNOWN"

    def to_dict(self) -> dict:
        return {
            "nodes": [
                {
                    "id": n,
                    "region": self.graph.nodes[n].get("region"),
                    "node_type": self.graph.nodes[n].get("node_type", "city"),
                }
                for n in self.graph.nodes
            ],
            "edges": [
                {
                    "from": u, "to": v,
                    "weight": d.get("weight", 100),
                    "road_type": d.get("road_type", "road"),
                    "capacity": d.get("capacity", 5),
                    "bookings": d.get("bookings", 0),
                }
                for u, v, d in self.graph.edges(data=True)
            ],
        }

    def print_graph(self):
        """Pretty-print the graph to stdout."""
        from rich.console import Console
        from rich.table import Table

        c = Console()
        c.print(f"\n[bold bright_green]  Road Network: {self.region_name}[/bold bright_green]")
        local = [n for n, d in self.graph.nodes(data=True) if d.get("node_type") == "city"]
        gateways = [n for n, d in self.graph.nodes(data=True) if d.get("node_type") == "gateway"]
        c.print(f"  [green]Local cities:[/green] {', '.join(local)}")
        if gateways:
            c.print(f"  [cyan]Gateway nodes:[/cyan] {', '.join(gateways)}")

        tbl = Table(title="Roads", show_lines=True)
        tbl.add_column("From", style="yellow")
        tbl.add_column("To", style="yellow")
        tbl.add_column("Dist (km)", justify="right")
        tbl.add_column("Bookings/Cap", justify="right")
        tbl.add_column("Type")

        for u, v, d in self.graph.edges(data=True):
            tbl.add_row(
                u, v,
                str(d.get("weight", "?")),
                f"{d.get('bookings',0)}/{d.get('capacity',5)}",
                d.get("road_type", "road"),
            )
        c.print(tbl)
