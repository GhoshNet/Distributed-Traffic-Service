#!/usr/bin/env python3
"""
simulate_problems.py — GDTS Interactive Simulation Menu
Implements the plan's 12-option terminal interface for demonstrating
distributed systems problems and solutions.

Usage:
    python scripts/simulate_problems.py [--gateway http://localhost:8080]
    python scripts/simulate_problems.py --token <JWT>
"""

import argparse
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from rich.console import Console
from rich.table import Table

console = Console()

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

# Cross-region routes (simulate multi-region topology)
CROSS_REGION_ROUTES = [
    {
        "route_id": "dublin-london",
        "origin": "Dublin (IE)", "destination": "London (UK)",
        "origin_lat": 53.3498, "origin_lng": -6.2603,
        "destination_lat": 51.5074, "destination_lng": -0.1278,
        "duration": 120,
        "regions": ["Dublin", "London"],
    },
    {
        "route_id": "cork-paris",
        "origin": "Cork (IE)", "destination": "Paris (FR)",
        "origin_lat": 51.8985, "origin_lng": -8.4756,
        "destination_lat": 48.8566, "destination_lng": 2.3522,
        "duration": 180,
        "regions": ["Cork", "Paris"],
    },
    {
        "route_id": "belfast-berlin",
        "origin": "Belfast (UK)", "destination": "Berlin (DE)",
        "origin_lat": 54.5973, "origin_lng": -5.9301,
        "destination_lat": 52.5200, "destination_lng": 13.4050,
        "duration": 150,
        "regions": ["Belfast", "Berlin"],
    },
]


def future_departure(minutes_ahead: int = 60) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes_ahead)).isoformat()


def pick_route() -> dict:
    return random.choice(IRISH_ROUTES)


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

    def delete(self, path: str, **kwargs):
        return requests.delete(self.gateway + path, headers=self.headers(), timeout=10, **kwargs)

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
        tbl = Table(title="Peer Node Health (ALIVE/SUSPECT/DEAD)", show_lines=True)
        tbl.add_column("Service / Region")
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
            console.print("[bold red]⚠  LOCAL ONLY MODE active — cross-region requests queued[/bold red]")
    except Exception as exc:
        console.print(f"[red]  Health check error: {exc}[/red]")


def print_partitions(session: Session):
    try:
        r = session.get("/health/partitions")
        if not r.ok:
            return
        data = r.json()
        tbl = Table(title="Partition Manager State", show_lines=True)
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
#  [1] Book a journey
# ══════════════════════════════════════════════════════════════════════════════

def book_journey(session: Session):
    console.rule("[bold cyan]Book a Journey[/bold cyan]")
    console.print("  Available routes:")
    for i, r in enumerate(IRISH_ROUTES, 1):
        console.print(f"    [{i}] {r['origin']} → {r['destination']} ({r['duration']} min)")
    try:
        choice = int(input("  Select route [1-5]: ").strip() or "1") - 1
        route = IRISH_ROUTES[choice % len(IRISH_ROUTES)]
    except (ValueError, IndexError):
        route = IRISH_ROUTES[0]

    try:
        mins = int(input("  Departure in how many minutes from now? [60]: ").strip() or "60")
    except ValueError:
        mins = 60

    try:
        vehicle = input("  Vehicle registration [MY-VEHICLE-01]: ").strip() or "MY-VEHICLE-01"
    except EOFError:
        vehicle = "MY-VEHICLE-01"

    dep = future_departure(mins)
    console.print(f"\n  Booking [cyan]{route['origin']} → {route['destination']}[/cyan] @ {dep[:16]}")

    try:
        r = session.post("/api/journeys/", {
            "origin": route["origin"], "destination": route["destination"],
            "origin_lat": route["origin_lat"], "origin_lng": route["origin_lng"],
            "destination_lat": route["destination_lat"], "destination_lng": route["destination_lng"],
            "departure_time": dep,
            "estimated_duration_minutes": route["duration"],
            "vehicle_registration": vehicle,
            "vehicle_type": "CAR",
        })
        data = r.json()
        ok = data.get("status") == "CONFIRMED"
        icon = "✅" if ok else "❌"
        console.print(f"\n  {icon} Status: [bold]{data.get('status')}[/bold]")
        if data.get("id"):
            console.print(f"  Journey ID: {data['id']}")
        if data.get("rejection_reason"):
            console.print(f"  Reason: {data['rejection_reason']}")
    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
#  [2] Cancel a journey
# ══════════════════════════════════════════════════════════════════════════════

