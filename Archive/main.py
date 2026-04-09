# ============================================================
# main.py — Entry point for a GDTS region node
# ============================================================
"""
Usage:
    source env/bin/activate
    python main.py

Each invocation becomes one region node.  Run this in multiple terminal
windows / on multiple machines to form the distributed cluster.
"""

import logging
import os
import random
import signal
import socket
import sys
import threading
import time
from datetime import datetime

import requests
from flask import Flask
from flask_cors import CORS
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

import config
from api.routes import api_bp
from database.db import Database
from models.road_network import RoadNetwork
from node_state import NodeState
from services.booking_service import BookingService
from services.coordinator import CoordinatorService
from services.discovery import DiscoveryService
from services.gateway import GatewayService
from services.health_monitor import HealthMonitorService
from services.region_service import RegionService
from services.replication import ReplicationService
from simulation.problems import run_menu
from utils.logger import banner, log, set_region

# Silence Flask/Werkzeug access logs
logging.getLogger("werkzeug").setLevel(logging.ERROR)

console = Console()

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

CITY_POOL = [
    "Dublin", "Cork", "Galway", "Limerick", "Waterford",
    "Kilkenny", "Sligo", "Drogheda", "Athlone", "Tralee",
    "Wexford", "Ennis", "Letterkenny", "Dundalk", "Mullingar",
    "London", "Manchester", "Birmingham", "Leeds", "Liverpool",
    "Paris", "Lyon", "Marseille", "Bordeaux", "Nice",
    "Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne",
    "Madrid", "Barcelona", "Seville", "Valencia", "Bilbao",
    "Rome", "Milan", "Naples", "Turin", "Venice",
    "Warsaw", "Krakow", "Wroclaw", "Gdansk", "Poznan",
    "Amsterdam", "Rotterdam", "Utrecht", "Eindhoven", "Groningen",
    "Brussels", "Antwerp", "Ghent", "Liege", "Bruges",
    "Vienna", "Graz", "Linz", "Salzburg", "Innsbruck",
    "Zurich", "Geneva", "Bern", "Basel", "Lausanne",
    "Stockholm", "Gothenburg", "Malmo", "Uppsala", "Orebro",
    "Oslo", "Bergen", "Trondheim", "Stavanger", "Drammen",
    "Copenhagen", "Aarhus", "Odense", "Aalborg", "Esbjerg",
    "Helsinki", "Tampere", "Turku", "Oulu", "Jyvaskyla",
    "Athens", "Thessaloniki", "Patras", "Heraklion", "Larissa",
    "Lisbon", "Porto", "Braga", "Coimbra", "Setubal",
]


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _find_free_port(start: int) -> int:
    port = start
    while port < start + 100:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("", port))
                return port
            except OSError:
                port += 1
    raise RuntimeError("No free port found in range")


# ──────────────────────────────────────────────────────────────────────
# Setup wizard
# ──────────────────────────────────────────────────────────────────────

