"""
Local Demo Script - Journey Booking System (no API gateway)
===========================================================
Same as demo.py but uses direct service ports instead of going through Nginx.

Run: conda run -n DS python scripts/demo_local.py
Requires: Services running via bash scripts/run_local.sh
"""

import httpx
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta

# Direct service URLs (no nginx)
USER_URL = "http://localhost:8001"
JOURNEY_URL = "http://localhost:8002"
CONFLICT_URL = "http://localhost:8003"
NOTIFICATION_URL = "http://localhost:8004"
ENFORCEMENT_URL = "http://localhost:8005"
ANALYTICS_URL = "http://localhost:8006"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def header(text):
    print(f"\n{BOLD}{CYAN}{'='*60}")
    print(f"  {text}")
    print(f"{'='*60}{RESET}\n")


def success(text):
    print(f"  {GREEN}OK  {text}{RESET}")


def error(text):
    print(f"  {RED}ERR {text}{RESET}")


def info(text):
    print(f"  {YELLOW}... {text}{RESET}")


def pretty_json(data):
    print(f"  {json.dumps(data, indent=2, default=str)}")


async def check_service(client, url, name):
    try:
        resp = await client.get(f"{url}/health", timeout=5)
        if resp.status_code == 200:
            success(f"{name} healthy")
            return True
        else:
            error(f"{name} unhealthy (HTTP {resp.status_code})")
            return False
    except Exception as e:
        error(f"{name} unreachable: {e}")
        return False


async def wait_for_services():
    """Poll until all services are healthy (max 60s)."""
    header("Waiting for all services to be ready...")
    deadline = time.time() + 60
    services = [
        (USER_URL, "user-service"),
        (JOURNEY_URL, "journey-service"),
        (CONFLICT_URL, "conflict-service"),
        (NOTIFICATION_URL, "notification-service"),
        (ENFORCEMENT_URL, "enforcement-service"),
        (ANALYTICS_URL, "analytics-service"),
    ]

    while time.time() < deadline:
        all_up = True
        async with httpx.AsyncClient() as client:
            for url, name in services:
                try:
                    resp = await client.get(f"{url}/health", timeout=3)
                    if resp.status_code != 200:
                        all_up = False
                except Exception:
                    all_up = False

        if all_up:
            success("All services healthy!")
            return True

        info(f"Some services not ready yet, retrying in 3s...")
        await asyncio.sleep(3)

    error("Timed out waiting for services. Run 'bash scripts/run_local.sh start' first.")
    return False


