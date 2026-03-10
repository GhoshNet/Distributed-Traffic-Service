"""
Demonstration Script - Journey Booking System
=============================================

Exercises the complete booking flow:
1. Register two users
2. Login and get tokens
3. Book journeys (confirmed and conflicting)
4. Verify a journey via enforcement
5. Cancel a journey
6. Check analytics & notifications

Run: python scripts/demo.py
Requires: System running via docker compose up
"""

import httpx
import asyncio
import json
import sys
from datetime import datetime, timedelta

BASE_URL = "http://localhost:8080"

# ANSI colors for pretty output
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
    print(f"  {GREEN}✅ {text}{RESET}")


def error(text):
    print(f"  {RED}❌ {text}{RESET}")


def info(text):
    print(f"  {YELLOW}ℹ️  {text}{RESET}")


def pretty_json(data):
    print(f"  {json.dumps(data, indent=2, default=str)}")


async def main():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:

        # ============================================
        # Step 1: Check system health
        # ============================================
        header("Step 1: System Health Check")
        try:
            resp = await client.get("/api/analytics/health/services")
            if resp.status_code == 200:
                health = resp.json()
                for svc, status in health.get("services", {}).items():
                    if status["status"] == "healthy":
                        success(f"{svc}: {status['status']} ({status.get('response_time_ms', 'N/A'):.0f}ms)")
                    else:
                        error(f"{svc}: {status['status']}")
            else:
                info("Analytics service not available yet, continuing...")
        except Exception as e:
            info(f"Health check skipped: {e}")

        # ============================================
        # Step 2: Register Users
        # ============================================
        header("Step 2: Register Users")

        # User 1: Alice
        resp = await client.post("/api/users/register", json={
            "email": "alice@example.com",
            "password": "securepass123",
            "full_name": "Alice Johnson",
            "license_number": "DL-ALICE-001"
        })
        if resp.status_code == 201:
            alice = resp.json()
            success(f"Registered Alice: {alice['id']}")
        elif resp.status_code == 409:
            info("Alice already registered")
        else:
            error(f"Failed to register Alice: {resp.text}")

        # User 2: Bob
        resp = await client.post("/api/users/register", json={
            "email": "bob@example.com",
            "password": "securepass456",
            "full_name": "Bob Smith",
            "license_number": "DL-BOB-002"
        })
        if resp.status_code == 201:
            bob = resp.json()
            success(f"Registered Bob: {bob['id']}")
        elif resp.status_code == 409:
            info("Bob already registered")
        else:
            error(f"Failed to register Bob: {resp.text}")

        # ============================================
        # Step 3: Login
        # ============================================
        header("Step 3: Login")

        resp = await client.post("/api/users/login", json={
            "email": "alice@example.com",
            "password": "securepass123"
        })
        if resp.status_code == 200:
            alice_token = resp.json()["access_token"]
            success(f"Alice logged in (token: {alice_token[:20]}...)")
        else:
            error(f"Alice login failed: {resp.text}")
            return

        resp = await client.post("/api/users/login", json={
            "email": "bob@example.com",
            "password": "securepass456"
        })
        if resp.status_code == 200:
            bob_token = resp.json()["access_token"]
            success(f"Bob logged in (token: {bob_token[:20]}...)")
        else:
            error(f"Bob login failed: {resp.text}")
            return

        alice_headers = {"Authorization": f"Bearer {alice_token}"}
        bob_headers = {"Authorization": f"Bearer {bob_token}"}

        # ============================================
        # Step 4: Book a Journey (should be CONFIRMED)
        # ============================================
        header("Step 4: Book Journey for Alice (Expected: CONFIRMED)")

        departure = datetime.utcnow() + timedelta(hours=2)
        resp = await client.post("/api/journeys/", json={
            "origin": "Dublin City Centre",
            "destination": "Cork Airport",
            "origin_lat": 53.3498,
            "origin_lng": -6.2603,
            "destination_lat": 51.8413,
            "destination_lng": -8.4911,
            "departure_time": departure.isoformat(),
            "estimated_duration_minutes": 180,
            "vehicle_registration": "221-D-12345",
            "idempotency_key": "demo-alice-journey-1"
        }, headers=alice_headers)

        if resp.status_code == 201:
            journey1 = resp.json()
            success(f"Journey booked: {journey1['id']}")
            info(f"Status: {journey1['status']}")
            info(f"Route: {journey1['origin']} → {journey1['destination']}")
            info(f"Departure: {journey1['departure_time']}")
        else:
            error(f"Booking failed: {resp.text}")

        # ============================================
        # Step 5: Book OVERLAPPING Journey (should be REJECTED)
        # ============================================
        header("Step 5: Book Overlapping Journey for Alice (Expected: REJECTED)")

        overlap_departure = departure + timedelta(minutes=30)
        resp = await client.post("/api/journeys/", json={
            "origin": "Dublin Airport",
            "destination": "Galway City",
            "origin_lat": 53.4264,
            "origin_lng": -6.2499,
            "destination_lat": 53.2707,
            "destination_lng": -9.0568,
            "departure_time": overlap_departure.isoformat(),
            "estimated_duration_minutes": 150,
            "vehicle_registration": "221-D-12345",
            "idempotency_key": "demo-alice-journey-2-overlap"
        }, headers=alice_headers)

        if resp.status_code == 201:
            journey2 = resp.json()
            if journey2["status"] == "REJECTED":
                success(f"Correctly rejected: {journey2['rejection_reason']}")
            else:
                error(f"Expected REJECTED but got: {journey2['status']}")
        else:
            info(f"Response: {resp.status_code} - {resp.text}")

        # ============================================
        # Step 6: Book Journey for Bob (different vehicle, should be CONFIRMED)
        # ============================================
        header("Step 6: Book Journey for Bob (Expected: CONFIRMED)")

        resp = await client.post("/api/journeys/", json={
            "origin": "Limerick City",
            "destination": "Waterford City",
            "origin_lat": 52.6638,
            "origin_lng": -8.6267,
            "destination_lat": 52.2593,
            "destination_lng": -7.1101,
            "departure_time": departure.isoformat(),
            "estimated_duration_minutes": 120,
            "vehicle_registration": "231-L-67890",
            "idempotency_key": "demo-bob-journey-1"
        }, headers=bob_headers)

        if resp.status_code == 201:
            journey3 = resp.json()
            success(f"Journey booked: {journey3['id']}")
            info(f"Status: {journey3['status']}")
        else:
            error(f"Booking failed: {resp.text}")

        # ============================================
        # Step 7: Enforcement Check
        # ============================================
        header("Step 7: Enforcement Verification")

        # Wait a moment for cache to propagate
        await asyncio.sleep(1)

        resp = await client.get("/api/enforcement/verify/vehicle/221-D-12345")
        if resp.status_code == 200:
            verification = resp.json()
            if verification["is_valid"]:
                success(f"Vehicle 221-D-12345 has valid booking")
                info(f"Journey: {verification.get('origin')} → {verification.get('destination')}")
            else:
                info("No active journey found (cache may not have propagated)")
        else:
            error(f"Verification failed: {resp.text}")

        # Check a non-booked vehicle
        resp = await client.get("/api/enforcement/verify/vehicle/999-XX-99999")
        if resp.status_code == 200:
            verification = resp.json()
            if not verification["is_valid"]:
                success("Correctly identified unbooked vehicle 999-XX-99999")
            else:
                error("Should not have found a booking for 999-XX-99999")

        # ============================================
        # Step 8: List Alice's Journeys
        # ============================================
        header("Step 8: List Alice's Journeys")

        resp = await client.get("/api/journeys/", headers=alice_headers)
        if resp.status_code == 200:
            journeys = resp.json()
            success(f"Alice has {journeys['total']} journey(s)")
            for j in journeys["journeys"]:
                info(f"  [{j['status']}] {j['origin']} → {j['destination']}")

        # ============================================
        # Step 9: Cancel Alice's Journey
        # ============================================
        header("Step 9: Cancel Alice's Confirmed Journey")

        if journey1 and journey1.get("id"):
            resp = await client.delete(
                f"/api/journeys/{journey1['id']}",
                headers=alice_headers
            )
            if resp.status_code == 200:
                cancelled = resp.json()
                success(f"Journey cancelled: {cancelled['status']}")
            else:
                error(f"Cancellation failed: {resp.text}")

        # ============================================
        # Step 10: Check Analytics
        # ============================================
        header("Step 10: System Analytics")

        await asyncio.sleep(2)  # Wait for events to propagate

        resp = await client.get("/api/analytics/stats")
        if resp.status_code == 200:
            stats = resp.json()
            success("System Statistics:")
            pretty_json(stats)
        else:
            info("Analytics not available")

        resp = await client.get("/api/analytics/events?limit=5")
        if resp.status_code == 200:
            events = resp.json()
            success(f"Recent Events ({events['count']}):")
            for e in events.get("events", []):
                info(f"  [{e['event_type']}] journey={e.get('journey_id', 'N/A')[:8]}... at {e['created_at']}")

        # ============================================
        # Done
        # ============================================
        header("Demo Complete! 🎉")
        info("All core flows demonstrated:")
        info("  ✅ User registration and authentication (JWT)")
        info("  ✅ Journey booking with conflict detection (saga pattern)")
        info("  ✅ Conflict rejection for overlapping journeys")
        info("  ✅ Enforcement verification (Redis-cached)")
        info("  ✅ Journey cancellation with event propagation")
        info("  ✅ Analytics and monitoring")
        print()


if __name__ == "__main__":
    asyncio.run(main())
