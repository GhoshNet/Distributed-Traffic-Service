# ============================================================
# simulation/problems.py — Distributed systems problem simulators
# ============================================================
"""
Interactive menu handlers that let users trigger each of the 7 classic
distributed-systems problems and observe how the system reacts.
All output is logged to the terminal in real time.
"""

import json
import random
import threading
import time
from datetime import datetime, timedelta

import requests
from rich.console import Console
from rich.table import Table
from tabulate import tabulate

import config
from utils.logger import log, banner, separator

console = Console()


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

def _local_url(state) -> str:
    return f"http://127.0.0.1:{state.api_port}"


def _pick_city(state) -> str:
    return random.choice(state.road_network.cities)


def _pick_two_cities(state):
    cities = state.road_network.cities
    if len(cities) < 2:
        return cities[0], cities[0]
    a, b = random.sample(cities, 2)
    return a, b


def _future_departure(minutes_ahead: int = 60) -> str:
    return (datetime.utcnow() + timedelta(minutes=minutes_ahead)).isoformat()


def _print_bookings(state):
    bookings = state.db.get_all_bookings()
    if not bookings:
        console.print("[dim]  (no bookings)[/dim]")
        return
    tbl = Table(show_lines=True)
    for col in ["booking_id", "driver_id", "origin", "destination", "status", "home_region"]:
        tbl.add_column(col.replace("_", " ").title())
    for b in bookings:
        status_color = {
            "CONFIRMED": "green", "HELD": "yellow",
            "CANCELLED": "red", "PENDING": "dim",
        }.get(b.get("status", ""), "white")
        tbl.add_row(
            b.get("booking_id", ""), b.get("driver_id", ""),
            b.get("origin", ""),    b.get("destination", ""),
            f"[{status_color}]{b.get('status','')}[/{status_color}]",
            b.get("home_region", ""),
        )
    console.print(tbl)


def _print_peers(state):
    peers = state.db.get_all_peers()
    if not peers:
        console.print("[dim]  (no peers discovered yet)[/dim]")
        return
    tbl = Table(show_lines=True)
    for col in ["Region", "Host", "Port", "Status", "Cities", "Failures"]:
        tbl.add_column(col)
    for p in peers:
        status_color = {"ALIVE": "green", "SUSPECT": "yellow", "DEAD": "red"}.get(
            p.get("status", ""), "white"
        )
        cities = json.loads(p.get("cities", "[]"))
        tbl.add_row(
            p.get("region_name", ""), p.get("host", ""),
            str(p.get("port", "")),
            f"[{status_color}]{p.get('status','')}[/{status_color}]",
            ", ".join(cities[:4]) + ("…" if len(cities) > 4 else ""),
            str(p.get("consecutive_failures", 0)),
        )
    console.print(tbl)


# ══════════════════════════════════════════════════════════════════════
#  Problem 1 — Network Delay
# ══════════════════════════════════════════════════════════════════════

def simulate_network_delay(state):
    banner("SIMULATE: Network Delay")
    log("SIMULATION", "This injects artificial latency on every outgoing call.")

    current = state.network_delay_ms
    console.print(f"  Current delay: [yellow]{current} ms[/yellow]")
    try:
        raw = input("  Enter delay in ms (0 to remove): ").strip()
        ms  = int(raw)
    except ValueError:
        console.print("[red]  Invalid input.[/red]")
        return

    state.network_delay_ms = ms
    if ms == 0:
        log("SIMULATION", "✅ Network delay removed", "SUCCESS")
    else:
        log("SIMULATION",
            f"⏳ Injecting {ms} ms delay on all outgoing calls", "WARN")
        log("SIMULATION",
            "   → Retry / timeout logic will kick in if delay > REQUEST_TIMEOUT",
            "INFO")
        log("SIMULATION",
            f"   → REQUEST_TIMEOUT is {config.REQUEST_TIMEOUT * 1000} ms", "INFO")

        # Demo: make a test booking and show the delay
        a, b = _pick_two_cities(state)
        log("SIMULATION", f"   Making a test booking {a} → {b} with delay active…")
        t0 = time.time()
        try:
            resp = requests.post(
                f"{_local_url(state)}/api/booking/create",
                json={
                    "driver_id": "DELAY-TEST",
                    "origin": a, "destination": b,
                    "departure_time": _future_departure(30),
                },
                timeout=10,
            )
            elapsed = (time.time() - t0) * 1000
            log("SIMULATION",
                f"   Response in {elapsed:.0f} ms — status={resp.status_code}",
                "SUCCESS")
        except Exception as e:
            elapsed = (time.time() - t0) * 1000
            log("SIMULATION", f"   ❌ Request timed out after {elapsed:.0f} ms: {e}", "ERROR")