async def main():
    if not await wait_for_services():
        sys.exit(1)

    # ============================================
    # Step 1: System Health
    # ============================================
    header("Step 1: System Health Check")
    async with httpx.AsyncClient() as client:
        for url, name in [
            (USER_URL, "user-service"),
            (JOURNEY_URL, "journey-service"),
            (CONFLICT_URL, "conflict-service"),
            (NOTIFICATION_URL, "notification-service"),
            (ENFORCEMENT_URL, "enforcement-service"),
            (ANALYTICS_URL, "analytics-service"),
        ]:
            await check_service(client, url, name)

    # ============================================
    # Step 2: Register Users
    # ============================================
    header("Step 2: Register Users")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{USER_URL}/api/users/register", json={
            "email": "alice@example.com",
            "password": "securepass123",
            "full_name": "Alice Johnson",
            "license_number": "DL-ALICE-001"
        })
        if resp.status_code == 201:
            success(f"Registered Alice: {resp.json()['id']}")
        elif resp.status_code == 409:
            info("Alice already registered")
        else:
            error(f"Alice registration failed: {resp.text}")

        resp = await client.post(f"{USER_URL}/api/users/register", json={
            "email": "bob@example.com",
            "password": "securepass456",
            "full_name": "Bob Smith",
            "license_number": "DL-BOB-002"
        })
        if resp.status_code == 201:
            success(f"Registered Bob: {resp.json()['id']}")
        elif resp.status_code == 409:
            info("Bob already registered")
        else:
            error(f"Bob registration failed: {resp.text}")

    # ============================================
    # Step 3: Login
    # ============================================
    header("Step 3: Login")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{USER_URL}/api/users/login", json={
            "email": "alice@example.com", "password": "securepass123"
        })
        if resp.status_code != 200:
            error(f"Alice login failed: {resp.text}")
            return
        alice_token = resp.json()["access_token"]
        success(f"Alice logged in (token: {alice_token[:20]}...)")

        resp = await client.post(f"{USER_URL}/api/users/login", json={
            "email": "bob@example.com", "password": "securepass456"
        })
        if resp.status_code != 200:
            error(f"Bob login failed: {resp.text}")
            return
        bob_token = resp.json()["access_token"]
        success(f"Bob logged in (token: {bob_token[:20]}...)")

    alice_headers = {"Authorization": f"Bearer {alice_token}"}
    bob_headers = {"Authorization": f"Bearer {bob_token}"}

    # ============================================
    # Step 3b: Register Vehicles
    # ============================================
    header("Step 3b: Register Vehicles")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{USER_URL}/api/users/vehicles", json={
            "registration": "221-D-12345",
            "vehicle_type": "CAR"
        }, headers=alice_headers)
        if resp.status_code in (201, 409):
            success("Alice's vehicle 221-D-12345 registered")
        else:
            error(f"Alice vehicle registration failed: {resp.text}")

        resp = await client.post(f"{USER_URL}/api/users/vehicles", json={
            "registration": "231-L-67890",
            "vehicle_type": "CAR"
        }, headers=bob_headers)
        if resp.status_code in (201, 409):
            success("Bob's vehicle 231-L-67890 registered")
        else:
            error(f"Bob vehicle registration failed: {resp.text}")

    # ============================================
    # Step 4: Show Predefined Routes
    # ============================================
    header("Step 4: Available Predefined Road Routes")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{CONFLICT_URL}/api/routes")
        if resp.status_code == 200:
            routes_data = resp.json()
            success(f"{routes_data['count']} predefined routes available:")
            for r in routes_data["routes"]:
                info(f"  [{r['route_id']}] {r['name']} ({r['estimated_duration_minutes']} min)")
                info(f"    Waypoints: {' → '.join(w['name'] for w in r['waypoints'])}")
        else:
            error(f"Could not fetch routes: {resp.text}")

    # ============================================
    # Step 5: Book Dublin → Galway for Alice (CONFIRMED)
    # Uses the predefined M6 route with real waypoints
    # ============================================
    header("Step 5: Alice books Dublin → Galway via M6 (Expected: CONFIRMED)")
    departure = datetime.utcnow() + timedelta(hours=2)
    journey1 = None

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{JOURNEY_URL}/api/journeys/", json={
            "origin": "Dublin",
            "destination": "Galway",
            "origin_lat": 53.3498, "origin_lng": -6.2603,
            "destination_lat": 53.2707, "destination_lng": -9.0568,
            "departure_time": departure.isoformat(),
            "estimated_duration_minutes": 135,
            "vehicle_registration": "221-D-12345",
            "route_id": "dublin-galway",
            "idempotency_key": f"demo-alice-dub-gal-{int(time.time())}"
        }, headers=alice_headers)

        if resp.status_code == 201:
            journey1 = resp.json()
            if journey1["status"] == "CONFIRMED":
                success(f"Alice's Dublin→Galway journey CONFIRMED: {journey1['id']}")
                info(f"  Road cells locked: Dublin, Leixlip, Kinnegad, Athlone, Ballinasloe, Galway")
            else:
                info(f"Journey status: {journey1['status']} — {journey1.get('rejection_reason', '')}")
        else:
            error(f"Booking failed: {resp.text}")

    # ============================================
    # Step 5b: Driver time overlap (same driver, same time)
    # ============================================
    header("Step 5b: Alice tries a second journey at same time (Expected: REJECTED — driver overlap)")
    overlap_departure = departure + timedelta(minutes=30)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{JOURNEY_URL}/api/journeys/", json={
            "origin": "Dublin",
            "destination": "Cork",
            "origin_lat": 53.3498, "origin_lng": -6.2603,
            "destination_lat": 51.8985, "destination_lng": -8.4756,
            "departure_time": overlap_departure.isoformat(),
            "estimated_duration_minutes": 150,
            "vehicle_registration": "221-D-12345",
            "route_id": "dublin-cork",
            "idempotency_key": f"demo-alice-dub-cork-{int(time.time())}"
        }, headers=alice_headers)

        if resp.status_code == 201:
            j = resp.json()
            if j["status"] == "REJECTED":
                success(f"Correctly REJECTED: {j.get('rejection_reason')}")
            else:
                error(f"Expected REJECTED but got: {j['status']}")

    # ============================================
    # Step 5c: Road capacity conflict (different driver, same road segment)
    #
    # Bob tries to book Kinnegad → Athlone — a midpoint segment of the M6.
    # Even without specifying route_id, the straight-line path between those
    # two coordinates crosses the same grid cells Alice's M6 booking locked.
    # With max_capacity=1, this must be REJECTED.
    # ============================================
    header("Step 5c: Bob books Kinnegad→Athlone (M6 midpoint) at same time (Expected: REJECTED — road capacity)")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{JOURNEY_URL}/api/journeys/", json={
            "origin": "Kinnegad",
            "destination": "Athlone",
            "origin_lat": 53.4608, "origin_lng": -7.1006,
            "destination_lat": 53.4239, "destination_lng": -7.9407,
            "departure_time": (departure + timedelta(minutes=45)).isoformat(),
            "estimated_duration_minutes": 45,
            "vehicle_registration": "231-L-67890",
            "idempotency_key": f"demo-bob-kin-ath-{int(time.time())}"
        }, headers=bob_headers)

        if resp.status_code == 201:
            j = resp.json()
            if j["status"] == "REJECTED":
                success(f"Correctly REJECTED (M6 road segment already occupied):")
                info(f"  {j.get('rejection_reason')}")
            else:
                error(f"Expected REJECTED but got: {j['status']} — road conflict not detected")

    # ============================================
    # Step 6: Bob books a completely different route (CONFIRMED)
    # Galway → Limerick uses N18, zero grid cell overlap with the M6
    # ============================================
    header("Step 6: Bob books Galway → Limerick via N18 (Expected: CONFIRMED — different road)")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{JOURNEY_URL}/api/journeys/", json={
            "origin": "Galway",
            "destination": "Limerick",
            "origin_lat": 53.2707, "origin_lng": -9.0568,
            "destination_lat": 52.6638, "destination_lng": -8.6267,
            "departure_time": departure.isoformat(),
            "estimated_duration_minutes": 60,
            "vehicle_registration": "231-L-67890",
            "route_id": "galway-limerick",
            "idempotency_key": f"demo-bob-gal-lim-{int(time.time())}"
        }, headers=bob_headers)

        if resp.status_code == 201:
            bob_journey = resp.json()
            if bob_journey["status"] == "CONFIRMED":
                success(f"Bob's Galway→Limerick journey CONFIRMED: {bob_journey['id']}")
                info(f"  N18 route: Galway → Gort → Ennis → Limerick")
                info(f"  No overlap with Alice's M6 route — correctly allowed")
            else:
                info(f"Bob's journey: {bob_journey['status']} — {bob_journey.get('rejection_reason', '')}")
        else:
            error(f"Bob's booking failed: {resp.text}")

    # ============================================
    # Step 7: Enforcement Check
    # ============================================
    header("Step 7: Enforcement Verification")
    await asyncio.sleep(1)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{ENFORCEMENT_URL}/api/enforcement/verify/vehicle/221-D-12345")
        if resp.status_code == 200:
            v = resp.json()
            if v["is_valid"]:
                success(f"Vehicle 221-D-12345 has valid booking")
                info(f"  Journey: {v.get('origin')} -> {v.get('destination')}")
            else:
                info("No active journey found (Redis may not have propagated yet)")

        resp = await client.get(f"{ENFORCEMENT_URL}/api/enforcement/verify/license/DL-ALICE-001")
        if resp.status_code == 200:
            v = resp.json()
            if v["is_valid"]:
                success(f"License DL-ALICE-001 has valid booking")
            else:
                info("License verification: no active booking found via cache (will try DB fallback)")

        resp = await client.get(f"{ENFORCEMENT_URL}/api/enforcement/verify/vehicle/999-XX-99999")
        if resp.status_code == 200 and not resp.json()["is_valid"]:
            success("Correctly identified unbooked vehicle 999-XX-99999")

    # ============================================
    # Step 8: List Alice's Journeys
    # ============================================
    header("Step 8: List Alice's Journeys")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{JOURNEY_URL}/api/journeys/", headers=alice_headers)
        if resp.status_code == 200:
            data = resp.json()
            success(f"Alice has {data['total']} journey(s)")
            for j in data["journeys"]:
                info(f"  [{j['status']}] {j['origin']} -> {j['destination']}")

    # ============================================
    # Step 9: Cancel Alice's Journey
    # ============================================
    header("Step 9: Cancel Alice's Confirmed Journey")
    if journey1 and journey1.get("id"):
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(
                f"{JOURNEY_URL}/api/journeys/{journey1['id']}",
                headers=alice_headers
            )
            if resp.status_code == 200:
                success(f"Journey cancelled: {resp.json()['status']}")
            else:
                error(f"Cancellation failed: {resp.text}")

    # ============================================
    # Step 10: Notifications
    # ============================================
    header("Step 10: Check Notifications")
    await asyncio.sleep(2)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{NOTIFICATION_URL}/api/notifications/",
            params={"token": alice_token, "limit": 5}
        )
        if resp.status_code == 200:
            data = resp.json()
            success(f"Alice has {data['count']} notification(s)")
            for n in data["notifications"][:3]:
                info(f"  [{n['event_type']}] {n['title']}")

    # ============================================
    # Step 11: Analytics
    # ============================================
    header("Step 11: Analytics & System Stats")
    await asyncio.sleep(2)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{ANALYTICS_URL}/api/analytics/stats")
        if resp.status_code == 200:
            success("System Statistics:")
            pretty_json(resp.json())

        resp = await client.get(f"{ANALYTICS_URL}/api/analytics/events?limit=5")
        if resp.status_code == 200:
            data = resp.json()
            success(f"Recent Events ({data['count']}):")
            for e in data.get("events", []):
                info(f"  [{e['event_type']}] journey={str(e.get('journey_id', 'N/A'))[:8]}...")

        resp = await client.get(f"{ANALYTICS_URL}/api/analytics/health/services")
        if resp.status_code == 200:
            health = resp.json()
            info(f"Overall system status: {health.get('overall_status', 'unknown')}")

    # ============================================
    # Done
    # ============================================
    header("Demo Complete!")
    info("Flows demonstrated:")
    info("  OK  User registration and JWT authentication")
    info("  OK  Predefined road routes fetched from conflict service")
    info("  OK  Journey booking with real M6 road waypoints (not straight-line)")
    info("  OK  Driver time-overlap rejection (same driver, overlapping window)")
    info("  OK  Road capacity rejection: Kinnegad→Athlone blocked by Alice's M6 booking")
    info("  OK  Independent road confirmed: Bob's N18 Galway→Limerick has zero M6 overlap")
    info("  OK  Enforcement verification (Redis-cached + API fallback)")
    info("  OK  Journey cancellation with RabbitMQ event propagation")
    info("  OK  Notification delivery (WebSocket + Redis history)")
    info("  OK  Analytics and monitoring")
    print()


if __name__ == "__main__":
    asyncio.run(main())
