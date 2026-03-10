"""
Load Testing Script - Journey Booking System
=============================================

Simulates concurrent users booking journeys to test system throughput.

Run: python scripts/load_test.py [--users N] [--duration S]
"""

import httpx
import asyncio
import argparse
import random
import time
import statistics
from datetime import datetime, timedelta

BASE_URL = "http://localhost:8080"


class LoadTester:
    def __init__(self, base_url: str, num_users: int, duration_seconds: int):
        self.base_url = base_url
        self.num_users = num_users
        self.duration = duration_seconds
        self.results = {
            "total_requests": 0,
            "successful": 0,
            "failed": 0,
            "confirmed": 0,
            "rejected": 0,
            "latencies": [],
        }
        self.running = True
        self.tokens = []

    async def setup(self):
        """Register users and get tokens."""
        print(f"Setting up {self.num_users} test users...")
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            for i in range(self.num_users):
                email = f"loadtest_{i}_{int(time.time())}@test.com"
                license_num = f"LT-{i}-{int(time.time())}"

                # Register
                await client.post("/api/users/register", json={
                    "email": email,
                    "password": "loadtest123",
                    "full_name": f"Load Test User {i}",
                    "license_number": license_num,
                })

                # Login
                resp = await client.post("/api/users/login", json={
                    "email": email,
                    "password": "loadtest123",
                })
                if resp.status_code == 200:
                    self.tokens.append(resp.json()["access_token"])

        print(f"  {len(self.tokens)} users ready")

    async def simulate_user(self, user_id: int, token: str):
        """Simulate a single user making booking requests."""
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            headers = {"Authorization": f"Bearer {token}"}
            request_num = 0

            while self.running:
                request_num += 1
                departure = datetime.utcnow() + timedelta(
                    hours=random.randint(1, 48),
                    minutes=random.randint(0, 59),
                )

                # Random coordinates in Ireland
                origin_lat = random.uniform(51.4, 55.4)
                origin_lng = random.uniform(-10.5, -5.5)
                dest_lat = random.uniform(51.4, 55.4)
                dest_lng = random.uniform(-10.5, -5.5)

                payload = {
                    "origin": f"Location {random.randint(1, 100)}",
                    "destination": f"Destination {random.randint(1, 100)}",
                    "origin_lat": origin_lat,
                    "origin_lng": origin_lng,
                    "destination_lat": dest_lat,
                    "destination_lng": dest_lng,
                    "departure_time": departure.isoformat(),
                    "estimated_duration_minutes": random.randint(15, 300),
                    "vehicle_registration": f"LT-{user_id}-{random.randint(1, 3)}",
                    "idempotency_key": f"lt-{user_id}-{request_num}-{int(time.time()*1000)}",
                }

                start = time.time()
                try:
                    resp = await client.post("/api/journeys/", json=payload, headers=headers)
                    latency = (time.time() - start) * 1000  # ms

                    self.results["total_requests"] += 1
                    self.results["latencies"].append(latency)

                    if resp.status_code == 201:
                        self.results["successful"] += 1
                        status = resp.json().get("status", "UNKNOWN")
                        if status == "CONFIRMED":
                            self.results["confirmed"] += 1
                        else:
                            self.results["rejected"] += 1
                    else:
                        self.results["failed"] += 1

                except Exception:
                    self.results["failed"] += 1
                    self.results["total_requests"] += 1

                # Small random delay between requests
                await asyncio.sleep(random.uniform(0.1, 1.0))

    async def run(self):
        """Run the load test."""
        await self.setup()

        if not self.tokens:
            print("No tokens available, aborting")
            return

        print(f"\nStarting load test: {self.num_users} users for {self.duration}s")
        print(f"{'='*50}")

        # Start user simulations
        tasks = []
        for i, token in enumerate(self.tokens):
            tasks.append(asyncio.create_task(self.simulate_user(i, token)))

        # Run for specified duration
        await asyncio.sleep(self.duration)
        self.running = False

        # Wait for tasks to complete
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        self.print_results()

    def print_results(self):
        """Print load test results."""
        print(f"\n{'='*50}")
        print(f"  LOAD TEST RESULTS")
        print(f"{'='*50}")
        print(f"  Duration:         {self.duration}s")
        print(f"  Concurrent Users: {self.num_users}")
        print(f"  Total Requests:   {self.results['total_requests']}")
        print(f"  Successful:       {self.results['successful']}")
        print(f"  Failed:           {self.results['failed']}")
        print(f"  Confirmed:        {self.results['confirmed']}")
        print(f"  Rejected:         {self.results['rejected']}")

        if self.results["latencies"]:
            latencies = self.results["latencies"]
            print(f"\n  Latency Statistics:")
            print(f"    Min:    {min(latencies):.0f}ms")
            print(f"    Max:    {max(latencies):.0f}ms")
            print(f"    Mean:   {statistics.mean(latencies):.0f}ms")
            print(f"    Median: {statistics.median(latencies):.0f}ms")
            if len(latencies) > 1:
                p95 = sorted(latencies)[int(len(latencies) * 0.95)]
                p99 = sorted(latencies)[int(len(latencies) * 0.99)]
                print(f"    P95:    {p95:.0f}ms")
                print(f"    P99:    {p99:.0f}ms")

            rps = self.results["total_requests"] / self.duration
            print(f"\n  Throughput: {rps:.1f} requests/second")

        print(f"{'='*50}\n")


async def main():
    parser = argparse.ArgumentParser(description="Load test the Journey Booking System")
    parser.add_argument("--users", type=int, default=10, help="Number of concurrent users")
    parser.add_argument("--duration", type=int, default=30, help="Test duration in seconds")
    parser.add_argument("--url", type=str, default=BASE_URL, help="Base URL")
    args = parser.parse_args()

    tester = LoadTester(args.url, args.users, args.duration)
    await tester.run()


if __name__ == "__main__":
    asyncio.run(main())