def setup_wizard() -> tuple:
    """Interactive setup — returns (region_name, cities, api_port)."""
    console.print(Panel.fit(
        Text(
            f"{config.SYSTEM_NAME}\n"
            f"Version {config.VERSION}\n\n"
            "A globally-distributed journey pre-booking service\n"
            "demonstrating distributed-systems concepts in real time.",
            justify="center",
        ),
        title="[bold bright_cyan]Welcome[/bold bright_cyan]",
        border_style="bright_cyan",
    ))

    console.print("\n[bold]── Region Setup ──[/bold]\n")

    # Region name
    region_name = input("  Enter region name (e.g. Ireland, France): ").strip()
    if not region_name:
        region_name = f"Region-{random.randint(100, 999)}"
    region_name = region_name.replace(" ", "_")

    # Number of cities
    while True:
        try:
            n_cities = int(input("  Number of cities/counties in this region (2–15): ").strip())
            if 2 <= n_cities <= 15:
                break
            console.print("[red]  Please enter a number between 2 and 15.[/red]")
        except ValueError:
            console.print("[red]  Please enter a valid integer.[/red]")

    # City names: auto or manual
    auto = input(f"  Auto-generate {n_cities} city names? (Y/n): ").strip().lower()
    if auto in ("n", "no"):
        cities = []
        console.print(f"  Enter {n_cities} city names (one per line):")
        for i in range(n_cities):
            c = input(f"    City {i+1}: ").strip()
            cities.append(c if c else f"City-{i+1}")
    else:
        # Pick n_cities unique names from the pool that include the region name in at least one
        pool = [c for c in CITY_POOL if c not in [region_name]]
        random.shuffle(pool)
        cities = pool[:n_cities]

    # API port
    suggested_port = _find_free_port(config.API_PORT_START)
    port_input = input(f"  API port [{suggested_port}]: ").strip()
    try:
        api_port = int(port_input) if port_input else suggested_port
    except ValueError:
        api_port = suggested_port

    # Optional seed node
    console.print("\n[dim]  If another node is already running, enter its address to bootstrap.[/dim]")
    seed = input("  Seed node address (e.g. 192.168.1.5:6000, blank to skip): ").strip()

    return region_name, cities, api_port, seed or None


# ──────────────────────────────────────────────────────────────────────
# Flask app factory
# ──────────────────────────────────────────────────────────────────────

def create_flask_app(state: NodeState) -> Flask:
    app = Flask(__name__)
    CORS(app)
    app.config["NODE_STATE"] = state
    app.register_blueprint(api_bp)
    return app


# ──────────────────────────────────────────────────────────────────────
# Flask server thread
# ──────────────────────────────────────────────────────────────────────

def start_flask(app: Flask, port: int):
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)


# ──────────────────────────────────────────────────────────────────────
# Seed-node bootstrap
# ──────────────────────────────────────────────────────────────────────

def bootstrap_from_seed(state: NodeState, seed_address: str):
    """Announce ourselves to a known seed node and fetch its peer list."""
    parts = seed_address.rsplit(":", 1)
    if len(parts) != 2:
        log("DISCOVERY", f"Invalid seed address format: {seed_address}", "WARN")
        return
    host, port = parts[0], parts[1]
    base_url = f"http://{host}:{port}"

    # Announce ourselves
    payload = {
        "region_name": state.region_name,
        "host": state.host,
        "api_port": state.api_port,
        "cities": state.road_network.cities,
        "gateway_city": state.road_network.gateway_city(),
    }
    try:
        requests.post(f"{base_url}/api/peer/announce", json=payload, timeout=5)
        log("DISCOVERY", f"✅ Announced to seed node {seed_address}", "SUCCESS")
    except Exception as e:
        log("DISCOVERY", f"Could not reach seed {seed_address}: {e}", "WARN")
        return

    # Fetch their peer list and announce to everyone
    try:
        resp = requests.get(f"{base_url}/api/peer/list", timeout=5)
        peers = resp.json().get("peers", [])
        for peer in peers:
            if peer["region_name"] == state.region_name:
                continue
            purl = f"http://{peer['host']}:{peer['port']}/api/peer/announce"
            try:
                requests.post(purl, json=payload, timeout=3)
                log("DISCOVERY",
                    f"   Announced to [{peer['region_name']}] @ {peer['host']}:{peer['port']}")
            except Exception:
                pass
    except Exception as e:
        log("DISCOVERY", f"Could not fetch peer list from seed: {e}", "WARN")


# ──────────────────────────────────────────────────────────────────────
# Print startup summary
# ──────────────────────────────────────────────────────────────────────