# ══════════════════════════════════════════════════════════════════════
#  Problem 2 — Node Failure
# ══════════════════════════════════════════════════════════════════════

def simulate_node_failure(state):
    banner("SIMULATE: Node Failure")
    if state.failure_simulated:
        log("SIMULATION",
            "ℹ️  Node is already in failure mode. Use [11] Recovery to restore.", "WARN")
        return

    log("SIMULATION", "Simulating node failure:", "WARN")
    log("SIMULATION", "  • API ping endpoint will return 503")
    log("SIMULATION", "  • Discovery broadcasts will stop")
    log("SIMULATION", "  • New bookings will be rejected")
    log("SIMULATION", "  • Peers will detect this via missed heartbeats")
    log("SIMULATION",
        f"  • Peers will mark this node SUSPECT after {config.SUSPECT_THRESHOLD} misses "
        f"({config.SUSPECT_THRESHOLD * config.HEARTBEAT_INTERVAL}s)")
    log("SIMULATION",
        f"  • Then DEAD after {config.DEAD_THRESHOLD} misses "
        f"({config.DEAD_THRESHOLD * config.HEARTBEAT_INTERVAL}s)")

    confirm = input("  Confirm failure simulation? (y/N): ").strip().lower()
    if confirm != "y":
        return

    state.failure_simulated = True
    log("SIMULATION", "💀 NODE NOW IN FAILURE STATE", "ERROR")
    log("SIMULATION",
        "   Watch peer terminals — they will detect this via missed heartbeats.",
        "INFO")
    log("SIMULATION", "   Use option [11] to recover this node.", "INFO")
    state.db.log_event("NODE_FAILURE_SIMULATED", {"region": state.region_name})


# ══════════════════════════════════════════════════════════════════════
#  Problem 3 — Data Consistency Conflict
# ══════════════════════════════════════════════════════════════════════

def simulate_data_consistency(state):
    banner("SIMULATE: Data Consistency Conflict")
    log("SIMULATION",
        "Two concurrent clients attempt to write the same route at the same time.")
    log("SIMULATION",
        "Optimistic locking + conflict detection ensures only one succeeds.")

    a, b = _pick_two_cities(state)
    dep  = _future_departure(90)
    results = []
    errors  = []

    def attempt(driver_id):
        try:
            resp = requests.post(
                f"{_local_url(state)}/api/booking/create",
                json={"driver_id": driver_id, "origin": a,
                      "destination": b, "departure_time": dep},
                timeout=8,
            )
            results.append((driver_id, resp.json()))
        except Exception as e:
            errors.append((driver_id, str(e)))

    log("SIMULATION",
        f"   Firing two simultaneous requests: {a} → {b} @ {dep[:16]}")

    t1 = threading.Thread(target=attempt, args=("DRIVER-A",))
    t2 = threading.Thread(target=attempt, args=("DRIVER-B",))
    t1.start(); t2.start()
    t1.join();  t2.join()

    for driver, res in results:
        ok  = res.get("success")
        msg = res.get("message", "")
        bid = (res.get("booking") or {}).get("booking_id", "—")
        icon = "✅" if ok else "❌"
        log("SIMULATION",
            f"   {icon} {driver}: {msg}  booking_id={bid}",
            "SUCCESS" if ok else "WARN")

    wins  = sum(1 for _, r in results if r.get("success"))
    log("SIMULATION",
        f"   Result: {wins}/2 succeeded — conflict detection {'✅ working' if wins <= 1 else '⚠️  multiple writes accepted'}",
        "SUCCESS" if wins <= 1 else "ERROR")


# ══════════════════════════════════════════════════════════════════════
#  Problem 4 — Concurrent Booking Storm
# ══════════════════════════════════════════════════════════════════════

