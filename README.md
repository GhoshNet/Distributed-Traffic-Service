# Journey Booking System — Distributed Microservices

A fault-tolerant distributed system that lets road-vehicle drivers pre-book journeys. No driver may start a journey without a confirmed booking. The system checks for scheduling conflicts, notifies drivers in real time, and allows roadside enforcement agents to verify active bookings instantly.

> CS7NS6 Distributed Systems — Exercise 2 (Group J)

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Quick Start](#2-quick-start)
3. [Project Structure](#3-project-structure)
4. [Services](#4-services)
5. [System Architecture](#5-system-architecture)
6. [Infrastructure](#6-infrastructure)
7. [Configuration & Environment Variables](#7-configuration--environment-variables)
8. [Deployment Options](#8-deployment-options)
9. [Running the Demo & Tests](#9-running-the-demo--tests)
10. [API Reference](#10-api-reference)
11. [Distributed Systems Features](#11-distributed-systems-features)
12. [Known Limitations](#12-known-limitations)

---

## 1. Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| **Docker Desktop** | 24+ | Container runtime (includes Compose v2) |
| **Docker Compose** | v2.x | `docker compose` (no hyphen) must work |
| **Python 3.9+** | optional | Running demo/simulation scripts |
| **pip** | optional | `pip install httpx` for simulation scripts |
| **jq** | optional | Pretty-printing JSON responses in the shell |

**Hardware:** 8 GB RAM minimum for the slim stack (~12 containers). 16 GB+ recommended for the full stack (26 containers). The project runs on macOS, Linux, and Windows (WSL2).

Check your setup:

```bash
docker --version          # Docker version 24.x or higher
docker compose version    # Docker Compose version v2.x
python3 --version         # Python 3.9+
```

---

## 2. Quick Start

Clone the repo and start the slim stack (recommended — all 6 services work, minimal resource use):

```bash
git clone https://github.com/GhoshNet/Distributed-Traffic-Service.git
cd Distributed-Traffic-Service

# Start the stack (builds images, starts ~12 containers)
./start.sh
```

`start.sh` handles everything: teardown of any stale containers, port cleanup, image build, and a health-check loop. When it prints `Stack is up`, you're ready.

| URL | What |
|---|---|
| `http://localhost:3000` | Web frontend (register, book journeys, view map) |
| `http://localhost:8080` | API Gateway (HAProxy → nginx, rate-limited) |
| `http://localhost:15672` | RabbitMQ Management UI (`journey_admin` / `journey_pass`) |

**Verify all services are healthy:**

```bash
curl http://localhost:8080/api/analytics/health/services | jq
```

All 6 services should show `"status": "healthy"`.

---

## 3. Project Structure

```
.
├── start.sh                    # Main entry point — start/update/verify the stack
├── register_peers.sh           # Register peer nodes for multi-device mode
├── docker-compose.yml          # Full stack (26 containers: replicas, sentinels, cluster)
├── docker-compose.slim.yml     # Slim overlay (disables HA replicas, ~12 containers)
├── docker-compose.swarm.yml    # Docker Swarm deployment
│
├── api-gateway/
│   ├── nginx.conf              # Rate limiting (3 zones), upstream routing, JWT headers
│   └── haproxy.cfg             # Load balances across 2 nginx instances
│
├── frontend/
│   ├── index.html              # Single-page app (register, book, track journeys)
│   ├── app.js                  # API calls, WebSocket notifications, map UI
│   └── style.css
│
├── user-service/               # Python · FastAPI · :8001
├── journey-service/            # Python · FastAPI · :8002
├── conflict-service/           # Go · Gin · :8003
├── notification-service/       # Go · Gorilla WebSocket · :8004
├── enforcement-service/        # Python · FastAPI · :8005
├── analytics-service/          # Go · :8006
│
├── shared/                     # Shared Python utilities (imported by Python services)
│   ├── circuit_breaker.py      # Circuit breaker wrapping conflict-service calls
│   ├── partition.py            # CONNECTED→SUSPECTED→PARTITIONED state machine
│   └── tracing.py              # X-Request-ID propagation
│
├── postgres-init/              # SQL init scripts run by each Postgres container
├── postgres-custom/            # Custom Postgres config (WAL replication settings)
│
├── scripts/
│   ├── demo_local.py           # Automated 11-step end-to-end demo
│   ├── simulate_problems.py    # Interactive distributed failure simulator
│   ├── demo.py                 # Manual demo helper
│   └── load_test.py            # Load testing script
│
└── docs/
    ├── DEMO_GUIDE.md
    ├── MIDDLEWARE.md
    ├── SERVICE_DESCRIPTIONS.md
    └── ...
```

Each service directory follows the same layout:

```
<service>/
├── Dockerfile
├── requirements.txt  (or go.mod / go.sum for Go services)
└── app/
    ├── main.py       (or main.go)
    ├── models.py
    ├── routes.py
    └── ...
```

---

## 4. Services

| Service | Language | Port | Role |
|---|---|---|---|
| **user-service** | Python / FastAPI | 8001 | JWT auth, user profiles, vehicle registration |
| **journey-service** | Python / FastAPI | 8002 | Booking saga, transactional outbox, circuit breaker |
| **conflict-service** | Go / Gin | 8003 | Atomic capacity check (SERIALIZABLE tx + SELECT FOR UPDATE) |
| **notification-service** | Go | 8004 | WebSocket push, Redis-backed notification history |
| **enforcement-service** | Python / FastAPI | 8005 | Redis-first booking lookup for roadside checks |
| **analytics-service** | Go | 8006 | Event ingestion, hourly rollup, replica lag, service health |

### How services talk to each other

```
journey-service  ──REST──►  conflict-service    (saga: check slot before confirming)
journey-service  ──DB tx──  outbox table        (write journey + event atomically)
journey-service  ──drain──► RabbitMQ            (background thread replays outbox)
RabbitMQ         ──fanout─► notification-service, analytics-service,
                             enforcement-service, conflict-service
```

All services publish / consume from the RabbitMQ **topic exchange** `journey_events`. Routing keys:

| Key | Published by | Consumed by |
|---|---|---|
| `journey.confirmed` | journey-service | notification, analytics, enforcement |
| `journey.rejected` | journey-service | notification, analytics |
| `journey.cancelled` | journey-service | conflict (release slot), notification, enforcement, analytics |
| `user.registered` | user-service | analytics |

---

## 5. System Architecture

```
                    ┌─────────────────────────────┐
                    │    Client (Web / Mobile)     │
                    └──────────────┬──────────────┘
                                   │ HTTP / WebSocket
                                   ▼
                    ┌──────────────────────────────┐
                    │        HAProxy (:8080)        │
                    │  Load balances across nginx   │
                    └──────────┬───────────────────┘
                               │
                    ┌──────────▼───────────┐
                    │  Nginx API Gateway    │
                    │  Rate limiting (3     │
                    │  zones) · JWT routing │
                    └──┬──────┬──────┬─────┘
                       │      │      │
         ┌─────────────┘  ┌───┘  ┌───┘
         ▼                ▼      ▼
  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
  │  User    │ │ Journey  │ │ Conflict │ │Notificat-│ │Enforcemt │ │Analytics │
  │ Service  │ │ Service  │ │ Service  │ │ion Svc   │ │ Service  │ │ Service  │
  │  :8001   │ │  :8002   │ │  :8003   │ │  :8004   │ │  :8005   │ │  :8006   │
  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘
       │            │  REST saga  │             │             │             │
       │            └────────────┘             │             │             │
       ▼            │              ┌────────────┴─────────────┴─────────────┘
  ┌─────────┐       ▼              ▼
  │Postgres │  ┌─────────┐   ┌──────────────────────────────────────────┐
  │users_db │  │Postgres │   │           RabbitMQ (topic exchange)       │
  │+replica │  │jrny_db  │   │  journey.confirmed / rejected / cancelled │
  └─────────┘  │+replica │   └──────────────────────────────────────────┘
               └─────────┘
               ┌─────────┐   ┌─────────────────────────────────────┐
               │Postgres │   │  Redis Primary + Replica             │
               │cnflt_db │   │  3 × Sentinel (quorum = 2)          │
               └─────────┘   │  enforcement cache · notif history   │
               ┌─────────┐   │  analytics counters · dedup keys     │
               │Postgres │   └─────────────────────────────────────┘
               │anlyt_db │
               │+replica │
               └─────────┘
```

**Request path for a journey booking:**

```
Client → HAProxy → nginx → journey-service
  → conflict-service (REST, SERIALIZABLE tx, SELECT FOR UPDATE)
  ← conflict approved / rejected
  → DB: write journey row + outbox event (same transaction)
  → background thread: drain outbox → RabbitMQ
      → notification-service (WebSocket push to driver)
      → analytics-service   (stats + hourly rollup)
      → enforcement-service (cache active journey in Redis)
      → conflict-service    (free slot on cancel)
```

---

## 6. Infrastructure

| Component | Config | Notes |
|---|---|---|
| **Postgres** | 4 per-service DBs, each with a streaming replica (WAL level = replica) | Full stack: 8 containers. Slim: 4 containers (primaries only). Replica lag visible at `/api/analytics/replica-lag` |
| **Redis** | Primary + 1 replica + 3 Sentinel instances (quorum = 2) | Full stack: 5 containers. Slim: 1 container (primary only, Sentinel disabled). All services use `REDIS_SENTINEL_ADDRS` |
| **RabbitMQ** | Topic exchange `journey_events`, DLX / DLQ per consumer queue (24h TTL) | Full stack: 3-node cluster configured. Slim: single node. Single node is stable and supports all messaging features |
| **nginx** | 2 instances, rate limiting: auth 5 r/m · booking 30 r/m · general 60 r/m | Upstream routing to all 6 services |
| **HAProxy** | Fronts both nginx instances | Disabled in slim mode; nginx-1 is exposed directly on `:8080` |

---

## 7. Configuration & Environment Variables

The stack is configured via `docker-compose.yml` and a `.env` file (auto-generated by `start.sh` when you pass peer IPs).

### Key environment variables (set per service in compose files)

| Variable | Services | Description |
|---|---|---|
| `DATABASE_URL` | all | Primary Postgres connection string |
| `DATABASE_READ_URL` | user, journey, analytics | Replica connection string (reads route here) |
| `RABBITMQ_URL` | all | `amqp://journey_admin:journey_pass@rabbitmq:5672/journey_vhost` |
| `REDIS_URL` | all | Primary Redis URL |
| `REDIS_SENTINEL_ADDRS` | all | Comma-separated sentinel addresses. Empty string in slim mode |
| `JWT_SECRET` | user, journey | Shared secret for JWT signing / verification |
| `PEER_CONFLICT_URLS` | journey, conflict | Comma-separated conflict-service URLs on peer nodes |
| `PEER_USER_URLS` | user | Comma-separated user-service gateway URLs on peer nodes |

### Multi-node `.env` (generated automatically by `start.sh <PEER_IP>`)

```dotenv
PEER_CONFLICT_URLS=http://192.168.1.20:8003,http://192.168.1.21:8003
PEER_USER_URLS=http://192.168.1.20:8080,http://192.168.1.21:8080
MY_LABEL=A
```

You can also create this file manually before running `./start.sh`.

---

## 8. Deployment Options

### Option A — Slim stack (recommended, works on any 8 GB+ machine)

Disables DB replicas, Redis Sentinel, the RabbitMQ cluster, and HAProxy. Runs ~12 containers using ~2.5 GB RAM. All 6 services remain fully functional.

```bash
./start.sh
```

`start.sh` automatically uses `docker-compose.yml` + `docker-compose.slim.yml`. If you prefer to run the compose command directly:

```bash
docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d --build
```

### Option B — Full stack (requires 16 GB+ RAM)

Includes all replicas, Redis Sentinel, and the RabbitMQ cluster (~26 containers).

```bash
docker compose up -d --build
```

### Option C — Multi-node (two or more machines on the same network)

Run a separate stack on each machine. Peer nodes sync conflict capacity and user data, showing real distributed behaviour.

**On each machine:**

```bash
# Replace <PEER_IP> with the other machine's LAN IP
./start.sh <PEER_IP>
```

`start.sh` writes a `.env`, builds images with peer URLs baked in, and calls `register_peers.sh` automatically after startup.

**Manually update peers without restarting:**

```bash
./start.sh --update <PEER_IP>
```

**Verify peer sync is working:**

```bash
./start.sh --verify
```

**Finding your LAN IP:**

```bash
# macOS
ipconfig getifaddr en0        # Wi-Fi
ipconfig getifaddr bridge100  # Mobile hotspot

# Linux
hostname -I | awk '{print $1}'
```

### Option D — Oracle Cloud Free ARM (permanent public URL)

Oracle provides a permanently free ARM VM (4 OCPU, 24 GB RAM) — enough for the full stack.

```bash
# 1. Provision VM.Standard.A1.Flex on cloud.oracle.com
# 2. Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 3. Clone and start
git clone https://github.com/GhoshNet/Distributed-Traffic-Service.git
cd Distributed-Traffic-Service
./start.sh

# 4. Open ports in Oracle security list: 3000, 8080, 8001–8006, 15672
curl http://<VM_IP>:8080/api/analytics/health/services | jq
```

---

## 9. Running the Demo & Tests

### Automated end-to-end demo (11 steps)

```bash
# Requires: pip install httpx
python3 scripts/demo_local.py
```

Runs all 11 steps (register, book, conflict rejection, outbox recovery, circuit breaker, enforcement cache, etc.) and prints pass/fail for each.

### Interactive failure simulator

```bash
# Get a JWT token first
TOKEN=$(curl -s -X POST http://localhost:8080/api/users/login \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@example.com","password":"pass123"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

python3 scripts/simulate_problems.py --gateway http://localhost:8080 --token "$TOKEN"
```

| # | Demo | What it shows |
|---|---|---|
| 1 | Data Consistency Conflict | Two concurrent bookings on the same slot → second rejected by `SERIALIZABLE` isolation |
| 2 | Concurrent Booking Storm | 5 parallel bookings → race conditions handled by `SELECT FOR UPDATE` |
| 3 | 2PC / TCC Demo | Two-Phase Commit: PREPARE → COMMIT or ABORT with compensating cancel |
| 4 | Failure Detection | ALIVE → SUSPECT → DEAD state machine — stop a service to trigger |
| 5 | Circuit Breaker | Stop conflict-service → breaker opens after 3 failures, subsequent calls fail-fast |
| 6 | Graceful Degradation | More than half peers down → `LOCAL_ONLY` mode, system degrades gracefully |
| 7 | Transactional Outbox | Stop RabbitMQ, book a journey (succeeds), restart → event drains automatically |

### Manual demo steps

#### 1. Check all services are healthy

```bash
curl http://localhost:8006/api/analytics/health/services | jq
# Open RabbitMQ UI: http://localhost:15672  (journey_admin / journey_pass)
```

#### 2. Register, login, book a journey

```bash
# Register
curl -s -X POST http://localhost:8001/api/users/register \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@example.com","password":"pass123","full_name":"Demo User","license_number":"DL-DEMO-001"}'

# Login — store token
TOKEN=$(curl -s -X POST http://localhost:8001/api/users/login \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@example.com","password":"pass123"}' | jq -r .access_token)

# Register a vehicle
curl -s -X POST http://localhost:8001/api/users/vehicles \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"registration":"222-D-99999","vehicle_type":"CAR"}'

# Book a journey (watch journey-service + conflict-service logs)
docker compose logs -f journey-service conflict-service &
curl -s -X POST http://localhost:8002/api/journeys/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-001" \
  -d '{
    "origin":"Dublin","destination":"Cork",
    "origin_lat":53.3498,"origin_lng":-6.2603,
    "destination_lat":51.8413,"destination_lng":-8.4911,
    "departure_time":"'$(date -u -v+2H +%Y-%m-%dT%H:%M:%S)'",
    "estimated_duration_minutes":180,
    "vehicle_registration":"222-D-99999"
  }'
```

#### 3. Kill RabbitMQ → book → recover (outbox demo)

```bash
docker compose -f docker-compose.yml -f docker-compose.slim.yml stop rabbitmq
# Book a journey — it succeeds (event stored in outbox table)
docker compose -f docker-compose.yml -f docker-compose.slim.yml start rabbitmq
docker compose logs journey-service | grep -i outbox
# Event drains automatically after RabbitMQ reconnects
```

#### 4. Circuit breaker demo

```bash
docker compose -f docker-compose.yml -f docker-compose.slim.yml stop conflict-service
# Attempt 3+ bookings — after 3 failures the circuit opens, calls fail instantly
docker compose logs journey-service | grep -i circuit
docker compose -f docker-compose.yml -f docker-compose.slim.yml start conflict-service
# Next booking succeeds — circuit half-opens, probes, closes
```

#### 5. Enforcement cache & Redis Sentinel failover

```bash
# Cache miss → resolved via journey-service API
curl http://localhost:8005/api/enforcement/verify/vehicle/222-D-99999

# Second call — Redis cache hit (compare response times)
curl http://localhost:8005/api/enforcement/verify/vehicle/222-D-99999

# Stop Redis primary — Sentinel promotes replica (full stack only)
docker compose stop redis
sleep 15
curl http://localhost:8005/api/enforcement/verify/vehicle/222-D-99999
docker compose start redis
```

#### 6. Replica lag

```bash
curl http://localhost:8006/api/analytics/replica-lag | jq
# Shows write/flush/replay lag from pg_stat_replication (full stack only)
```

#### 7. Partition detection

```bash
docker network disconnect excercise2_journey-net excercise2-rabbitmq-1
docker compose logs journey-service | grep -i "PARTITIONED"
docker network connect excercise2_journey-net excercise2-rabbitmq-1
docker compose logs journey-service | grep -iE "MERGING|CONNECTED"
```

---

## 10. API Reference

All routes are accessible via the API gateway at `http://localhost:8080`. Direct service ports are listed for development use.

### User Service — `:8001`

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/users/register` | — | Register a new driver account |
| `POST` | `/api/users/register/agent` | — | Register an enforcement agent |
| `POST` | `/api/users/login` | — | Login, returns JWT access token |
| `GET` | `/api/users/profile` | JWT | Get own profile |
| `GET` | `/api/users/license/{number}` | — | Lookup user by licence number |
| `POST` | `/api/users/vehicles` | JWT | Register a vehicle to your account |
| `GET` | `/api/users/vehicles` | JWT | List your registered vehicles |

### Journey Service — `:8002`

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/journeys/` | JWT | Book a journey (include `Idempotency-Key` header) |
| `GET` | `/api/journeys/` | JWT | List own journeys |
| `GET` | `/api/journeys/{id}` | JWT | Get journey detail |
| `DELETE` | `/api/journeys/{id}` | JWT | Cancel a journey |
| `GET` | `/api/journeys/user/{user_id}/active` | — | Active journeys for a user (internal) |
| `GET` | `/api/journeys/vehicle/{reg}/active` | — | Active journeys for a vehicle (internal) |
| `GET` | `/health/nodes` | — | Per-peer ALIVE / SUSPECT / DEAD status |
| `GET` | `/health/partitions` | — | Partition detection state |
| `POST` | `/admin/peers/register` | — | Register a remote peer node to monitor |
| `DELETE` | `/admin/peers/{name}` | — | Remove a monitored peer |
| `POST` | `/admin/recovery/drain-outbox` | — | Force-drain unpublished outbox events |
| `POST` | `/admin/recovery/rebuild-enforcement-cache` | — | Rebuild enforcement Redis cache from DB |

### Conflict Service — `:8003`

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/conflicts/check` | — | Check and reserve a booking slot (called by journey-service) |
| `GET` | `/health` | — | Health check |

### Notification Service — `:8004`

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/notifications/` | `?token=<JWT>` | Past notifications (Redis-backed, max 50) |
| `WS` | `/ws/notifications/` | `?token=<JWT>` | Real-time WebSocket push |
| `GET` | `/health` | — | Health check |

### Enforcement Service — `:8005`

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/enforcement/verify/vehicle/{plate}` | — | Verify active booking by vehicle plate |
| `GET` | `/api/enforcement/verify/license/{number}` | — | Verify active booking by driving licence |
| `GET` | `/health/partitions` | — | Partition detection state |

### Analytics Service — `:8006`

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/analytics/stats` | — | Real-time stats (Redis counters + DB aggregates) |
| `GET` | `/api/analytics/hourly` | — | Hourly aggregated stats (`?limit=24`) |
| `GET` | `/api/analytics/events` | — | Event log (`?event_type=journey.confirmed&limit=50`) |
| `GET` | `/api/analytics/replica-lag` | — | Postgres streaming replication lag |
| `GET` | `/api/analytics/health/services` | — | Aggregated health of all 6 services |

### Health endpoint (all services)

```bash
GET /health
# → {"status": "healthy", "service": "...", "timestamp": "..."}
```

---

## 11. Distributed Systems Features

| Feature | Where |
|---|---|
| Service decomposition | 6 independent services, separate DBs, separate codebases |
| Async messaging (pub/sub) | RabbitMQ topic exchange `journey_events` |
| Saga pattern | `journey-service` orchestrates sync conflict check → confirm / reject |
| Transactional outbox | `journey_events` table written in same DB tx; background drain to RabbitMQ |
| Circuit breaker | `shared/circuit_breaker.py` wraps conflict-service call in journey-service |
| Read/write separation | Primary + streaming replica on users, journeys, conflicts, analytics |
| Pessimistic locking | `SELECT FOR UPDATE` in points ledger (journey) and capacity check (conflict) |
| Distributed atomic check | Conflict service wraps entire check + reserve in `SERIALIZABLE` transaction |
| Caching | Enforcement Redis-first lookup; `license→user_id` cached 24 h |
| Dead-letter queue | All 4 consumers, 24 h TTL, proper DLX exchange |
| Correlation IDs | `shared/tracing.py`, `X-Request-ID` propagated across all services |
| Rate limiting | nginx: 3 zones (auth 5 r/m · booking 30 r/m · general 60 r/m) |
| Health checks | All services `/health` + aggregated `/api/analytics/health/services` |
| Graceful shutdown | All services handle `SIGTERM` cleanly |
| Partition detection | `shared/partition.py` — CONNECTED → SUSPECTED → PARTITIONED → MERGING |
| Database replication | WAL streaming replication on all 4 service DBs |
| Redis HA (Sentinel) | 3-sentinel quorum; automatic failover |
| Idempotency | journey-service idempotency keys; analytics + notification consumers deduplicate via Redis SETNX |
| At-least-once delivery | Outbox guarantees publish; consumers handle redelivery safely |
| Time-series aggregation | Hourly rollup goroutine fills `hourly_stats` table |
| 2PC / TCC | `TwoPhaseCoordinator` in journey-service; select `?mode=2pc` on the booking endpoint |

---

## 12. Known Limitations

| Gap | Impact | Effort |
|---|---|---|
| Saga compensating transaction | If conflict-service is down, bookings fail permanently with no retry | Medium |
| Audit HMAC chain | `event_logs` table has no cryptographic integrity check | Medium |
| WebSocket registry in-memory | Notification connections lost on service restart | Medium |
| Enforcement cache cold start | First request after startup always misses Redis | Low |
| Token blacklisting on logout | Revoked JWTs remain valid until expiry | Low |
| RabbitMQ 3-node cluster | Nodes 2 and 3 may not join on a single Docker host (Erlang distribution) | High |
| Distributed tracing (Jaeger) | Correlation IDs exist but no visual span tree | High |
