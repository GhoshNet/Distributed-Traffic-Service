# Journey Booking System — Distributed Microservices

> **CS7NS6 Distributed Systems — Exercise 2 (Group J)**
>
> A globally-accessible, fault-tolerant system that lets road-vehicle drivers pre-book journeys before they travel. No driver may start a journey without a confirmed booking. The system checks for scheduling conflicts, notifies drivers in real time, and allows roadside enforcement agents to verify active bookings instantly.

---

## Table of Contents

1. [What This System Does](#1-what-this-system-does)
2. [System Architecture](#2-system-architecture)
3. [How Each Service Works](#3-how-each-service-works)
4. [Distributed Systems Techniques](#4-distributed-systems-techniques)
5. [Project File Structure](#5-project-file-structure)
6. [Requirements](#6-requirements)
7. [Setup — Local (no Docker)](#7-setup--local-no-docker)
8. [Setup — Docker](#8-setup--docker)
9. [Running the Demo](#9-running-the-demo)
10. [API Reference](#10-api-reference)
11. [Testing & Failure Scenarios](#11-testing--failure-scenarios)
12. [Load Testing](#12-load-testing)
13. [How Booking Works End-to-End](#13-how-booking-works-end-to-end)

---

## 1. What This System Does

Imagine a world where every car journey must be booked in advance, like an airline ticket. This system is the backend that makes that possible, at global scale.

**Core flows:**

| Who | What they can do |
|-----|-----------------|
| Driver | Register an account, log in, book a journey, cancel a journey, receive notifications |
| System | Check for scheduling conflicts, reject double-bookings, track road capacity by area |
| Enforcement agent | Scan a vehicle plate or driving licence and instantly see if the driver has a valid active booking |
| Operator | View real-time statistics, event history, and health status of all services |

---

## 2. System Architecture

```
                    ┌─────────────────────────────┐
                    │    Client (Web / Mobile)     │
                    └──────────────┬──────────────┘
                                   │ HTTP / WebSocket
                                   ▼
                    ┌──────────────────────────────┐
                    │   Nginx API Gateway (:8080)   │  ← Docker mode only
                    │  Rate limiting · Routing      │
                    └──┬──────┬──────┬──────┬──────┘
                       │      │      │      │
              ┌────────┘  ┌───┘  ┌───┘  ┌──┘
              ▼           ▼      ▼      ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
        │  User    │ │ Journey  │ │ Conflict │ │Notificat-│ │Enforcemt │ │Analytics │
        │ Service  │ │ Service  │ │ Service  │ │ion Svc   │ │ Service  │ │ Service  │
        │  :8001   │ │  :8002   │ │  :8003   │ │  :8004   │ │  :8005   │ │  :8006   │
        └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
             │            │   REST      │             │             │             │
             │            └────────────┘             │             │             │
             ▼            │              ┌────────────┴─────────────┴─────────────┘
        ┌─────────┐       ▼              ▼
        │Postgres │  ┌─────────┐   ┌──────────────────────────┐
        │users_db │  │Postgres │   │       RabbitMQ           │
        └─────────┘  │jrny_db  │   │  (Topic exchange)        │
                     └─────────┘   │  journey_events          │
                     ┌─────────┐   └──────────────────────────┘
                     │Postgres │        ▲ publish       │ consume
                     │cnflt_db │        │               ▼
                     └─────────┘   Journey Svc   Notification Svc
                     ┌─────────┐               + Conflict Svc
                     │Postgres │               + Analytics Svc
                     │anlyt_db │               + Enforcement Svc
                     └─────────┘
                     ┌─────────┐
                     │  Redis  │  ← active journey cache (enforcement)
                     └─────────┘    + notification history
                                    + analytics counters
```

**Communication patterns:**

- **Synchronous (REST):** Journey Service calls Conflict Service during booking to check for overlaps. Enforcement Service calls Journey Service as a cache-miss fallback.
- **Asynchronous (RabbitMQ):** After a booking is confirmed, rejected, or cancelled, Journey Service publishes an event. Downstream services (Notification, Analytics, Conflict, Enforcement) consume it in the background.

---

## 3. How Each Service Works

### User Service (`user-service/`, port 8001)
Handles driver accounts. Stores users in PostgreSQL with bcrypt-hashed passwords. Issues JWT tokens on login that are used by all other services to authenticate requests.

### Journey Service (`journey-service/`, port 8002)
The heart of the system. When a driver requests a booking:
1. Creates the journey with status `PENDING` in the database
2. Calls the Conflict Service synchronously (the "saga")
3. If no conflict → marks `CONFIRMED`, caches the journey in Redis, publishes a `journey.confirmed` event
4. If conflict or timeout → marks `REJECTED`, publishes a `journey.rejected` event

Also handles cancellations and idempotency (safe retries using client-generated keys).

### Conflict Detection Service (`conflict-service/`, port 8003)
Called by the Journey Service for every booking. Runs three checks:
1. **Driver time overlap** — Does the same driver have another journey at this time?
2. **Vehicle time overlap** — Is the same vehicle already booked at this time?
3. **Road capacity** — Does the origin or destination grid cell (~1 km²) exceed 100 bookings in the departure/arrival 30-minute slot?

Also consumes `journey.cancelled` events from RabbitMQ to free up the booking slot.

### Notification Service (`notification-service/`, port 8004)
Consumes journey events from RabbitMQ and delivers notifications to drivers:
- Stores the last 50 notifications per user in Redis (7-day TTL)
- Pushes real-time updates to connected WebSocket clients
- Exposes a REST endpoint to retrieve notification history

### Enforcement Service (`enforcement-service/`, port 8005)
Designed for sub-500ms responses at roadside checks. Uses a two-layer lookup:
1. **Redis cache (primary)** — Journey Service writes active journeys here on confirmation. Sub-millisecond lookup by vehicle registration or user ID.
2. **Journey Service API fallback** — On cache miss (e.g. after a Redis flush), queries Journey Service directly. Re-populates cache on hit.

Supports lookup by vehicle registration plate OR driving licence number.

### Analytics & Monitoring Service (`analytics-service/`, port 8006)
Consumes all events from RabbitMQ and:
- Logs every event to PostgreSQL for historical analysis
- Maintains real-time daily counters in Redis (confirmed, rejected, cancelled today)
- Exposes an aggregated health dashboard that pings all 6 services

---

## 4. Distributed Systems Techniques

| Technique | Implementation |
|-----------|---------------|
| **Saga Pattern** | Journey Service orchestrates a two-step booking: create PENDING → call Conflict Service → confirm or reject. If Conflict Service is unreachable (30s timeout), the saga compensates by rejecting the booking. The driver can safely retry. |
| **Database-per-Service** | Each stateful service owns its own isolated PostgreSQL database. Services cannot directly query each other's data — they communicate only through APIs and events. |
| **Event-Driven Messaging** | RabbitMQ topic exchange (`journey_events`). Journey Service publishes; Notification, Analytics, Conflict, and Enforcement Services subscribe. Messages are persistent (survive broker restart). |
| **Dead-Letter Queue** | Failed messages (after processing errors) are routed to `dead_letter_queue` via `journey_events_dlx`. Keeps the main queue clean and allows manual inspection. |
| **Redis Caching** | Active journeys cached by vehicle registration and user ID. TTL = journey arrival time + 1 hour. Enforcement Service reads cache first; falls back to API on miss. |
| **Idempotency Keys** | Clients can send a unique key with each booking request. If the same key arrives twice (network retry), the same journey is returned without creating a duplicate. |
| **Rate Limiting** | Nginx API Gateway enforces per-IP limits: 5 req/s on auth endpoints, 10 req/s on booking, 30 req/s general. Protects against abuse. |
| **Load Balancing** | Nginx round-robin across service replicas (Docker mode). Unhealthy instances removed based on `/health` checks. |
| **Geographic Partitioning** | Road capacity is tracked per ~1 km² grid cell (0.01° lat/lng resolution) per 30-minute time slot. Allows regional capacity checks to scale independently. |
| **Health Checks** | Every service exposes `GET /health`. Docker uses this for automatic container restart. Analytics Service aggregates all health statuses into a single dashboard. |
| **Crash-Recovery Model** | All state is in durable databases. Services can crash and restart at any time with no data loss. RabbitMQ consumers auto-reconnect on restart. |

---

## 5. Project File Structure

```
Excercise2/
├── api-gateway/
│   └── nginx.conf              # Nginx routing, rate limits, WebSocket proxy
│
├── user-service/
│   ├── app/
│   │   ├── main.py             # FastAPI app, lifespan (DB init, RabbitMQ connect)
│   │   ├── routes.py           # HTTP endpoints: /register, /login, /me, /license/{n}
│   │   ├── service.py          # Business logic: register, login, get profile
│   │   ├── database.py         # SQLAlchemy User model + async engine
│   │   └── __init__.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── journey-service/
│   ├── app/
│   │   ├── main.py             # FastAPI app
│   │   ├── routes.py           # POST/GET/DELETE journeys, vehicle active lookup
│   │   ├── service.py          # CRUD, Redis caching, idempotency
│   │   ├── saga.py             # BookingSaga: conflict check + event publishing
│   │   ├── database.py         # Journey + IdempotencyRecord models
│   │   └── __init__.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── conflict-service/
│   ├── app/
│   │   ├── main.py             # FastAPI app + starts RabbitMQ consumer
│   │   ├── routes.py           # POST /check, POST /cancel/{id}
│   │   ├── service.py          # 3 conflict checks + slot recording
│   │   ├── consumer.py         # Listens for journey.cancelled → deactivates slots
│   │   ├── database.py         # BookedSlot + RoadSegmentCapacity models
│   │   └── __init__.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── notification-service/
│   ├── app/
│   │   ├── main.py             # FastAPI app + WebSocket endpoint + REST history
│   │   ├── consumer.py         # RabbitMQ consumer → Redis store + WebSocket push
│   │   └── __init__.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── enforcement-service/
│   ├── app/
│   │   ├── main.py             # FastAPI app + 2 verify endpoints
│   │   ├── service.py          # Redis-first lookup + Journey Service fallback
│   │   └── __init__.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── analytics-service/
│   ├── app/
│   │   ├── main.py             # FastAPI app + stats/events/health endpoints
│   │   ├── consumer.py         # Consumes all events → PostgreSQL + Redis counters
│   │   ├── database.py         # EventLog + HourlyStats models
│   │   └── __init__.py
│   ├── Dockerfile
│   └── requirements.txt
│
├── shared/                     # Shared library (copied into each Docker image)
│   ├── __init__.py
│   ├── auth.py                 # JWT create/decode, FastAPI dependency
│   ├── config.py               # Logging setup, Settings base class
│   ├── messaging.py            # RabbitMQ client (connect, publish, subscribe, DLQ)
│   └── schemas.py              # All Pydantic models shared between services
│
├── scripts/
│   ├── run_local.sh            # Start/stop/status all 6 services locally
│   ├── demo_local.py           # End-to-end demo using direct ports (local mode)
│   ├── demo.py                 # End-to-end demo using API Gateway (Docker mode)
│   ├── failure_tests.py        # 4 failure scenario tests (Docker mode)
│   └── load_test.py            # Concurrent load test with latency stats
│
├── docker-compose.yml          # Full orchestration: 6 services + 4 DBs + Redis + RabbitMQ
├── docker-compose.test.yml     # Override: faster health checks for failure testing
└── README.md                   # This file
```

---

## 6. Requirements

### Python packages (same across all services)

Each service has its own `requirements.txt`. Here is the complete combined list:

```
fastapi==0.115.0
uvicorn[standard]==0.30.6
sqlalchemy[asyncio]==2.0.35
asyncpg==0.30.0
alembic==1.13.3
pydantic==2.9.2
pydantic-settings==2.5.2
passlib[bcrypt]==1.7.4
bcrypt==4.2.0
PyJWT==2.9.0
redis==5.1.1
aio-pika==9.4.3
httpx==0.27.2
python-multipart==0.0.12
websockets==13.1
```

> **Note:** `passlib`, `bcrypt`, and `alembic` are only used by the services that need them, but installing all packages in one conda environment is the simplest approach.

### Infrastructure

| Service | Version | Purpose |
|---------|---------|---------|
| PostgreSQL | 16 | Persistent storage for Users, Journeys, Conflicts, Analytics |
| Redis | 7 | Active journey cache, notification history, analytics counters |
| RabbitMQ | 3.13 | Async message broker between services |
| Nginx | 1.25 | API Gateway — routing, rate limiting, load balancing (Docker only) |
| Python | 3.12 | All 6 microservices |

---

## 7. Setup — Local (no Docker)

This runs all 6 services as native Python processes. Requires macOS with Homebrew. For Linux, replace `brew` commands with `apt` equivalents.

### Step 1 — Create the conda environment

```bash
conda create -n DS python=3.12 -y
conda activate DS
```

### Step 2 — Install infrastructure with Homebrew

```bash
brew install postgresql@16 redis rabbitmq
```

Start the services (and configure them to restart automatically at login):

```bash
brew services start postgresql@16
brew services start redis
brew services start rabbitmq
```

Verify they are running:

```bash
brew services list
# postgresql@16   started
# redis           started
# rabbitmq        started
```

### Step 3 — Create PostgreSQL databases and users

Each microservice gets its own isolated database. Run this once:

```bash
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"

psql -U $(whoami) postgres << 'EOF'
CREATE USER users_user     WITH PASSWORD 'users_pass';
CREATE USER journeys_user  WITH PASSWORD 'journeys_pass';
CREATE USER conflicts_user WITH PASSWORD 'conflicts_pass';
CREATE USER analytics_user WITH PASSWORD 'analytics_pass';

CREATE DATABASE users_db     OWNER users_user;
CREATE DATABASE journeys_db  OWNER journeys_user;
CREATE DATABASE conflicts_db OWNER conflicts_user;
CREATE DATABASE analytics_db OWNER analytics_user;

GRANT ALL PRIVILEGES ON DATABASE users_db     TO users_user;
GRANT ALL PRIVILEGES ON DATABASE journeys_db  TO journeys_user;
GRANT ALL PRIVILEGES ON DATABASE conflicts_db TO conflicts_user;
GRANT ALL PRIVILEGES ON DATABASE analytics_db TO analytics_user;
EOF
```

### Step 4 — Configure RabbitMQ

Create a dedicated virtual host and admin user (run this once):

```bash
export PATH="/opt/homebrew/opt/rabbitmq/sbin:$PATH"

rabbitmqctl add_vhost journey_vhost
rabbitmqctl add_user journey_admin journey_pass
rabbitmqctl set_permissions -p journey_vhost journey_admin ".*" ".*" ".*"
rabbitmqctl set_user_tags journey_admin administrator
```

You can verify via the RabbitMQ Management UI at [http://localhost:15672](http://localhost:15672) (login: `journey_admin` / `journey_pass`).

### Step 5 — Install Python dependencies

```bash
conda run -n DS pip install \
  fastapi==0.115.0 \
  "uvicorn[standard]==0.30.6" \
  "sqlalchemy[asyncio]==2.0.35" \
  asyncpg==0.30.0 \
  alembic==1.13.3 \
  "pydantic==2.9.2" \
  "pydantic-settings==2.5.2" \
  "passlib[bcrypt]==1.7.4" \
  "bcrypt==4.2.0" \
  PyJWT==2.9.0 \
  "redis==5.1.1" \
  "aio-pika==9.4.3" \
  httpx==0.27.2 \
  python-multipart==0.0.12 \
  websockets==13.1
```

### Step 6 — Start all 6 services

From the project root:

```bash
bash scripts/run_local.sh start
```

This starts each service in the background (logs written to `logs/<service-name>.log`). You should see:

```
Starting Journey Booking System (local mode)...
  [user-service]        Starting on port 8001
  [conflict-service]    Starting on port 8003
  [analytics-service]   Starting on port 8006
  [notification-service] Starting on port 8004
  [enforcement-service] Starting on port 8005
  [journey-service]     Starting on port 8002
```

### Step 7 — Verify everything is healthy

```bash
bash scripts/run_local.sh status
```

Or check each service manually:

```bash
curl http://localhost:8001/health   # user-service
curl http://localhost:8002/health   # journey-service
curl http://localhost:8003/health   # conflict-service
curl http://localhost:8004/health   # notification-service
curl http://localhost:8005/health   # enforcement-service
curl http://localhost:8006/health   # analytics-service
```

Each should return `{"status": "healthy", ...}`.

### Step 8 — Run the demo

```bash
conda run -n DS python scripts/demo_local.py
```

### Stopping everything

```bash
bash scripts/run_local.sh stop
```

### Viewing logs

```bash
bash scripts/run_local.sh logs user-service      # tail user-service log
bash scripts/run_local.sh logs journey-service   # tail journey-service log
# etc.

# Or view all logs at once:
tail -f logs/*.log
```

### Restarting a single service (e.g. after a code change)

```bash
bash scripts/run_local.sh stop
# Make your changes
bash scripts/run_local.sh start
```

---

## 8. Setup — Docker

If Docker Desktop is installed, the entire system (including all databases, Redis, and RabbitMQ) runs in containers automatically. No manual database setup is needed.

### Start everything

```bash
cd Excercise2
docker compose up --build -d
```

Wait about 30 seconds for all services to pass their health checks:

```bash
docker compose ps
# All services should show "healthy"
```

### Check logs

```bash
docker compose logs -f journey-service     # Follow journey-service logs
docker compose logs --tail=50 conflict-service
```

### Stop

```bash
docker compose down        # Stop containers (data is preserved in volumes)
docker compose down -v     # Stop containers AND delete all data
```

---

## 9. Running the Demo

The demo exercises the full booking lifecycle automatically.

### Local mode

```bash
conda run -n DS python scripts/demo_local.py
```

### Docker mode

```bash
conda run -n DS python scripts/demo.py
```

### What the demo does (11 steps)

| Step | Action | Expected result |
|------|--------|----------------|
| 1 | Health check all services | All 6 show healthy |
| 2 | Register Alice and Bob | 201 Created |
| 3 | Login as Alice and Bob | JWT tokens returned |
| 4 | Alice books Dublin → Cork (vehicle 221-D-12345) | Status: CONFIRMED |
| 5 | Alice tries to book again with same vehicle, overlapping time | Status: REJECTED (conflict detected) |
| 6 | Bob books Limerick → Waterford (different vehicle) | Status: CONFIRMED |
| 7 | Enforcement verifies vehicle 221-D-12345 | Valid booking found |
| 8 | Enforcement checks a non-booked vehicle (999-XX-99999) | No active booking |
| 9 | Alice lists her journeys | 2 journeys shown (1 confirmed, 1 rejected) |
| 10 | Alice cancels her confirmed journey | Status: CANCELLED |
| 11 | Check notifications and analytics | 3 notifications + event stats |

> **Note on enforcement timing:** Enforcement checks only return a valid booking if the journey departs within the next 30 minutes, or is already in progress. A journey booked 2 hours in advance will not show as "active" until close to its departure time. This is intentional — enforcement checks are for vehicles currently on the road.

> **Note on re-running the demo:** The demo uses unique idempotency keys on each run, so it creates new journeys every time. Vehicle bookings from previous runs persist in the Conflict Service database. If you re-run the demo and see Bob's journey rejected, it is because that vehicle already has a confirmed booking from the previous run at the same departure time — which is exactly the conflict detection working correctly.

---

## 10. API Reference

### Local mode base URLs

| Service | Base URL |
|---------|----------|
| User Service | `http://localhost:8001` |
| Journey Service | `http://localhost:8002` |
| Conflict Service | `http://localhost:8003` |
| Notification Service | `http://localhost:8004` |
| Enforcement Service | `http://localhost:8005` |
| Analytics Service | `http://localhost:8006` |

### Docker mode base URL

All routes go through the API Gateway: `http://localhost:8080`

---

### User Service

#### Register a driver
```http
POST /api/users/register
Content-Type: application/json

{
  "email": "alice@example.com",
  "password": "securepass123",
  "full_name": "Alice Johnson",
  "license_number": "DL-ALICE-001"
}
```
Response `201`:
```json
{
  "id": "uuid",
  "email": "alice@example.com",
  "full_name": "Alice Johnson",
  "license_number": "DL-ALICE-001",
  "created_at": "2026-03-10T18:00:00"
}
```

#### Login
```http
POST /api/users/login
Content-Type: application/json

{
  "email": "alice@example.com",
  "password": "securepass123"
}
```
Response `200`:
```json
{
  "access_token": "eyJhbGci...",
  "token_type": "bearer",
  "expires_in": 86400
}
```

#### Get your profile
```http
GET /api/users/me
Authorization: Bearer <token>
```

---

### Journey Service

#### Book a journey
```http
POST /api/journeys/
Authorization: Bearer <token>
Content-Type: application/json

{
  "origin": "Dublin City Centre",
  "destination": "Cork Airport",
  "origin_lat": 53.3498,
  "origin_lng": -6.2603,
  "destination_lat": 51.8413,
  "destination_lng": -8.4911,
  "departure_time": "2026-03-10T20:00:00",
  "estimated_duration_minutes": 180,
  "vehicle_registration": "221-D-12345",
  "idempotency_key": "my-unique-key-001"
}
```
Response `201`:
```json
{
  "id": "uuid",
  "status": "CONFIRMED",
  "origin": "Dublin City Centre",
  "destination": "Cork Airport",
  "departure_time": "2026-03-10T20:00:00",
  "estimated_arrival_time": "2026-03-10T23:00:00",
  "vehicle_registration": "221-D-12345",
  "rejection_reason": null
}
```
If a conflict is detected, `status` will be `"REJECTED"` and `rejection_reason` will explain why.

#### List my journeys
```http
GET /api/journeys/
Authorization: Bearer <token>
```
Optional query params: `?status=CONFIRMED`, `?page=1`, `?page_size=20`

#### Get a specific journey
```http
GET /api/journeys/{journey_id}
Authorization: Bearer <token>
```

#### Cancel a journey
```http
DELETE /api/journeys/{journey_id}
Authorization: Bearer <token>
```

---

### Enforcement Service

#### Verify by vehicle registration
```http
GET /api/enforcement/verify/vehicle/221-D-12345
```
Response:
```json
{
  "is_valid": true,
  "driver_id": "uuid",
  "journey_id": "uuid",
  "journey_status": "CONFIRMED",
  "origin": "Dublin City Centre",
  "destination": "Cork Airport",
  "departure_time": "2026-03-10T20:00:00",
  "estimated_arrival_time": "2026-03-10T23:00:00",
  "checked_at": "2026-03-10T19:55:00"
}
```
If no active booking: `{ "is_valid": false, "checked_at": "..." }`

#### Verify by driving licence number
```http
GET /api/enforcement/verify/license/DL-ALICE-001
```
Same response format as above.

---

### Analytics Service

#### System statistics
```http
GET /api/analytics/stats
```
Response:
```json
{
  "total_events_today": 12,
  "confirmed_today": 4,
  "rejected_today": 5,
  "cancelled_today": 3,
  "total_events_all_time": 12,
  "events_last_hour": 12
}
```

#### Event history
```http
GET /api/analytics/events?limit=20&event_type=journey.confirmed
```

#### All services health dashboard
```http
GET /api/analytics/health/services
```
Response:
```json
{
  "overall_status": "healthy",
  "services": {
    "user-service":        { "status": "healthy", "response_time_ms": 9 },
    "journey-service":     { "status": "healthy", "response_time_ms": 4 },
    "conflict-service":    { "status": "healthy", "response_time_ms": 4 },
    "notification-service":{ "status": "healthy", "response_time_ms": 3 },
    "enforcement-service": { "status": "healthy", "response_time_ms": 2 },
    "analytics-service":   { "status": "healthy", "response_time_ms": 1 }
  }
}
```

---

### Notification Service

#### Get notification history
```http
GET /api/notifications/?token=<JWT>&limit=20
```
Response:
```json
{
  "notifications": [
    {
      "event_type": "journey.confirmed",
      "title": "Journey Confirmed",
      "message": "Your journey from Dublin City Centre to Cork Airport at 2026-03-10T20:00:00 has been confirmed.",
      "journey_id": "uuid",
      "timestamp": "2026-03-10T18:05:00"
    }
  ],
  "count": 1
}
```

#### Real-time WebSocket
```
ws://localhost:8004/ws/notifications/?token=<JWT>
```
Send `"ping"` → receive `"pong"` to keep the connection alive. Notifications are pushed as JSON when journey events occur.

---

## 11. Testing & Failure Scenarios

> **Docker required** for failure tests (they use `docker compose stop/start` to simulate crashes).

```bash
conda run -n DS python scripts/failure_tests.py
```

### Scenario 1 — Conflict Service crash during booking

The Conflict Service is stopped mid-booking. The Journey Service saga times out after 30 seconds and rejects the booking gracefully (rather than leaving it stuck as `PENDING`). After the Conflict Service restarts, new bookings succeed normally.

**Expected:** `status: REJECTED`, `rejection_reason: "Conflict check service unavailable. Please retry."`

### Scenario 2 — Redis cache flushed

Redis is completely flushed (`FLUSHALL`). The Enforcement Service no longer finds journeys in cache, so it falls back to querying the Journey Service API directly. The result is the same — the booking is found — just slightly slower (~50ms vs sub-1ms).

**Expected:** Enforcement still returns a valid booking via API fallback.

### Scenario 3 — RabbitMQ restart

RabbitMQ is restarted. Since messages are persisted to disk (durable queues, persistent delivery mode), no messages are lost. All services auto-reconnect using `aio_pika.connect_robust()` with retry logic. Event processing resumes automatically.

**Expected:** After ~30s, all services reconnect and continue processing.

### Scenario 4 — Journey database outage

The `postgres-journeys` container is stopped. The Journey Service returns `HTTP 500` on all requests (graceful degradation — no silent failures). After the database restarts, the service resumes with zero data loss.

**Expected:** `HTTP 500` during outage, `HTTP 200` after recovery.

---

## 12. Load Testing

```bash
# Default: 10 concurrent users for 30 seconds
conda run -n DS python scripts/load_test.py

# Custom
conda run -n DS python scripts/load_test.py --users 50 --duration 60

# Against Docker gateway
conda run -n DS python scripts/load_test.py --url http://localhost:8080 --users 20 --duration 30
```

Sample output:
```
LOAD TEST RESULTS
==================================================
  Duration:         30s
  Concurrent Users: 10
  Total Requests:   87
  Successful:       87
  Failed:           0
  Confirmed:        72
  Rejected:         15

  Latency Statistics:
    Min:    120ms
    Max:    850ms
    Mean:   310ms
    Median: 290ms
    P95:    620ms
    P99:    800ms

  Throughput: 2.9 requests/second
```

---

## 13. How Booking Works End-to-End

Here is the complete flow for a successful booking:

```
1. Client sends POST /api/journeys/ with JWT token
        ↓
2. Journey Service creates journey record with status=PENDING in PostgreSQL
        ↓
3. Journey Service calls BookingSaga.execute(journey)
        ↓
4. BookingSaga sends POST /api/conflicts/check to Conflict Service (sync REST, 30s timeout)
        ↓
5. Conflict Service checks 3 things:
   a) Does this driver have an overlapping booking?  → BookedSlots table
   b) Does this vehicle have an overlapping booking? → BookedSlots table
   c) Is road capacity exceeded at origin/destination grid cell? → RoadSegmentCapacity table
        ↓
6a. No conflict → Conflict Service inserts a new BookedSlot record, returns is_conflict=false
        ↓
7a. Journey Service updates status=CONFIRMED, caches journey in Redis (by vehicle + user ID)
        ↓
8a. Journey Service publishes journey.confirmed event to RabbitMQ
        ↓
9a. RabbitMQ fans out to 3 consumers:
   - Notification Service → stores notification in Redis, pushes to WebSocket if connected
   - Analytics Service → inserts EventLog row, increments Redis daily counter
   - (Enforcement Service reads from Redis cache on demand — no consumer needed)
        ↓
10a. Client receives HTTP 201 with status=CONFIRMED ✅

6b. Conflict found → returns is_conflict=true with reason
        ↓
7b. Journey Service updates status=REJECTED, sets rejection_reason
        ↓
8b. Publishes journey.rejected event
        ↓
9b. Notification + Analytics consume the event
        ↓
10b. Client receives HTTP 201 with status=REJECTED ❌

6c. Conflict Service unreachable / timeout after 30s
        ↓
7c. Journey Service updates status=REJECTED, reason="Conflict check service unavailable"
        ↓
10c. Client receives HTTP 201 with status=REJECTED (safe to retry) 🔄
```

---

## Environment Variables Reference

Every service reads configuration from environment variables. Defaults assume the Docker network hostnames.

| Variable | Default (Docker) | Description |
|----------|-----------------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://...@postgres-xxx:5432/xxx_db` | Service database connection |
| `REDIS_URL` | `redis://redis:6379/1` | Redis connection (DB 1 for journey cache) |
| `RABBITMQ_URL` | `amqp://journey_admin:journey_pass@rabbitmq:5672/journey_vhost` | Message broker |
| `JWT_SECRET` | `super-secret-jwt-key-change-in-production` | JWT signing key (change in production!) |
| `SERVICE_NAME` | `unknown-service` | Used in log formatting |
| `CONFLICT_SERVICE_URL` | `http://conflict-service:8000` | Journey Service → Conflict Service URL |
| `JOURNEY_SERVICE_URL` | `http://journey-service:8000` | Enforcement Service → Journey Service URL |

In local mode, `run_local.sh` overrides these automatically with `localhost` addresses.

---

## Interactive API Documentation

FastAPI auto-generates Swagger UI for every service:

| Service | Swagger URL |
|---------|------------|
| User Service | http://localhost:8001/docs |
| Journey Service | http://localhost:8002/docs |
| Conflict Service | http://localhost:8003/docs |
| Notification Service | http://localhost:8004/docs |
| Enforcement Service | http://localhost:8005/docs |
| Analytics Service | http://localhost:8006/docs |

---

## Tech Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Language | Python 3.12 | Fast development, strong async ecosystem |
| Web framework | FastAPI | Native async/await, auto OpenAPI docs, high throughput |
| ORM | SQLAlchemy 2.0 (async) | Mature, full async support |
| Database | PostgreSQL 16 | ACID guarantees, table partitioning, streaming replication |
| Cache | Redis 7 | Sub-millisecond lookups, TTL expiry, AOF persistence |
| Message broker | RabbitMQ 3.13 | Durable topic routing, dead-letter queues, management UI |
| API Gateway | Nginx 1.25 | Reverse proxy, built-in rate limiting, WebSocket support |
| Auth | PyJWT + bcrypt | Stateless JWT, no session store needed |
| Containerisation | Docker + Docker Compose | Reproducible multi-service deployment |