def simulate_concurrent_storm(state):
    banner("SIMULATE: Concurrent Booking Storm")
    try:
        n = int(input("  Number of concurrent booking requests (e.g. 10): ").strip())
    except ValueError:
        n = 10

    log("SIMULATION",
        f"🌪️  Firing {n} concurrent booking requests to stress-test locking…", "WARN")

    cities  = state.road_network.cities
    results = []
    lock    = threading.Lock()

    def worker(i):
        if len(cities) < 2:
            return
        a, b = random.sample(cities, 2)
        dep  = _future_departure(random.randint(10, 180))
        try:
            resp = requests.post(
                f"{_local_url(state)}/api/booking/create",
                json={
                    "driver_id": f"STORM-{i:03d}",
                    "origin": a, "destination": b,
                    "departure_time": dep,
                },
                timeout=8,
            )
            data = resp.json()
            with lock:
                results.append(data.get("success", False))
                icon = "✅" if data.get("success") else "⚠️ "
                log("SIMULATION",
                    f"   [{i:03d}] {a}→{b}  {icon} {data.get('message','')[:60]}",
                    "SUCCESS" if data.get("success") else "WARN")
        except Exception as e:
            with lock:
                results.append(False)
                log("SIMULATION", f"   [{i:03d}] ERROR: {e}", "ERROR")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    t0 = time.time()
    for t in threads: t.start()
    for t in threads: t.join()
    elapsed = time.time() - t0

    ok_count = sum(results)
    log("SIMULATION",
        f"✅ Storm complete: {ok_count}/{n} bookings succeeded in {elapsed:.2f}s",
        "SUCCESS")
    log("SIMULATION",
        f"   Throughput: {n/elapsed:.1f} req/s  |  "
        f"SQLite WAL + thread lock held data integrity",
        "INFO")


# ══════════════════════════════════════════════════════════════════════
#  Problem 5 — Cross-Region Booking (Partitioned Data)
# ══════════════════════════════════════════════════════════════════════

def simulate_cross_region(state):
    banner("SIMULATE: Cross-Region Booking (2PC)")
    peers = state.db.get_all_peers(status="ALIVE")

    if not peers:
        log("SIMULATION",
            "⚠️  No alive peers discovered. Start another node first.", "WARN")
        log("SIMULATION",
            "   Run: python main.py  in a second terminal to create another region.", "INFO")
        return

    # Pick a city from a remote peer
    remote_peer = random.choice(peers)
    remote_cities = json.loads(remote_peer.get("cities", "[]"))
    if not remote_cities:
        log("SIMULATION", f"Peer [{remote_peer['region_name']}] has no cities.", "WARN")
        return

    local_origin  = _pick_city(state)
    remote_dest   = random.choice(remote_cities)

    log("SIMULATION",
        f"   Booking: {local_origin} [{state.region_name}] → {remote_dest} [{remote_peer['region_name']}]")
    log("SIMULATION", "   This will trigger a 2PC between the two regions.", "INFO")
    log("SIMULATION", "   Watch BOTH terminal windows for PREPARE/COMMIT messages.")

    dep = _future_departure(120)
    try:
        resp = requests.post(
            f"{_local_url(state)}/api/booking/create",
            json={
                "driver_id": "CROSS-REGION-DEMO",
                "origin": local_origin,
                "destination": remote_dest,
                "departure_time": dep,
            },
            timeout=15,
        )
        data = resp.json()
        ok   = data.get("success")
        log("SIMULATION",
            f"{'✅' if ok else '❌'} {data.get('message', '')}",
            "SUCCESS" if ok else "ERROR")
        if ok and data.get("booking"):
            b = data["booking"]
            log("SIMULATION",
                f"   booking_id={b.get('booking_id')}  status={b.get('status')}")
    except Exception as e:
        log("SIMULATION", f"❌ Request failed: {e}", "ERROR")


# ══════════════════════════════════════════════════════════════════════
#  Problem 6 — Node Recovery
# ══════════════════════════════════════════════════════════════════════

