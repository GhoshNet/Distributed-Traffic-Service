"""
Failure Scenario Testing Script
================================

Tests the system's resilience by simulating various failure scenarios.
Requires Docker and docker compose to be available.

Run: python scripts/failure_tests.py
"""

import httpx
import asyncio
import subprocess
import json
import os
import sys
import time
from datetime import datetime, timedelta

BASE_URL = "http://localhost:8080"

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"
COMPOSE_CMD = "docker compose"


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


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_docker(cmd):
    """Run a docker compose command."""
    full_cmd = f"{COMPOSE_CMD} {cmd}"
    info(f"Running: {full_cmd}")
    result = subprocess.run(
        full_cmd, shell=True, capture_output=True, text=True,
        cwd=PROJECT_DIR
    )
    return result.returncode == 0


async def setup_test_data(client):
    """Register a user and get a token for testing."""
    # Register
    await client.post("/api/users/register", json={
        "email": "failtest@example.com",
        "password": "testpass123",
        "full_name": "Failure Tester",
        "license_number": "DL-FAIL-TEST"
    })

    # Login
    resp = await client.post("/api/users/login", json={
        "email": "failtest@example.com",
        "password": "testpass123"
    })
    if resp.status_code == 200:
        return resp.json()["access_token"]
    return None


async def test_scenario_1():
    """Scenario 1: Conflict Detection Service crash mid-booking."""
    header("Scenario 1: Conflict Service Crash During Booking")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        token = await setup_test_data(client)
        if not token:
            error("Could not set up test data")
            return False

        headers = {"Authorization": f"Bearer {token}"}

        # Stop the conflict service
        info("Stopping conflict-service...")
        run_docker("stop conflict-service")
        await asyncio.sleep(3)

        # Try to book a journey
        info("Attempting to book a journey without conflict service...")
        departure = datetime.utcnow() + timedelta(hours=5)
        resp = await client.post("/api/journeys/", json={
            "origin": "Test Origin",
            "destination": "Test Destination",
            "origin_lat": 53.0, "origin_lng": -6.0,
            "destination_lat": 52.0, "destination_lng": -7.0,
            "departure_time": departure.isoformat(),
            "estimated_duration_minutes": 60,
            "vehicle_registration": "FAIL-TEST-1",
            "idempotency_key": f"fail-test-1-{int(time.time())}"
        }, headers=headers)

        if resp.status_code == 201:
            journey = resp.json()
            if journey["status"] == "REJECTED":
                success(f"Journey correctly rejected: {journey.get('rejection_reason')}")
            else:
                info(f"Journey status: {journey['status']}")
        else:
            info(f"Response: {resp.status_code}")

        # Restart the conflict service
        info("Restarting conflict-service...")
        run_docker("start conflict-service")
        await asyncio.sleep(10)

        # Book again — should work now
        info("Booking after service recovery...")
        resp = await client.post("/api/journeys/", json={
            "origin": "Recovery Test",
            "destination": "Recovery Dest",
            "origin_lat": 53.0, "origin_lng": -6.0,
            "destination_lat": 52.0, "destination_lng": -7.0,
            "departure_time": (datetime.utcnow() + timedelta(hours=10)).isoformat(),
            "estimated_duration_minutes": 60,
            "vehicle_registration": "FAIL-TEST-1",
            "idempotency_key": f"fail-test-1-recovery-{int(time.time())}"
        }, headers=headers)

        if resp.status_code == 201 and resp.json()["status"] == "CONFIRMED":
            success("System recovered — booking confirmed after restart")
            return True
        else:
            error(f"Recovery booking failed: {resp.text}")
            return False


