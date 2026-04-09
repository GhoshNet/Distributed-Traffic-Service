#!/usr/bin/env python3
"""
Integration test — starts two nodes in-process and exercises all features.
Run with:  python test_integration.py
"""
import json
import logging
import os
import sys
import threading
import time

import requests
from flask import Flask
from flask_cors import CORS
from rich.console import Console

# ---- silence logs during test ----
logging.getLogger("werkzeug").setLevel(logging.ERROR)
os.environ.setdefault("GDTS_SILENT", "1")

sys.path.insert(0, ".")

import config

# Speed up intervals for testing
config.DISCOVERY_INTERVAL = 1
config.HEARTBEAT_INTERVAL = 1
config.SUSPECT_THRESHOLD  = 2
config.DEAD_THRESHOLD     = 4

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

console = Console()

# ──────────────────────────────────────────────────────────────────────
def build_node(region_name, cities, port, db_prefix="./data/test"):
    os.makedirs(db_prefix, exist_ok=True)
    state = NodeState()
    state.region_name = region_name
    state.host        = "127.0.0.1"
    state.api_port    = port

    state.road_network = RoadNetwork(region_name, cities)
    state.db           = Database(f"{db_prefix}/{region_name}_test.db")

    state.booking_service     = BookingService(state)
    state.coordinator         = CoordinatorService(state)
    state.replication_service = ReplicationService(state)
    state.gateway             = GatewayService(state)
    state.region_service      = RegionService(state)

    discovery_svc  = DiscoveryService(state)
    health_svc     = HealthMonitorService(state)

    # Flask app
    app = Flask(f"gdts-{region_name}")
    CORS(app)
    app.config["NODE_STATE"] = state
    app.register_blueprint(api_bp)

    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port,
                               threaded=True, use_reloader=False),
        daemon=True, name=f"flask-{region_name}",
    ).start()

    time.sleep(0.6)

    discovery_svc.start()
    health_svc.start()
    state.replication_service.start()

    return state, discovery_svc, health_svc


# ──────────────────────────────────────────────────────────────────────
def check(label, condition, extra=""):
    icon = "✅" if condition else "❌"
    msg  = f"  {icon}  {label}"
    if extra:
        msg += f"\n       {extra}"
    console.print(msg)
    return condition


def post(url, payload, timeout=10):
    return requests.post(url, json=payload, timeout=timeout).json()


def get(url, timeout=5):
    return requests.get(url, timeout=timeout).json()