def simulate_node_recovery(state):
    banner("SIMULATE: Node Recovery")
    if not state.failure_simulated:
        log("SIMULATION",
            "ℹ️  Node is not in failure mode. Use [7] to simulate failure first.", "INFO")
        return

    log("SIMULATION", "🔄 Recovering node…", "INFO")
    state.failure_simulated = False
    log("SIMULATION",
        "✅ Node restored! Discovery broadcasts will resume.", "SUCCESS")
    log("SIMULATION",
        "   Replication service will pull missed bookings from peers…", "INFO")

    # Trigger sync from all alive peers
    peers = state.db.get_all_peers(status="ALIVE")
    if peers and state.replication_service:
        for peer in peers:
            threading.Thread(
                target=state.replication_service.sync_from_peer,
                args=(peer,), daemon=True,
            ).start()
        log("SIMULATION",
            f"   Initiated state sync from {len(peers)} peer(s)", "INFO")

    state.db.log_event("NODE_RECOVERED", {"region": state.region_name})
    log("SIMULATION",
        "   Peers will re-detect this node as ALIVE within "
        f"{config.HEARTBEAT_INTERVAL}s", "INFO")


# ══════════════════════════════════════════════════════════════════════
#  Problem 7 — Graceful Degradation
# ══════════════════════════════════════════════════════════════════════

def simulate_graceful_degradation(state):
    banner("SIMULATE: Graceful Degradation")
    log("SIMULATION",
        "Forcing LOCAL_ONLY mode to simulate majority-peer failure.", "WARN")
    log("SIMULATION",
        "  • Cross-region bookings will be queued/rejected")
    log("SIMULATION",
        "  • Local bookings continue to work normally")
    log("SIMULATION",
        "  • System returns to normal when peers recover")

    if state.local_only_mode:
        log("SIMULATION",
            "ℹ️  Already in LOCAL ONLY mode. Disabling it now.", "INFO")
        state.local_only_mode = False
        log("SIMULATION", "✅ Back to normal mode", "SUCCESS")
        return

    state.local_only_mode = True
    log("SIMULATION",
        "🔴 LOCAL ONLY mode enabled — cross-region requests will be blocked", "ERROR")

    # Demo: attempt a cross-region booking (should be blocked)
    peers = state.db.get_all_peers(status="ALIVE")
    if peers:
        remote_peer   = random.choice(peers)
        remote_cities = json.loads(remote_peer.get("cities", "[]"))
        if remote_cities:
            local_city  = _pick_city(state)
            remote_city = random.choice(remote_cities)
            log("SIMULATION",
                f"   Attempting cross-region booking {local_city} → {remote_city} (should fail)…")
            try:
                resp = requests.post(
                    f"{_local_url(state)}/api/booking/create",
                    json={
                        "driver_id": "DEGRADE-TEST",
                        "origin": local_city, "destination": remote_city,
                        "departure_time": _future_departure(45),
                    },
                    timeout=8,
                )
                data = resp.json()
                log("SIMULATION",
                    f"   Result: {data.get('message', '')}",
                    "SUCCESS" if data.get("success") else "WARN")
            except Exception as e:
                log("SIMULATION", f"   Error: {e}", "ERROR")

    log("SIMULATION",
        "   Local bookings still work. Testing…")
    a, b = _pick_two_cities(state)
    try:
        resp = requests.post(
            f"{_local_url(state)}/api/booking/create",
            json={"driver_id": "DEGRADE-LOCAL",
                  "origin": a, "destination": b,
                  "departure_time": _future_departure(60)},
            timeout=8,
        )
        data = resp.json()
        ok   = data.get("success")
        log("SIMULATION",
            f"   Local booking {a}→{b}: {'✅' if ok else '❌'} {data.get('message', '')}",
            "SUCCESS" if ok else "WARN")
    except Exception as e:
        log("SIMULATION", f"   Local booking error: {e}", "ERROR")


# ══════════════════════════════════════════════════════════════════════
#  Interactive menu
# ══════════════════════════════════════════════════════════════════════