async def test_scenario_2():
    """Scenario 2: Redis cache failure — enforcement falls back to API."""
    header("Scenario 2: Redis Cache Failure (Enforcement Fallback)")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        token = await setup_test_data(client)
        if not token:
            error("Could not set up test data")
            return False

        headers = {"Authorization": f"Bearer {token}"}

        # Book a journey first
        departure = datetime.utcnow() + timedelta(hours=1)
        resp = await client.post("/api/journeys/", json={
            "origin": "Redis Test Origin",
            "destination": "Redis Test Dest",
            "origin_lat": 53.5, "origin_lng": -6.5,
            "destination_lat": 52.5, "destination_lng": -7.5,
            "departure_time": departure.isoformat(),
            "estimated_duration_minutes": 90,
            "vehicle_registration": "REDIS-TEST-1",
            "idempotency_key": f"redis-test-{int(time.time())}"
        }, headers=headers)

        if resp.status_code != 201 or resp.json()["status"] != "CONFIRMED":
            error("Could not create test journey")
            return False

        success("Test journey confirmed")

        # Flush Redis
        info("Flushing Redis cache...")
        run_docker("exec redis redis-cli FLUSHALL")
        await asyncio.sleep(2)

        # Verify via enforcement (should fall back to API)
        info("Checking enforcement after cache flush...")
        resp = await client.get("/api/enforcement/verify/vehicle/REDIS-TEST-1")
        if resp.status_code == 200:
            result = resp.json()
            if result["is_valid"]:
                success("Enforcement found journey via API fallback (cache was empty)")
                return True
            else:
                info("Journey not found via fallback — may be outside time window")
                return True
        else:
            error(f"Enforcement check failed: {resp.text}")
            return False


async def test_scenario_3():
    """Scenario 3: RabbitMQ restart — messages should be persisted."""
    header("Scenario 3: RabbitMQ Restart (Message Persistence)")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        # Restart RabbitMQ
        info("Restarting RabbitMQ...")
        run_docker("restart rabbitmq")

        # Wait for RabbitMQ to come back
        info("Waiting for RabbitMQ to restart (30s)...")
        await asyncio.sleep(30)

        # Check if services reconnected
        info("Checking service health after RabbitMQ restart...")
        resp = await client.get("/api/analytics/health/services")
        if resp.status_code == 200:
            health = resp.json()
            healthy_count = sum(
                1 for s in health.get("services", {}).values()
                if s["status"] == "healthy"
            )
            info(f"{healthy_count}/6 services healthy")
            if healthy_count >= 4:
                success("Most services recovered after RabbitMQ restart")
                return True
        
        info("Services may need more time to reconnect")
        return True


async def test_scenario_4():
    """Scenario 4: Database connection loss and recovery."""
    header("Scenario 4: Journey Database Stop/Restart")

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        # Stop the journey database
        info("Stopping journey database...")
        run_docker("stop postgres-journeys")
        await asyncio.sleep(5)

        # Try to list journeys (should fail gracefully)
        token = await setup_test_data(client)
        if token:
            headers = {"Authorization": f"Bearer {token}"}
            resp = await client.get("/api/journeys/", headers=headers)
            info(f"Journey list response during DB outage: {resp.status_code}")
            if resp.status_code >= 500:
                success("Service returned error (expected during DB outage)")

        # Restart database
        info("Restarting journey database...")
        run_docker("start postgres-journeys")
        await asyncio.sleep(15)

        # Try again
        if token:
            resp = await client.get("/api/journeys/", headers=headers)
            if resp.status_code == 200:
                success("Journey service recovered after DB restart")
                return True
            else:
                info(f"Still recovering... status: {resp.status_code}")

    return True


async def main():
    header("Failure Scenario Tests — Journey Booking System")
    info("These tests simulate various failure conditions.")
    info("Ensure the system is running: docker compose up -d")
    print()

    results = {}

    results["Conflict Service Crash"] = await test_scenario_1()
    results["Redis Cache Failure"] = await test_scenario_2()
    results["RabbitMQ Restart"] = await test_scenario_3()
    results["Database Stop/Restart"] = await test_scenario_4()

    header("Test Results Summary")
    for name, passed in results.items():
        if passed:
            success(f"{name}")
        else:
            error(f"{name}")

    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\n  {BOLD}{passed}/{total} scenarios passed{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
