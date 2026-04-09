import requests
import time
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings("ignore")

BASE = "http://localhost:8080"
ts = int(time.time())


# ---------- Helpers ----------
def safe_json(response):
    try:
        return response.json()
    except Exception:
        return {}


def print_result(name, condition, extra=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name} {extra}")


# ---------- Start Session ----------
session = requests.Session()
TIMEOUT = 5


# ---------- 1. Register driver ----------
email = f"driver_{ts}@test.com"

r = session.post(
    f"{BASE}/api/users/register",
    json={
        "email": email,
        "password": "Test1234!",
        "full_name": "Test Driver",
        "license_number": f"DRV{ts}",
    },
    timeout=TIMEOUT,
)

print_result("Register driver", r.status_code == 201, f"(HTTP {r.status_code})")


# ---------- 2. Login ----------
r = session.post(
    f"{BASE}/api/users/login",
    json={"email": email, "password": "Test1234!"},
    timeout=TIMEOUT,
)

if r.status_code != 200:
    print_result("Login", False, f"(HTTP {r.status_code})")
    exit()

token = safe_json(r).get("access_token")
print_result("Login", token is not None)

session.headers.update({"Authorization": f"Bearer {token}"})


# ---------- 3. Register vehicle ----------
vreg = f"DTS{ts%10000:04d}"

r = session.post(
    f"{BASE}/api/users/vehicles",
    json={
        "registration": vreg,
        "make": "Toyota",
        "model": "Corolla",
        "year": 2023,
        "vehicle_type": "CAR",
    },
    timeout=TIMEOUT,
)

print_result(
    f"Register vehicle {vreg}",
    r.status_code == 201,
    f"(HTTP {r.status_code})",
)


# ---------- 4. Book journey (Saga) ----------
dep = (datetime.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")

r = session.post(
    f"{BASE}/api/journeys/",
    json={
        "origin": "Dublin",
        "destination": "Cork",
        "origin_lat": 53.3498,
        "origin_lng": -6.2603,
        "destination_lat": 51.8985,
        "destination_lng": -8.4756,
        "departure_time": dep,
        "estimated_duration_minutes": 150,
        "vehicle_registration": vreg,
        "idempotency_key": f"saga-{ts}",
    },
    timeout=TIMEOUT,
)

j = safe_json(r)
j_id = j.get("id")
status = j.get("status")

print_result(
    "Book journey (Saga)",
    status in ["CONFIRMED", "PENDING"],
    f"(status={status})",
)


# ---------- 5. Idempotency ----------
r2 = session.post(
    f"{BASE}/api/journeys/",
    json={
        "origin": "Dublin",
        "destination": "Cork",
        "origin_lat": 53.3498,
        "origin_lng": -6.2603,
        "destination_lat": 51.8985,
        "destination_lng": -8.4756,
        "departure_time": dep,
        "estimated_duration_minutes": 150,
        "vehicle_registration": vreg,
        "idempotency_key": f"saga-{ts}",
    },
    timeout=TIMEOUT,
)

j2 = safe_json(r2)
same = j2.get("id") == j_id and j2.get("status") == status

print_result("Idempotency", same, f"(same_id={same})")


# ---------- 6. Conflict detection ----------
dep2 = (datetime.utcnow() + timedelta(hours=2, minutes=30)).strftime(
    "%Y-%m-%dT%H:%M:%S"
)

r3 = session.post(
    f"{BASE}/api/journeys/",
    json={
        "origin": "Cork",
        "destination": "Limerick",
        "origin_lat": 51.8985,
        "origin_lng": -8.4756,
        "destination_lat": 52.668,
        "destination_lng": -8.6305,
        "departure_time": dep2,
        "estimated_duration_minutes": 60,
        "vehicle_registration": vreg,
        "idempotency_key": f"conflict-{ts}",
    },
    timeout=TIMEOUT,
)

j3 = safe_json(r3)
conflict_status = j3.get("status")

conflict_ok = (
    conflict_status in ["REJECTED", "CONFLICT"]
    or r3.status_code == 409
)

print_result(
    "Conflict detection",
    conflict_ok,
    f"(status={conflict_status}, HTTP {r3.status_code})",
)


# ---------- 7. 2PC booking ----------
vreg2 = f"TPC{ts%10000:04d}"

r = session.post(
    f"{BASE}/api/users/vehicles",
    json={
        "registration": vreg2,
        "make": "BMW",
        "model": "X5",
        "year": 2024,
        "vehicle_type": "CAR",
    },
    timeout=TIMEOUT,
)

print_result("Register vehicle (2PC)", r.status_code == 201)

dep3 = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S")

r4 = session.post(
    f"{BASE}/api/journeys/?mode=2pc",
    json={
        "origin": "Galway",
        "destination": "Dublin",
        "origin_lat": 53.2707,
        "origin_lng": -9.0568,
        "destination_lat": 53.3498,
        "destination_lng": -6.2603,
        "departure_time": dep3,
        "estimated_duration_minutes": 120,
        "vehicle_registration": vreg2,
        "idempotency_key": f"2pc-{ts}",
    },
    timeout=TIMEOUT,
)

j4 = safe_json(r4)

print_result(
    "2PC booking",
    j4.get("status") in ["CONFIRMED", "PENDING"],
    f"(status={j4.get('status')})",
)


# ---------- 8. Register agent & enforcement ----------
agent_email = f"agent_{ts}@test.com"

session.post(
    f"{BASE}/api/users/register/agent",
    json={
        "email": agent_email,
        "password": "Agent1234!",
        "full_name": "Bob Agent",
        "license_number": f"AGT{ts}",
    },
    timeout=TIMEOUT,
)

r = session.post(
    f"{BASE}/api/users/login",
    json={"email": agent_email, "password": "Agent1234!"},
    timeout=TIMEOUT,
)

ag_token = safe_json(r).get("access_token")

if ag_token:
    agent_headers = {"Authorization": f"Bearer {ag_token}"}

    r5 = requests.get(
        f"{BASE}/api/enforcement/verify/vehicle/{vreg}",
        headers=agent_headers,
        timeout=TIMEOUT,
    )

    j5 = safe_json(r5)

    print_result(
        "Enforcement verify",
        r5.status_code == 200,
        f"(is_valid={j5.get('is_valid')})",
    )
else:
    print_result("Agent login", False)


# ---------- 9. Cancel journey ----------
if j_id:
    r6 = session.delete(
        f"{BASE}/api/journeys/{j_id}",
        timeout=TIMEOUT,
    )

    j6 = safe_json(r6)

    print_result(
        "Cancel journey",
        j6.get("status") == "CANCELLED",
        f"(status={j6.get('status')})",
    )
else:
    print_result("Cancel journey", False, "(no journey id)")


# ---------- 10. Analytics ----------
r7 = session.get(f"{BASE}/api/analytics/stats", timeout=TIMEOUT)

j7 = safe_json(r7)

valid = r7.status_code == 200 and isinstance(j7, dict)

print_result("Analytics", valid)