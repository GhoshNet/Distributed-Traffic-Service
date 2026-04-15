#!/usr/bin/env python3
"""
simulate_problems.py — Interactive Distributed Systems Problem Simulator
Ported and adapted from Archive/simulation/problems.py for the DTS microservices stack.

Runs OUTSIDE the Docker stack and calls the services via the nginx gateway (port 8080).
Requires: requests, rich, tabulate
    pip install requests rich tabulate

Usage:
    python scripts/simulate_problems.py [--gateway http://localhost:8080]
    python scripts/simulate_problems.py --token <JWT>        # skip login prompt
"""

import argparse
import random
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from rich.console import Console
from rich.table import Table

console = Console()

# ─── Irish cities matching the conflict-service predefined routes ────────────
IRISH_ROUTES = [
    {
        "route_id": "dublin-galway",
        "origin": "Dublin", "destination": "Galway",
        "origin_lat": 53.3498, "origin_lng": -6.2603,
        "destination_lat": 53.2707, "destination_lng": -9.0568,
        "duration": 135,
    },
    {
        "route_id": "dublin-cork",
        "origin": "Dublin", "destination": "Cork",
        "origin_lat": 53.3498, "origin_lng": -6.2603,
        "destination_lat": 51.8985, "destination_lng": -8.4756,
        "duration": 150,
    },
    {
        "route_id": "dublin-belfast",
        "origin": "Dublin", "destination": "Belfast",
        "origin_lat": 53.3498, "origin_lng": -6.2603,
        "destination_lat": 54.5973, "destination_lng": -5.9301,
        "duration": 120,
    },
    {
        "route_id": "galway-limerick",
        "origin": "Galway", "destination": "Limerick",
        "origin_lat": 53.2707, "origin_lng": -9.0568,
        "destination_lat": 52.6638, "destination_lng": -8.6267,
        "duration": 60,
    },
    {
        "route_id": "limerick-cork",
        "origin": "Limerick", "destination": "Cork",
        "origin_lat": 52.6638, "origin_lng": -8.6267,
        "destination_lat": 51.8985, "destination_lng": -8.4756,
        "duration": 75,
    },
]


def future_departure(minutes_ahead: int = 60) -> str:
    return (datetime.utcnow() + timedelta(minutes=minutes_ahead)).isoformat()


def pick_route() -> dict:
    return random.choice(IRISH_ROUTES)


def pick_two_routes():
    if len(IRISH_ROUTES) < 2:
        return IRISH_ROUTES[0], IRISH_ROUTES[0]
    return random.sample(IRISH_ROUTES, 2)


# ─── Session / auth ──────────────────────────────────────────────────────────

class Session:
    def __init__(self, gateway: str, token: Optional[str] = None):
        self.gateway = gateway.rstrip("/")
        self.token = token

    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def get(self, path: str, **kwargs):
        return requests.get(self.gateway + path, headers=self.headers(), timeout=10, **kwargs)

    def post(self, path: str, body: Optional[dict] = None, **kwargs):
        return requests.post(
            self.gateway + path, json=body or {},
            headers=self.headers(), timeout=15, **kwargs
        )

    def login(self, email: str, password: str) -> bool:
        try:
            r = requests.post(
                self.gateway + "/api/users/login",
                json={"email": email, "password": password},
                timeout=10,
            )
            if r.ok:
                self.token = r.json()["access_token"]
                return True
            console.print(f"[red]Login failed: {r.text}[/red]")
            return False
        except Exception as exc:
            console.print(f"[red]Login error: {exc}[/red]")
            return False


# ─── Status helpers ──────────────────────────────────────────────────────────

def print_node_health(session: Session):
    try:
        r = session.get("/health/nodes")
        if not r.ok:
            console.print("[red]  Could not fetch node health[/red]")
            return
        data = r.json()
        tbl = Table(title="Peer Node Health (Archive ALIVE/SUSPECT/DEAD model)", show_lines=True)
        tbl.add_column("Service")
        tbl.add_column("Status")
        tbl.add_column("Failures")
        tbl.add_column("Last Seen (s)")
        for name, info in data.get("peers", {}).items():
            status = info["status"]
            color = {"ALIVE": "green", "SUSPECT": "yellow", "DEAD": "red"}.get(status, "white")
            tbl.add_row(
                name,
                f"[{color}]{status}[/{color}]",
                str(info["consecutive_failures"]),
                str(info["last_seen_s_ago"]),
            )
        console.print(tbl)
        if data.get("local_only_mode"):
            console.print("[bold red]⚠  LOCAL ONLY MODE active[/bold red]")
    except Exception as exc:
        console.print(f"[red]  Health check error: {exc}[/red]")