def print_startup_summary(state: NodeState):
    banner(f"Node Started — Region: {state.region_name}")
    log("MAIN", f"🌍 Region  : {state.region_name}")
    log("MAIN", f"🖥️  Host    : {state.host}:{state.api_port}")
    log("MAIN", f"🏙️  Cities  : {', '.join(state.road_network.cities)}")
    log("MAIN", f"🛣️  Roads   : {state.road_network.graph.number_of_edges()} edges")
    log("MAIN", f"💾 DB      : {state.db.db_path}")
    log("MAIN", "")
    log("MAIN", "Services running:")
    log("MAIN", "  [1] Discovery Service   — UDP broadcast peer discovery")
    log("MAIN", "  [2] Region Service      — Road network & metadata")
    log("MAIN", "  [3] Booking Service     — Journey CRUD & conflict detection")
    log("MAIN", "  [4] Coordinator Service — 2PC cross-region transactions")
    log("MAIN", "  [5] Health Monitor      — Heartbeat & failure detection")
    log("MAIN", "  [6] Replication Service — Eventual consistency sync")
    log("MAIN", "  [7] Gateway Service     — Request routing")
    log("MAIN", "")
    log("MAIN",
        f"REST API  → http://{state.host}:{state.api_port}/api/health/ping",
        "SUCCESS")
    state.road_network.print_graph()


# ──────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────

def main():
    # ── 1. Setup wizard ──────────────────────────────────────────────
    region_name, cities, api_port, seed_address = setup_wizard()

    # ── 2. Initialise shared state ───────────────────────────────────
    state = NodeState()
    state.region_name = region_name
    state.host        = _get_local_ip()
    state.api_port    = api_port
    state.started_at  = datetime.utcnow().isoformat()

    set_region(region_name)     # configure logger prefix

    # ── 3. Build road network ─────────────────────────────────────────
    state.road_network = RoadNetwork(region_name, cities)
    log("REGION", f"Road network created: {len(cities)} cities, "
        f"{state.road_network.graph.number_of_edges()} roads")

    # ── 4. Initialise database ────────────────────────────────────────
    os.makedirs(config.DATA_DIR, exist_ok=True)
    db_path   = os.path.join(config.DATA_DIR, f"{region_name}.db")
    state.db  = Database(db_path)

    # ── 5. Wire up all 7 services ─────────────────────────────────────
    discovery_svc    = DiscoveryService(state)
    region_svc       = RegionService(state)
    booking_svc      = BookingService(state)
    coordinator_svc  = CoordinatorService(state)
    health_svc       = HealthMonitorService(state)
    replication_svc  = ReplicationService(state)
    gateway_svc      = GatewayService(state)

    # Inject service references into shared state
    state.booking_service     = booking_svc
    state.coordinator         = coordinator_svc
    state.replication_service = replication_svc
    state.gateway             = gateway_svc
    state.region_service      = region_svc

    # ── 6. Start Flask REST API ───────────────────────────────────────
    app         = create_flask_app(state)
    flask_thread = threading.Thread(
        target=start_flask, args=(app, api_port),
        daemon=True, name="flask-server",
    )
    flask_thread.start()
    time.sleep(0.8)     # wait for Flask to bind
    log("MAIN", f"✅ REST API listening on port {api_port}", "SUCCESS")

    # ── 7. Start background services ──────────────────────────────────
    discovery_svc.start()
    health_svc.start()
    replication_svc.start()

    # ── 8. Seed-node bootstrap (if provided) ─────────────────────────
    if seed_address:
        time.sleep(0.5)
        bootstrap_from_seed(state, seed_address)

    # ── 9. Print startup summary ──────────────────────────────────────
    time.sleep(0.5)
    print_startup_summary(state)

    # ── 10. Graceful shutdown on Ctrl-C ──────────────────────────────
    def _shutdown(sig, frame):
        log("MAIN", "\n⛔ Shutting down node…", "WARN")
        discovery_svc.stop()
        health_svc.stop()
        replication_svc.stop()
        state.db.log_event("NODE_SHUTDOWN", {"region": region_name})
        sys.exit(0)

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # ── 11. Interactive menu (blocks main thread) ─────────────────────
    time.sleep(0.3)
    run_menu(state)

    # Clean shutdown on menu exit
    _shutdown(None, None)


if __name__ == "__main__":
    main()