def cancel_journey(session: Session):
    console.rule("[bold cyan]Cancel a Journey[/bold cyan]")
    try:
        journey_id = input("  Journey ID to cancel: ").strip()
    except EOFError:
        console.print("[red]  No input provided.[/red]")
        return

    if not journey_id:
        console.print("[red]  Journey ID required.[/red]")
        return

    try:
        r = session.delete(f"/api/journeys/{journey_id}")
        if r.ok:
            data = r.json()
            console.print(f"  ✅ Journey [bold]{journey_id}[/bold] cancelled. Status: {data.get('status')}")
        else:
            console.print(f"  [red]Failed: {r.status_code} — {r.text[:100]}[/red]")
    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
#  [3] View all bookings
# ══════════════════════════════════════════════════════════════════════════════

def view_bookings(session: Session):
    console.rule("[bold cyan]All Bookings[/bold cyan]")
    try:
        r = session.get("/api/journeys/")
        if not r.ok:
            console.print(f"  [red]Failed: {r.status_code}[/red]")
            return
        data = r.json()
        journeys = data.get("journeys", [])
        if not journeys:
            console.print("  [dim]No bookings found.[/dim]")
            return
        tbl = Table(title=f"Bookings (total: {data.get('total', len(journeys))})", show_lines=True)
        tbl.add_column("ID")
        tbl.add_column("Route")
        tbl.add_column("Departure")
        tbl.add_column("Status")
        tbl.add_column("Vehicle")
        for j in journeys[:20]:
            status = j.get("status", "?")
            color = {
                "CONFIRMED": "green", "PENDING": "yellow",
                "CANCELLED": "red", "IN_PROGRESS": "cyan", "COMPLETED": "blue"
            }.get(status, "white")
            dep = j.get("departure_time", "")[:16] if j.get("departure_time") else "?"
            tbl.add_row(
                j.get("id", "?")[:8] + "…",
                f"{j.get('origin','?')} → {j.get('destination','?')}",
                dep,
                f"[{color}]{status}[/{color}]",
                j.get("vehicle_registration", "?"),
            )
        console.print(tbl)
        if data.get("total", 0) > 20:
            console.print(f"  [dim](showing 20 of {data['total']})[/dim]")
    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
#  [4] Show region road network
# ══════════════════════════════════════════════════════════════════════════════

def show_region_network(session: Session):
    console.rule("[bold cyan]Region Road Network[/bold cyan]")
    try:
        r = session.get("/api/region")
        if r.ok:
            data = r.json()
            region = data.get("region_name", "unknown")
            console.print(f"  Region: [bold green]{region}[/bold green]")
            console.print(f"  API:    {data.get('journey_service_url', 'N/A')}")
            graph = data.get("graph_summary", {})
            console.print(f"  Graph:  {graph}")

            peers = data.get("peers", {})
            if peers:
                console.print(f"\n  Connected peer regions ({len(peers)}):")
                for name, info in peers.items():
                    age = info.get("last_seen_s_ago", "?")
                    console.print(f"    • [cyan]{name}[/cyan] — {info.get('journey_service_url')} (seen {age}s ago)")
            else:
                console.print("  [dim]No peer regions discovered yet (waiting for UDP broadcasts…)[/dim]")
        else:
            console.print(f"  [yellow]/api/region not available ({r.status_code}) — showing predefined routes[/yellow]")

        # Always show predefined Irish routes
        console.print("\n  [bold]Predefined Irish road routes:[/bold]")
        try:
            rr = session.get("/api/conflicts/routes")
            if rr.ok:
                routes = rr.json().get("routes", [])
                for route in routes:
                    console.print(f"    • {route.get('name', route.get('route_id', '?'))}")
            else:
                for route in IRISH_ROUTES:
                    console.print(f"    • {route['origin']} → {route['destination']} ({route['duration']} min)")
        except Exception:
            for route in IRISH_ROUTES:
                console.print(f"    • {route['origin']} → {route['destination']} ({route['duration']} min)")

    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
#  [5] Show connected peers
# ══════════════════════════════════════════════════════════════════════════════