def print_partitions(session: Session):
    try:
        r = session.get("/health/partitions")
        if not r.ok:
            return
        data = r.json()
        tbl = Table(title="Partition Manager State (CONNECTED/SUSPECTED/PARTITIONED)", show_lines=True)
        tbl.add_column("Dependency")
        tbl.add_column("State")
        tbl.add_column("Failures")
        tbl.add_column("Queued Ops")
        for name, info in data.get("dependencies", {}).items():
            state = info["state"]
            color = {"CONNECTED": "green", "SUSPECTED": "yellow", "PARTITIONED": "red", "MERGING": "cyan"}.get(state, "white")
            tbl.add_row(
                name,
                f"[{color}]{state}[/{color}]",
                str(info["consecutive_failures"]),
                str(info["queued_operations"]),
            )
        console.print(tbl)
    except Exception as exc:
        console.print(f"[red]  Partition check error: {exc}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
#  Problem 1 — Data Consistency Conflict
# ══════════════════════════════════════════════════════════════════════════════

def simulate_data_consistency(session: Session):
    console.rule("[bold red]SIMULATE: Data Consistency Conflict[/bold red]")
    console.print("  [dim]Two concurrent drivers book the same route at the same time.[/dim]")
    console.print("  [dim]Conflict detection (SELECT FOR UPDATE) ensures only one succeeds.[/dim]\n")

    route = pick_route()
    dep = future_departure(90)
    results = []
    lock = threading.Lock()

    def attempt(driver_id: str):
        try:
            r = session.post("/api/journeys/", {
                "origin": route["origin"], "destination": route["destination"],
                "origin_lat": route["origin_lat"], "origin_lng": route["origin_lng"],
                "destination_lat": route["destination_lat"], "destination_lng": route["destination_lng"],
                "departure_time": dep,
                "estimated_duration_minutes": route["duration"],
                "vehicle_registration": f"SIM-{driver_id[-1]}01",
                "vehicle_type": "CAR",
            })
            with lock:
                results.append((driver_id, r.json() if r.ok else {"status": "ERROR", "rejection_reason": r.text}))
        except Exception as exc:
            with lock:
                results.append((driver_id, {"error": str(exc)}))

    console.print(f"  Firing two simultaneous bookings: [cyan]{route['origin']} → {route['destination']}[/cyan] @ {dep[:16]}")
    t1 = threading.Thread(target=attempt, args=("DRIVER-A",))
    t2 = threading.Thread(target=attempt, args=("DRIVER-B",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    wins = 0
    for driver, res in results:
        ok = res.get("status") == "CONFIRMED"
        if ok:
            wins += 1
        icon = "✅" if ok else "❌"
        reason = res.get("rejection_reason") or res.get("error") or res.get("status", "")
        console.print(f"  {icon} [bold]{driver}[/bold]: {reason or 'CONFIRMED'}")

    if wins <= 1:
        console.print("[bold green]  ✅ Conflict detection working — at most 1 booking succeeded[/bold green]")
    else:
        console.print("[bold red]  ⚠  Multiple writes accepted — check conflict service![/bold red]")


# ══════════════════════════════════════════════════════════════════════════════
#  Problem 2 — Concurrent Booking Storm
# ══════════════════════════════════════════════════════════════════════════════

def simulate_concurrent_storm(session: Session):
    console.rule("[bold red]SIMULATE: Concurrent Booking Storm[/bold red]")
    try:
        n = int(input("  Number of concurrent booking requests (e.g. 15): ").strip() or "15")
    except ValueError:
        n = 15

    console.print(f"  [yellow]Firing {n} concurrent bookings to stress-test locking and capacity…[/yellow]\n")

    results = []
    lock = threading.Lock()

    def worker(i: int):
        route = pick_route()
        dep = future_departure(random.randint(10, 240))
        try:
            r = session.post("/api/journeys/", {
                "origin": route["origin"], "destination": route["destination"],
                "origin_lat": route["origin_lat"], "origin_lng": route["origin_lng"],
                "destination_lat": route["destination_lat"], "destination_lng": route["destination_lng"],
                "departure_time": dep,
                "estimated_duration_minutes": route["duration"],
                "vehicle_registration": f"STRM-{i:03d}",
                "vehicle_type": "CAR",
            })
            data = r.json() if r.ok else {}
            ok = data.get("status") == "CONFIRMED"
            with lock:
                results.append(ok)
                icon = "✅" if ok else "⚠️ "
                reason = data.get("rejection_reason") or ("CONFIRMED" if ok else "rejected")
                console.print(f"  [{i:03d}] {route['origin']}→{route['destination']}  {icon} {reason[:60]}")
        except Exception as exc:
            with lock:
                results.append(False)
                console.print(f"  [{i:03d}] [red]ERROR: {exc}[/red]")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    t0 = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - t0

    ok_count = sum(results)
    console.print(f"\n  [bold green]Storm complete: {ok_count}/{n} confirmed in {elapsed:.2f}s "
                  f"({n/elapsed:.1f} req/s)[/bold green]")
    console.print("  [dim]Serialisable transactions in conflict-service ensured no phantom bookings.[/dim]")


# ══════════════════════════════════════════════════════════════════════════════
#  Problem 3 — Two-Phase Commit Demo
# ══════════════════════════════════════════════════════════════════════════════

def simulate_two_phase_commit(session: Session):
    console.rule("[bold red]SIMULATE: Two-Phase Commit (2PC / TCC)[/bold red]")
    console.print("  [dim]Books a journey using the 2PC TCC coordinator instead of the Saga.[/dim]")
    console.print("  [dim]PREPARE → CONFIRM/CANCEL — watch the logs for TXN=… entries.[/dim]\n")

    route = pick_route()
    dep = future_departure(120)

    console.print(f"  Route: [cyan]{route['origin']} → {route['destination']}[/cyan]")
    console.print(f"  Sending POST /api/journeys/?mode=2pc …\n")

    try:
        r = requests.post(
            session.gateway + "/api/journeys/?mode=2pc",
            json={
                "origin": route["origin"], "destination": route["destination"],
                "origin_lat": route["origin_lat"], "origin_lng": route["origin_lng"],
                "destination_lat": route["destination_lat"], "destination_lng": route["destination_lng"],
                "departure_time": dep,
                "estimated_duration_minutes": route["duration"],
                "vehicle_registration": "TPC-DEMO-01",
                "vehicle_type": "CAR",
            },
            headers=session.headers(),
            timeout=30,
        )
        data = r.json()
        ok = data.get("status") == "CONFIRMED"
        icon = "✅" if ok else "❌"
        console.print(f"  {icon} Status: [bold]{data.get('status')}[/bold]")
        if data.get("rejection_reason"):
            console.print(f"  Reason: {data['rejection_reason']}")
        if ok:
            console.print(f"  Journey ID: {data.get('id')}")
            console.print("  [green]2PC COMMIT phase succeeded — capacity reserved + journey confirmed atomically.[/green]")
        else:
            console.print("  [yellow]2PC ABORT path — capacity released via compensating CANCEL call.[/yellow]")
    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
#  Problem 4 — Node Health / Failure Detection
# ══════════════════════════════════════════════════════════════════════════════

def simulate_node_failure_detection(session: Session):
    console.rule("[bold red]SIMULATE: Node Failure Detection[/bold red]")
    console.print("  [dim]This shows the ALIVE/SUSPECT/DEAD state machine from the Archive.[/dim]")
    console.print("  [dim]To trigger it: docker stop <service> and watch the health monitor transition.[/dim]\n")

    print_node_health(session)
    console.print()
    print_partitions(session)

    console.print("\n  [bold]To simulate a node failure:[/bold]")
    console.print("  [cyan]  docker stop distributed-traffic-service-conflict-service-1[/cyan]")
    console.print("  [dim]  After 3 probes (~30s): conflict-service → SUSPECT[/dim]")
    console.print("  [dim]  After 6 probes (~60s): conflict-service → DEAD[/dim]")
    console.print("  [dim]  On restart: conflict-service → ALIVE (recovery triggered)[/dim]")
    console.print("\n  [bold]To recover:[/bold]")
    console.print("  [cyan]  docker start distributed-traffic-service-conflict-service-1[/cyan]")


# ══════════════════════════════════════════════════════════════════════════════
#  Problem 5 — Circuit Breaker
# ══════════════════════════════════════════════════════════════════════════════

def simulate_circuit_breaker(session: Session):
    console.rule("[bold red]SIMULATE: Circuit Breaker[/bold red]")
    console.print("  [dim]Fires rapid bookings. After 3 failures the circuit breaker opens.[/dim]")
    console.print("  [dim]Subsequent requests fast-fail instead of waiting for timeout.[/dim]\n")

    route = pick_route()
    console.print(f"  Route: [cyan]{route['origin']} → {route['destination']}[/cyan]")
    console.print("  Firing 6 sequential booking requests…\n")

    for i in range(1, 7):
        dep = future_departure(random.randint(30, 180))
        t0 = time.time()
        try:
            r = session.post("/api/journeys/", {
                "origin": route["origin"], "destination": route["destination"],
                "origin_lat": route["origin_lat"], "origin_lng": route["origin_lng"],
                "destination_lat": route["destination_lat"], "destination_lng": route["destination_lng"],
                "departure_time": dep,
                "estimated_duration_minutes": route["duration"],
                "vehicle_registration": f"CB-TEST-{i:02d}",
                "vehicle_type": "CAR",
            })
            elapsed_ms = (time.time() - t0) * 1000
            data = r.json()
            status = data.get("status", "?")
            reason = data.get("rejection_reason") or ""
            icon = "✅" if status == "CONFIRMED" else "⚠️ "
            console.print(f"  [{i}] {icon} {status} — {elapsed_ms:.0f}ms  {reason[:50]}")
        except Exception as exc:
            elapsed_ms = (time.time() - t0) * 1000
            console.print(f"  [{i}] [red]ERROR — {elapsed_ms:.0f}ms: {exc}[/red]")
        time.sleep(0.2)

    console.print(
        "\n  [dim]If conflict-service was down, requests 1-3 hit timeout, "
        "then 4-6 fast-fail (circuit OPEN).[/dim]"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  Problem 6 — Graceful Degradation
# ══════════════════════════════════════════════════════════════════════════════

def simulate_graceful_degradation(session: Session):
    console.rule("[bold red]SIMULATE: Graceful Degradation[/bold red]")
    console.print("  [dim]When >50% of peer services are unreachable, the health monitor[/dim]")
    console.print("  [dim]enters LOCAL ONLY mode — mirrors Archive's graceful-degradation flag.[/dim]\n")

    print_node_health(session)

    console.print("\n  [bold]Current mode:[/bold]")
    try:
        r = session.get("/health/nodes")
        if r.ok:
            data = r.json()
            if data.get("local_only_mode"):
                console.print("  [bold red]🔴 LOCAL ONLY MODE is active[/bold red]")
                console.print("  [dim]Restart stopped services to return to global mode.[/dim]")
            else:
                console.print("  [bold green]🟢 GLOBAL mode — all/majority of peers reachable[/bold green]")
                console.print(
                    "  [dim]Stop ≥3 services to trigger LOCAL ONLY mode:[/dim]\n"
                    "  [cyan]  docker stop distributed-traffic-service-notification-service-1[/cyan]\n"
                    "  [cyan]  docker stop distributed-traffic-service-analytics-service-1[/cyan]\n"
                    "  [cyan]  docker stop distributed-traffic-service-enforcement-service-1[/cyan]"
                )
    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
#  Problem 7 — Transactional Outbox / At-Least-Once Delivery
# ══════════════════════════════════════════════════════════════════════════════

def simulate_outbox_delivery(session: Session):
    console.rule("[bold red]SIMULATE: Transactional Outbox / At-Least-Once Delivery[/bold red]")
    console.print("  [dim]Books a journey and then triggers the outbox drain to confirm[/dim]")
    console.print("  [dim]the event was published to RabbitMQ (transactional outbox pattern).[/dim]\n")

    route = pick_route()
    dep = future_departure(60)

    console.print(f"  Step 1 — booking [cyan]{route['origin']} → {route['destination']}[/cyan]")
    try:
        r = session.post("/api/journeys/", {
            "origin": route["origin"], "destination": route["destination"],
            "origin_lat": route["origin_lat"], "origin_lng": route["origin_lng"],
            "destination_lat": route["destination_lat"], "destination_lng": route["destination_lng"],
            "departure_time": dep,
            "estimated_duration_minutes": route["duration"],
            "vehicle_registration": "OUTBOX-DEMO",
            "vehicle_type": "CAR",
        })
        data = r.json()
        console.print(f"  Journey status: [bold]{data.get('status')}[/bold]  id={data.get('id')}")

        console.print("\n  Step 2 — forcing outbox drain via POST /admin/recovery/drain-outbox")
        dr = session.post("/admin/recovery/drain-outbox")
        if dr.ok:
            ddata = dr.json()
            console.print(f"  [green]Drained {ddata.get('events_drained', 0)} outbox event(s) to RabbitMQ[/green]")
        else:
            console.print(f"  [yellow]Drain returned {dr.status_code}: {dr.text[:80]}[/yellow]")
    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")

    console.print("\n  [dim]The outbox guarantees at-least-once delivery: even if RabbitMQ was[/dim]")
    console.print("  [dim]temporarily down, the event is persisted and will be re-sent on next drain.[/dim]")


# ══════════════════════════════════════════════════════════════════════════════
#  Main menu
# ══════════════════════════════════════════════════════════════════════════════

def run_menu(session: Session):
    while True:
        console.rule()
        try:
            r = session.get("/health/nodes")
            hn = r.json() if r.ok else {}
            local_only = hn.get("local_only_mode", False)
        except Exception:
            hn = {}
            local_only = False

        alive = sum(
            1 for p in hn.get("peers", {}).values() if p["status"] == "ALIVE"
        )
        total = len(hn.get("peers", {}))

        console.print(f"\n[bold bright_white]  ═══ DTS Simulation Terminal ═══[/bold bright_white]")
        console.print(
            f"  [dim]Gateway: {session.gateway}  |  "
            f"Peers: {alive}/{total} alive  |  "
            f"{'🔴 LOCAL-ONLY' if local_only else '🟢 GLOBAL'}[/dim]\n"
        )

        console.print("  [bold cyan]── Standard Checks ──[/bold cyan]")
        console.print("  [1]  Show peer node health   (ALIVE/SUSPECT/DEAD)")
        console.print("  [2]  Show partition state    (CONNECTED/PARTITIONED)")

        console.print("\n  [bold red]── Simulate Distributed Problems ──[/bold red]")
        console.print("  [3]  🔀 Data Consistency     — concurrent conflict")
        console.print("  [4]  🌪️  Concurrent Storm    — booking flood")
        console.print("  [5]  🔄 Two-Phase Commit     — 2PC / TCC demo")
        console.print("  [6]  💀 Failure Detection   — ALIVE/SUSPECT/DEAD walkthrough")
        console.print("  [7]  ⚡ Circuit Breaker      — fast-fail demo")
        console.print("  [8]  🔴 Graceful Degradation — LOCAL ONLY mode")
        console.print("  [9]  📦 Transactional Outbox — at-least-once delivery")

        console.print("\n  [0]  Exit\n")

        try:
            choice = input("  Select: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == "0":
            break
        elif choice == "1":
            print_node_health(session)
        elif choice == "2":
            print_partitions(session)
        elif choice == "3":
            simulate_data_consistency(session)
        elif choice == "4":
            simulate_concurrent_storm(session)
        elif choice == "5":
            simulate_two_phase_commit(session)
        elif choice == "6":
            simulate_node_failure_detection(session)
        elif choice == "7":
            simulate_circuit_breaker(session)
        elif choice == "8":
            simulate_graceful_degradation(session)
        elif choice == "9":
            simulate_outbox_delivery(session)
        else:
            console.print("[red]  Unknown option.[/red]")


def main():
    parser = argparse.ArgumentParser(description="DTS Distributed Systems Simulator")
    parser.add_argument(
        "--gateway", default="http://localhost:8080",
        help="API gateway URL (default: http://localhost:8080)"
    )
    parser.add_argument("--token", default=None, help="JWT bearer token (skips login)")
    args = parser.parse_args()

    console.print(f"\n[bold bright_green]  DTS Distributed Systems Simulator[/bold bright_green]")
    console.print(f"  Gateway: [cyan]{args.gateway}[/cyan]\n")

    session = Session(gateway=args.gateway, token=args.token)

    if not session.token:
        console.print("  [dim]Enter credentials to authenticate (or Ctrl+C to exit)[/dim]")
        try:
            email = input("  Email    : ").strip()
            password = input("  Password : ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]  Exiting.[/yellow]")
            return

        if not session.login(email, password):
            console.print("[red]  Authentication failed. Exiting.[/red]")
            return
        console.print("[green]  Authenticated successfully.[/green]\n")

    run_menu(session)
    console.print("\n[dim]  Goodbye.[/dim]\n")


if __name__ == "__main__":
    main()