def run_menu(state):
    """Main interactive menu loop."""
    while True:
        separator()
        console.print(f"\n[bold bright_white]  ═══ GDTS Terminal — Region: {state.region_name} ═══[/bold bright_white]")
        console.print(f"  [dim]Host {state.host}:{state.api_port}  |  "
                      f"Peers: {len(state.db.get_all_peers())}  |  "
                      f"Bookings: {len(state.db.get_all_bookings())}  |  "
                      f"Delay: {state.network_delay_ms}ms  |  "
                      f"{'💀 FAILED' if state.failure_simulated else '🟢 ALIVE'}  |  "
                      f"{'🔴 LOCAL-ONLY' if state.local_only_mode else '🌐 GLOBAL'}[/dim]\n")

        console.print("  [bold cyan]── Standard Operations ──[/bold cyan]")
        console.print("  [1]  Book a journey")
        console.print("  [2]  Cancel a booking")
        console.print("  [3]  List all bookings")
        console.print("  [4]  Show road network graph")
        console.print("  [5]  Show connected peers")

        console.print("\n  [bold red]── Simulate Distributed Problems ──[/bold red]")
        console.print("  [6]  🌐 Network Delay — inject latency")
        console.print("  [7]  💀 Node Failure  — simulate crash")
        console.print("  [8]  🔀 Data Consistency — concurrent conflict")
        console.print("  [9]  🌪️  Concurrent Storm — booking flood")
        console.print("  [10] 🗺️  Cross-Region booking (2PC demo)")
        console.print("  [11] 🔄 Node Recovery — rejoin network")
        console.print("  [12] 🔴 Graceful Degradation — local-only mode")
        console.print("\n  [0]  Exit\n")

        try:
            choice = input("  Select: ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if choice == "0":
            break

        elif choice == "1":
            _menu_book(state)
        elif choice == "2":
            _menu_cancel(state)
        elif choice == "3":
            _print_bookings(state)
        elif choice == "4":
            state.road_network.print_graph()
        elif choice == "5":
            _print_peers(state)
        elif choice == "6":
            simulate_network_delay(state)
        elif choice == "7":
            simulate_node_failure(state)
        elif choice == "8":
            simulate_data_consistency(state)
        elif choice == "9":
            simulate_concurrent_storm(state)
        elif choice == "10":
            simulate_cross_region(state)
        elif choice == "11":
            simulate_node_recovery(state)
        elif choice == "12":
            simulate_graceful_degradation(state)
        else:
            console.print("[red]  Unknown option.[/red]")


# ──────────────────────────────────────────────────────────────────────
# Menu helpers
# ──────────────────────────────────────────────────────────────────────

def _menu_book(state):
    banner("Book a Journey")
    cities_all = list(state.road_network.graph.nodes)
    console.print(f"  Known cities: {', '.join(cities_all)}")

    origin      = input("  Origin city      : ").strip()
    destination = input("  Destination city : ").strip()

    dep_str = input(
        "  Departure time (YYYY-MM-DDTHH:MM, blank=+1h): "
    ).strip()
    if dep_str:
        try:
            dep_dt = datetime.fromisoformat(dep_str)
        except ValueError:
            console.print("[red]  Invalid date format.[/red]")
            return
    else:
        dep_dt = datetime.utcnow() + timedelta(hours=1)

    driver_id = input("  Driver ID (blank=AUTO): ").strip() or f"DRV-{random.randint(100,999)}"

    try:
        resp = requests.post(
            f"{_local_url(state)}/api/booking/create",
            json={
                "driver_id": driver_id,
                "origin": origin,
                "destination": destination,
                "departure_time": dep_dt.isoformat(),
            },
            timeout=12,
        )
        data = resp.json()
        ok   = data.get("success")
        log("SIMULATION",
            f"{'✅' if ok else '❌'} {data.get('message', '')}",
            "SUCCESS" if ok else "ERROR")
        if ok and data.get("booking"):
            b = data["booking"]
            console.print(f"\n  [green]Booking ID:[/green] {b.get('booking_id')}")
            console.print(f"  [green]Route:[/green]     {' → '.join(b.get('route_path') or [origin, destination])}")
            console.print(f"  [green]Status:[/green]    {b.get('status')}")
    except Exception as e:
        log("SIMULATION", f"❌ {e}", "ERROR")


def _menu_cancel(state):
    banner("Cancel a Booking")
    _print_bookings(state)
    bid = input("\n  Booking ID to cancel: ").strip().upper()
    if not bid:
        return
    try:
        resp = requests.post(
            f"{_local_url(state)}/api/booking/cancel/{bid}",
            timeout=8,
        )
        data = resp.json()
        ok   = data.get("success")
        log("SIMULATION",
            f"{'✅' if ok else '❌'} {data.get('message', '')}",
            "SUCCESS" if ok else "ERROR")
    except Exception as e:
        log("SIMULATION", f"❌ {e}", "ERROR")