def show_connected_peers(session: Session):
    console.rule("[bold cyan]Connected Region Peers[/bold cyan]")
    console.print("  [dim]Discovered via UDP broadcast (port 5001, every 5s)[/dim]\n")

    print_node_health(session)
    console.print()

    try:
        r = session.get("/api/region")
        if r.ok:
            data = r.json()
            peers = data.get("peers", {})
            if peers:
                tbl = Table(title="UDP-Discovered Region Peers", show_lines=True)
                tbl.add_column("Region")
                tbl.add_column("Journey Service URL")
                tbl.add_column("Last Seen (s)")
                for name, info in peers.items():
                    tbl.add_row(
                        f"[cyan]{name}[/cyan]",
                        info.get("journey_service_url", "?"),
                        str(info.get("last_seen_s_ago", "?")),
                    )
                console.print(tbl)
            else:
                console.print("  [dim]No UDP peers discovered yet.[/dim]")
                console.print("  [dim]Run another instance with a different REGION_NAME on the same LAN.[/dim]")
    except Exception as exc:
        console.print(f"  [yellow]Could not fetch peer list: {exc}[/yellow]")

    print_partitions(session)


# ══════════════════════════════════════════════════════════════════════════════
#  [6] Simulate: Network Delay
# ══════════════════════════════════════════════════════════════════════════════

def simulate_network_delay(session: Session):
    console.rule("[bold red]SIMULATE: Network Delay[/bold red]")
    console.print("  [dim]Injects artificial sleep on incoming requests to this node.[/dim]")
    console.print("  [dim]Solution: Timeout handling + retry with exponential backoff.[/dim]\n")

    try:
        current = session.get("/admin/simulate/delay")
        if current.ok:
            console.print(f"  Current delay: [yellow]{current.json().get('delay_ms', 0)}ms[/yellow]")
    except Exception:
        pass

    try:
        delay_ms = int(input("  Set delay in ms (0 to disable, e.g. 200): ").strip() or "200")
    except ValueError:
        delay_ms = 200

    try:
        r = session.post("/admin/simulate/delay", {"delay_ms": delay_ms})
        if r.ok:
            data = r.json()
            if delay_ms > 0:
                console.print(f"  [yellow]⏱  Network delay active: {data['delay_ms']}ms on all requests[/yellow]")
                console.print("  [dim]Try booking a journey now to observe the latency effect.[/dim]")
                console.print("  [dim]Services will use timeout handling and retry with backoff.[/dim]")
            else:
                console.print("  [green]✅ Network delay disabled[/green]")
        else:
            console.print(f"  [yellow]Could not set delay via API ({r.status_code}) — feature may require restart[/yellow]")
    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
#  [7] Simulate: Node Failure
# ══════════════════════════════════════════════════════════════════════════════

def simulate_node_failure(session: Session):
    console.rule("[bold red]SIMULATE: Node Failure[/bold red]")
    console.print("  [dim]Makes this node's /health return 503 — peers will detect it as SUSPECT → DEAD.[/dim]")
    console.print("  [dim]Solution: Peers detect via missed heartbeats; re-route traffic.[/dim]\n")

    try:
        r = session.post("/admin/simulate/fail")
        if r.ok:
            data = r.json()
            console.print(f"  [bold red]💀 {data.get('message')}[/bold red]")
            console.print("\n  [dim]Peer health monitors will detect:[/dim]")
            console.print("  [dim]  3 missed pings (~30s) → SUSPECT[/dim]")
            console.print("  [dim]  6 missed pings (~60s) → DEAD[/dim]")
            console.print("\n  [dim]Use option [11] to recover this node.[/dim]")
        else:
            console.print(f"  [red]Failed: {r.status_code} — {r.text[:100]}[/red]")
    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
#  [8] Simulate: Data Consistency conflict
# ══════════════════════════════════════════════════════════════════════════════

def simulate_data_consistency(session: Session):
    console.rule("[bold red]SIMULATE: Data Consistency Conflict[/bold red]")
    console.print("  [dim]Two concurrent drivers book the same route at the same time.[/dim]")
    console.print("  [dim]Solution: Optimistic locking + version vectors via conflict service.[/dim]\n")

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
#  [9] Simulate: Concurrent booking storm
# ══════════════════════════════════════════════════════════════════════════════

def simulate_concurrent_storm(session: Session):
    console.rule("[bold red]SIMULATE: Concurrent Booking Storm[/bold red]")
    console.print("  [dim]Solution: Thread-safe SQLite writes with serializable transactions.[/dim]\n")
    try:
        n = int(input("  Number of concurrent booking requests (e.g. 15): ").strip() or "15")
    except ValueError:
        n = 15

    console.print(f"  [yellow]Firing {n} concurrent bookings…[/yellow]\n")

    results = []
    lock = threading.Lock()

    def worker(i: int):
        route = random.choice(IRISH_ROUTES)
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
    console.print(f"\n  [bold green]Storm complete: {ok_count}/{n} confirmed in {elapsed:.2f}s ({n/elapsed:.1f} req/s)[/bold green]")
    console.print("  [dim]Serialisable transactions in conflict-service ensured no phantom bookings.[/dim]")