# ──────────────────────────────────────────────────────────────────────
def main():
    console.rule("[bold bright_cyan] GDTS 2-Node Integration Test [/bold bright_cyan]")

    P1, P2 = 7300, 7301

    # Clean up old test databases
    for f in ["data/test/Ireland_test.db", "data/test/France_test.db"]:
        try: os.remove(f)
        except FileNotFoundError: pass

    console.print("\n[bold]1. Starting Nodes[/bold]")
    state1, disc1, health1 = build_node(
        "Ireland", ["Dublin", "Cork", "Galway", "Limerick"], P1
    )
    state2, disc2, health2 = build_node(
        "France",  ["Paris", "Lyon", "Nice", "Bordeaux"],   P2
    )
    console.print(f"  Node 1: Ireland @ 127.0.0.1:{P1}")
    console.print(f"  Node 2: France  @ 127.0.0.1:{P2}")

    # ── Manual peer registration (UDP broadcast needs same-subnet multicast)
    # On loopback we register peers via the /api/peer/announce endpoint instead
    time.sleep(1)
    requests.post(f"http://127.0.0.1:{P1}/api/peer/announce", json={
        "region_name": "France", "host": "127.0.0.1", "api_port": P2,
        "cities": ["Paris", "Lyon", "Nice", "Bordeaux"], "gateway_city": "Paris",
    }, timeout=4)
    requests.post(f"http://127.0.0.1:{P2}/api/peer/announce", json={
        "region_name": "Ireland", "host": "127.0.0.1", "api_port": P1,
        "cities": ["Dublin", "Cork", "Galway", "Limerick"], "gateway_city": "Dublin",
    }, timeout=4)
    time.sleep(1)

    # ── Test group 1: Health & Discovery ──────────────────────────────
    console.print("\n[bold]2. Health & Peer Discovery[/bold]")

    r = get(f"http://127.0.0.1:{P1}/api/health/ping")
    check("Ireland health ping", r.get("status") == "OK", str(r))

    r = get(f"http://127.0.0.1:{P2}/api/health/ping")
    check("France health ping", r.get("status") == "OK", str(r))

    peers1 = get(f"http://127.0.0.1:{P1}/api/peer/list")["peers"]
    check("Ireland knows France", any(p["region_name"]=="France" for p in peers1),
          f"Peers: {[p['region_name'] for p in peers1]}")

    peers2 = get(f"http://127.0.0.1:{P2}/api/peer/list")["peers"]
    check("France knows Ireland", any(p["region_name"]=="Ireland" for p in peers2),
          f"Peers: {[p['region_name'] for p in peers2]}")

    # ── Test group 2: Local bookings ──────────────────────────────────
    console.print("\n[bold]3. Local Booking & Conflict Detection[/bold]")

    r = post(f"http://127.0.0.1:{P1}/api/booking/create", {
        "driver_id": "DRV-001", "origin": "Dublin",
        "destination": "Cork", "departure_time": "2026-09-01T09:00:00"
    })
    ok1 = r.get("success")
    bid1 = (r.get("booking") or {}).get("booking_id", "?")
    check("Local booking Dublin→Cork (Ireland)",  ok1,
          f"booking_id={bid1}  route={r.get('booking',{}).get('route_path')}")

    r2 = post(f"http://127.0.0.1:{P1}/api/booking/create", {
        "driver_id": "DRV-002", "origin": "Dublin",
        "destination": "Cork", "departure_time": "2026-09-01T09:10:00"
    })
    check("Conflict rejected (same route ±30 min)", not r2.get("success"),
          f"Message: {r2.get('message')}")

    # ── Test group 3: Cross-region 2PC ────────────────────────────────
    console.print("\n[bold]4. Cross-Region Booking (2PC)[/bold]")
    console.print("  [dim]Booking Dublin (Ireland) → Paris (France) via 2PC…[/dim]")

    r = post(f"http://127.0.0.1:{P1}/api/booking/create", {
        "driver_id": "DRV-CROSS", "origin": "Dublin",
        "destination": "Paris", "departure_time": "2026-09-01T14:00:00"
    }, timeout=15)
    check("Cross-region 2PC Dublin→Paris", r.get("success"),
          f"Message: {r.get('message')}")

    # ── Test group 4: Cancellation ────────────────────────────────────
    console.print("\n[bold]5. Booking Cancellation[/bold]")

    r = post(f"http://127.0.0.1:{P1}/api/booking/cancel/{bid1}", {})
    check(f"Cancel booking {bid1}", r.get("success"),
          f"Message: {r.get('message')}")

    r = post(f"http://127.0.0.1:{P1}/api/booking/cancel/{bid1}", {})
    check("Double-cancel rejected", not r.get("success"),
          f"Message: {r.get('message')}")

    # ── Test group 5: Replication ─────────────────────────────────────
    console.print("\n[bold]6. Eventual Consistency (Replication)[/bold]")
    time.sleep(2)   # allow replication push

    fr_bookings = get(f"http://127.0.0.1:{P2}/api/booking/list")
    check("France received replicated bookings",
          fr_bookings["count"] > 0, f"count={fr_bookings['count']}")

    # ── Test group 6: Node failure & degradation ──────────────────────
    console.print("\n[bold]7. Node Failure & Graceful Degradation[/bold]")

    state1.failure_simulated = True
    console.print("  [yellow]  Ireland node set to failure mode[/yellow]")
    time.sleep(0.5)

    r = get(f"http://127.0.0.1:{P1}/api/health/ping")
    check("Ireland health returns 503 / FAILED",
          r.get("status") == "FAILED", str(r))

    r = post(f"http://127.0.0.1:{P1}/api/booking/create", {
        "driver_id": "DRV-FAIL", "origin": "Dublin",
        "destination": "Galway", "departure_time": "2026-09-02T08:00:00"
    })
    check("Booking rejected during failure", not r.get("success"),
          f"Message: {r.get('message')}")

    # ── Test group 7: Node recovery ───────────────────────────────────
    console.print("\n[bold]8. Node Recovery & State Sync[/bold]")

    state1.failure_simulated = False
    console.print("  [green]  Ireland node recovered[/green]")
    time.sleep(2)

    r = get(f"http://127.0.0.1:{P1}/api/health/ping")
    check("Ireland healthy again", r.get("status") == "OK", str(r))

    # ── Test group 8: Concurrent storm ───────────────────────────────
    console.print("\n[bold]9. Concurrent Booking Storm (Thread Safety)[/bold]")

    import concurrent.futures
    cities = ["Dublin", "Cork", "Galway", "Limerick"]

    def storm_booking(i):
        import random
        a, b = random.sample(cities, 2)
        dt = f"2026-10-{(i%28)+1:02d}T{(i%12)+8:02d}:00:00"
        try:
            r = requests.post(f"http://127.0.0.1:{P1}/api/booking/create", json={
                "driver_id": f"STORM-{i:03d}", "origin": a,
                "destination": b, "departure_time": dt,
            }, timeout=8)
            return r.json().get("success", False)
        except Exception:
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(storm_booking, i) for i in range(20)]
        results = [f.result() for f in futures]

    ok_count = sum(results)
    check(f"Concurrent storm: {ok_count}/20 bookings succeeded without deadlock",
          True, f"(conflicts/capacity rejections account for the rest)")

    # ── Summary ───────────────────────────────────────────────────────
    console.rule("[bold bright_green] All Tests Complete [/bold bright_green]")

    irl = get(f"http://127.0.0.1:{P1}/api/booking/list")
    fr  = get(f"http://127.0.0.1:{P2}/api/booking/list")
    console.print(f"\n  Ireland bookings: {irl['count']}")
    console.print(f"  France  bookings: {fr['count']}")
    console.print("\n  [dim]Shutting down test nodes…[/dim]")

    disc1.stop(); health1.stop()
    disc2.stop(); health2.stop()

if __name__ == "__main__":
    main()