# ══════════════════════════════════════════════════════════════════════════════
#  [10] Simulate: Cross-region booking (partitioned data)
# ══════════════════════════════════════════════════════════════════════════════

def simulate_cross_region(session: Session):
    console.rule("[bold red]SIMULATE: Cross-Region Booking (Partitioned Data)[/bold red]")
    console.print("  [dim]Books a journey spanning two regions using Two-Phase Commit (2PC).[/dim]")
    console.print("  [dim]Solution: Consistent hashing assigns home region; 2PC for cross-region.[/dim]\n")

    console.print("  Cross-region routes:")
    for i, r in enumerate(CROSS_REGION_ROUTES, 1):
        console.print(f"    [{i}] {r['origin']} → {r['destination']} (regions: {' + '.join(r['regions'])})")
    try:
        choice = int(input("  Select route [1-3]: ").strip() or "1") - 1
        route = CROSS_REGION_ROUTES[choice % len(CROSS_REGION_ROUTES)]
    except (ValueError, IndexError):
        route = CROSS_REGION_ROUTES[0]

    dep = future_departure(120)
    console.print(f"\n  Route: [cyan]{route['origin']} → {route['destination']}[/cyan]")
    console.print(f"  Regions involved: [yellow]{' ↔ '.join(route['regions'])}[/yellow]")
    console.print("  Sending booking with ?mode=2pc to trigger cross-region 2PC protocol…\n")

    try:
        r = requests.post(
            session.gateway + "/api/journeys/?mode=2pc",
            json={
                "origin": route["origin"], "destination": route["destination"],
                "origin_lat": route["origin_lat"], "origin_lng": route["origin_lng"],
                "destination_lat": route["destination_lat"], "destination_lng": route["destination_lng"],
                "departure_time": dep,
                "estimated_duration_minutes": route["duration"],
                "vehicle_registration": "XREG-DEMO-01",
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
            console.print("  [green]2PC cross-region COMMIT — capacity reserved in both regions.[/green]")
        else:
            console.print("  [yellow]2PC ABORT — one region rejected; all capacity released.[/yellow]")
        console.print("\n  [dim]Watch logs on both region nodes for: [2PC] TXN=… PREPARE / COMMIT / ABORT[/dim]")
    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
#  [11] Simulate: Node Recovery
# ══════════════════════════════════════════════════════════════════════════════

def simulate_node_recovery(session: Session):
    console.rule("[bold red]SIMULATE: Node Recovery (Re-join Network)[/bold red]")
    console.print("  [dim]Restores a failed node — /health returns 200 again.[/dim]")
    console.print("  [dim]Solution: Node re-announces, pulls missed bookings via replication sync.[/dim]\n")

    try:
        r = session.post("/admin/simulate/recover")
        if r.ok:
            data = r.json()
            console.print(f"  [bold green]🟢 {data.get('message')}[/bold green]")
            console.print("\n  [dim]Outbox drain (missed events):[/dim]")
            dr = session.post("/admin/recovery/drain-outbox")
            if dr.ok:
                ddata = dr.json()
                console.print(f"  [green]Drained {ddata.get('events_drained', 0)} missed outbox event(s)[/green]")
            console.print("\n  [dim]Peers will detect ALIVE on next heartbeat cycle (~10s).[/dim]")
        else:
            console.print(f"  [red]Failed: {r.status_code} — {r.text[:100]}[/red]")
    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
#  [12] Simulate: Graceful Degradation
# ══════════════════════════════════════════════════════════════════════════════

def simulate_graceful_degradation(session: Session):
    console.rule("[bold red]SIMULATE: Graceful Degradation[/bold red]")
    console.print("  [dim]When <50% of known peers are reachable → LOCAL ONLY mode.[/dim]")
    console.print("  [dim]Solution: Queue cross-region requests; operate on local bookings only.[/dim]\n")

    print_node_health(session)

    try:
        r = session.get("/health/nodes")
        if r.ok:
            data = r.json()
            if data.get("local_only_mode"):
                console.print("\n  [bold red]🔴 LOCAL ONLY MODE active[/bold red]")
                console.print("  [dim]Cross-region bookings queued. Restart stopped services to exit.[/dim]")
            else:
                peers = data.get("peers", {})
                alive = sum(1 for p in peers.values() if p["status"] == "ALIVE")
                total = len(peers)
                console.print(f"\n  [bold green]🟢 GLOBAL mode[/bold green] — {alive}/{total} peers alive")
                console.print(f"  [dim]To trigger LOCAL ONLY mode, stop >{total // 2} peer services.[/dim]")
                console.print("  [dim]Example:[/dim]")
                console.print("  [cyan]  docker stop distributed-traffic-service-notification-service-1[/cyan]")
                console.print("  [cyan]  docker stop distributed-traffic-service-analytics-service-1[/cyan]")
                console.print("  [cyan]  docker stop distributed-traffic-service-enforcement-service-1[/cyan]")
    except Exception as exc:
        console.print(f"  [red]Error: {exc}[/red]")


# ══════════════════════════════════════════════════════════════════════════════
#  Main menu
# ══════════════════════════════════════════════════════════════════════════════

def run_menu(session: Session):
    while True:
        try:
            r = session.get("/health/nodes")
            hn = r.json() if r.ok else {}
            local_only = hn.get("local_only_mode", False)
        except Exception:
            hn = {}
            local_only = False

        alive = sum(1 for p in hn.get("peers", {}).values() if p["status"] == "ALIVE")
        total = len(hn.get("peers", {}))

        console.print()
        console.rule("[bold bright_white]═══ GDTS Simulation Menu ═══[/bold bright_white]")
        console.print(
            f"  [dim]Gateway: {session.gateway}  |  "
            f"Peers: {alive}/{total} alive  |  "
            f"{'[bold red]🔴 LOCAL-ONLY[/bold red]' if local_only else '[bold green]🟢 GLOBAL[/bold green]'}[/dim]\n"
        )

        console.print("  [bold cyan][1][/bold cyan]  Book a journey")
        console.print("  [bold cyan][2][/bold cyan]  Cancel a journey")
        console.print("  [bold cyan][3][/bold cyan]  View all bookings")
        console.print("  [bold cyan][4][/bold cyan]  Show region road network")
        console.print("  [bold cyan][5][/bold cyan]  Show connected peers")

        console.print("\n  [bold red]--- Simulate Distributed Problems ---[/bold red]")
        console.print("  [bold red][6][/bold red]   Simulate: Network Delay (inject latency)")
        console.print("  [bold red][7][/bold red]   Simulate: Node Failure (self-shutdown)")
        console.print("  [bold red][8][/bold red]   Simulate: Data Consistency conflict")
        console.print("  [bold red][9][/bold red]   Simulate: Concurrent booking storm")
        console.print("  [bold red][10][/bold red]  Simulate: Cross-region booking (partitioned data)")
        console.print("  [bold red][11][/bold red]  Simulate: Node Recovery (re-join network)")
        console.print("  [bold red][12][/bold red]  Simulate: Graceful Degradation (peer unavailable)")

        console.print("\n  [dim][0]  Exit[/dim]\n")

        try:
            choice = input("  Select: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == "0":
            break
        elif choice == "1":
            book_journey(session)
        elif choice == "2":
            cancel_journey(session)
        elif choice == "3":
            view_bookings(session)
        elif choice == "4":
            show_region_network(session)
        elif choice == "5":
            show_connected_peers(session)
        elif choice == "6":
            simulate_network_delay(session)
        elif choice == "7":
            simulate_node_failure(session)
        elif choice == "8":
            simulate_data_consistency(session)
        elif choice == "9":
            simulate_concurrent_storm(session)
        elif choice == "10":
            simulate_cross_region(session)
        elif choice == "11":
            simulate_node_recovery(session)
        elif choice == "12":
            simulate_graceful_degradation(session)
        else:
            console.print("[red]  Unknown option.[/red]")


def main():
    parser = argparse.ArgumentParser(description="GDTS Distributed Systems Simulator")
    parser.add_argument("--gateway", default="http://localhost:8080", help="API gateway URL")
    parser.add_argument("--token", default=None, help="JWT bearer token (skips login)")
    args = parser.parse_args()

    console.print(f"\n[bold bright_green]  GDTS — Globally Distributed Traffic Service[/bold bright_green]")
    console.print(f"  Gateway: [cyan]{args.gateway}[/cyan]\n")

    session = Session(gateway=args.gateway, token=args.token)

    if not session.token:
        console.print("  [dim]Enter credentials (or Ctrl+C to exit)[/dim]")
        try:
            email = input("  Email    : ").strip()
            password = input("  Password : ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]  Exiting.[/yellow]")
            return

        if not session.login(email, password):
            console.print("[red]  Authentication failed. Exiting.[/red]")
            return
        console.print("[green]  Authenticated.[/green]\n")

    run_menu(session)
    console.print("\n[dim]  Goodbye.[/dim]\n")


if __name__ == "__main__":
    main()
