# Journey Booking System — Distributed Microservices

> **CS7NS6 Distributed Systems — Exercise 2 (Group J)**
>
> A globally-accessible, fault-tolerant system that lets road-vehicle drivers pre-book journeys before they travel. No driver may start a journey without a confirmed booking. The system checks for scheduling conflicts, notifies drivers in real time, and allows roadside enforcement agents to verify active bookings instantly.

---

## Table of Contents

1. [Current Status](#1-current-status)
2. [Distributed Systems Principles Coverage](#2-distributed-systems-principles-coverage)
3. [Service Breakdown](#3-service-breakdown)
4. [System Architecture](#4-system-architecture)
5. [Infrastructure](#5-infrastructure)
6. [Deployment](#6-deployment)
7. [Bugs Fixed & Improvements Made](#7-bugs-fixed--improvements-made)
8. [Remaining Gaps](#8-remaining-gaps)
9. [Demo Script](#9-demo-script)
10. [API Reference](#10-api-reference)

---

## 1. Current Status

| Service | State | Remaining Gaps |
|---|---|---|
| **User Service** (Python · :8001) | Stateless JWT auth, primary/replica DB routing, publishes `user.registered` event on registration. | No token blacklisting on logout. |
| **Journey Service** (Python · :8002) | Strongest service. Transactional outbox, saga orchestration, circuit breaker, partition detection, idempotency keys, `SELECT FOR UPDATE` on points ledger, background lifecycle scheduler. | No saga compensating transaction on conflict-service failure. |
| **Conflict Service** (Go · :8003) | Atomic capacity check via `SERIALIZABLE` transaction + `SELECT FOR UPDATE`. Idempotent cancellation consumer (duplicate `journey.cancelled` events are safely ignored). DLQ configured. | — |
| **Notification Service** (Go · :8004) | WebSocket push, Redis-backed notification history (7-day TTL), DLQ, auto-reconnect. Consumer deduplication via Redis `SETNX` — redelivered messages produce no duplicates. | WebSocket registry is in-memory only (lost on restart). |
| **Enforcement Service** (Python · :8005) | Redis-first lookup, license→user_id cached in Redis (24h TTL), fallback to journey-service API, event-driven cache invalidation, partition staleness header `X-Cache-Stale`. | Cache cold on startup (no warm-up on boot). |
| **Analytics Service** (Go · :8006) | Dual-write Postgres + Redis. Hourly rollup job populates `hourly_stats` table. Consumer deduplication via Redis `SETNX`. Replica lag exposed at `/api/analytics/replica-lag`. Aggregated service health at `/api/analytics/health/services`. | Audit HMAC chain not completed. |

**Infrastructure:** Redis Sentinel (3 sentinels) fully operational — all services connect via Sentinel for automatic failover. Postgres streaming replication working on all 4 service databases. RabbitMQ single-node is stable; the 3-node cluster configuration is present but Erlang distribution is unreliable on a single Docker host. HAProxy + 2 nginx instances working.

**Demo verified:** Full end-to-end demo (`scripts/demo_local.py`) passes on Docker slim stack — all 6 services healthy, saga + conflict detection + enforcement + notifications + analytics all functional.

---

## 2. Distributed Systems Principles Coverage

| Principle | Status | Where |
|---|---|---|
| Service decomposition | ✅ Demonstrated | 6 independent services, separate DBs, separate codebases |
| Async messaging | ✅ Demonstrated | RabbitMQ topic exchange `journey_events`, routing keys per event type |
| Saga pattern | ✅ Demonstrated | journey-service orchestrates sync conflict check → confirm/reject |
| Transactional outbox | ✅ Demonstrated | `journey_events` table written in same DB transaction; background drain to RabbitMQ |
| Circuit breaker | ✅ Demonstrated | `shared/circuit_breaker.py` wraps conflict-service call in journey-service |
| Read/write separation | ✅ Demonstrated | Primary + streaming replica on users, journeys, conflicts, analytics |
| Pessimistic locking | ✅ Demonstrated | `SELECT FOR UPDATE` in points ledger (journey) and capacity check (conflict) |
| Distributed atomic check | ✅ Demonstrated | Conflict service wraps entire check+reserve in `SERIALIZABLE` transaction |
| Caching | ✅ Demonstrated | Enforcement Redis-first lookup; license→user_id cached 24h |
| Dead-letter queue | ✅ Demonstrated | All 4 consumers, 24h TTL, proper DLX exchange |
| Correlation IDs / tracing | ✅ Demonstrated | `shared/tracing.py`, `X-Request-ID` propagated across all services |
| Rate limiting | ✅ Demonstrated | nginx: 3 zones (auth / booking / general) |
| Health checks | ✅ Demonstrated | All services `/health` + analytics aggregated `/api/analytics/health/services` |
| Graceful shutdown | ✅ Demonstrated | All services handle SIGTERM cleanly |
| Partition detection | ✅ Demonstrated | `shared/partition.py` — CONNECTED → SUSPECTED → PARTITIONED → MERGING state machine; `X-Partition-Status` header on responses; `/health/partitions` endpoint |
| Database replication | ✅ Demonstrated | WAL streaming replication on all 4 service DBs; replica lag visible at `/api/analytics/replica-lag` |
| Redis HA (Sentinel) | ✅ Demonstrated | 3-sentinel quorum; all services use `REDIS_SENTINEL_ADDRS` env var for automatic failover |
| Idempotency | ✅ Demonstrated | journey-service idempotency keys; analytics + notification consumers deduplicate via Redis SETNX on `MessageId` / SHA-256 body hash |
| At-least-once delivery | ✅ Demonstrated | Outbox guarantees publish; consumers safely handle redelivery |
| Time-series aggregation | ✅ Demonstrated | Hourly rollup goroutine fills `hourly_stats` table; exposed at `/api/analytics/hourly` |
| Event bus participation | ✅ Demonstrated | All 6 services publish and/or consume from RabbitMQ — no silent bystanders |
| RabbitMQ clustering | ⚠️ Partial | 3-node cluster configured; Erlang distribution unreliable on single Docker host; single node demonstrates all messaging principles |
| Event sourcing / audit log | ⚠️ Partial | Full `event_logs` history with timestamps; HMAC audit chain not completed |
| Eventual consistency | ⚠️ Partial | Outbox + async event fan-out is real; no live demo of recovery from a real partition |
| Load balancing | ⚠️ Partial | HAProxy + 2 nginx configured; demo does not explicitly show traffic distribution |
| Compensating transactions | ❌ Missing | Saga rejects on conflict-service failure but never retries |
| Distributed tracing (spans) | ❌ Missing | Correlation IDs exist across services; no span visualisation (no Jaeger/Zipkin) |
| Cache warming | ❌ Missing | Enforcement cache cold on startup |

---

## 3. Service Breakdown

### User Service (Python · :8001)
Stateless JWT-based auth. Routes reads to a Postgres replica, writes to primary. Publishes a `user.registered` event to RabbitMQ after every successful registration — the last service to join the event bus.

### Journey Service (Python · :8002)
The most complete distributed systems implementation in the project.

- **Saga orchestration** — synchronously calls conflict-service (REST) to check slot availability before confirming. If conflict-service is unreachable, the circuit breaker opens and subsequent bookings fail fast.
- **Transactional outbox** — `journey_events` row is written in the same DB transaction as the journey row. A background thread drains it to RabbitMQ. If RabbitMQ is down at booking time, the event is not lost — it replays when the broker recovers.
- **Circuit breaker** — `shared/circuit_breaker.py` wraps the conflict-service call. After 3 consecutive failures the circuit opens; bookings fail immediately rather than hanging on timeouts.
- **Partition detection** — `shared/partition.py` probes Postgres, RabbitMQ, and the conflict-service every 5 seconds. Transitions through CONNECTED → SUSPECTED → PARTITIONED → MERGING. Flags responses with `X-Partition-Status` header.
- **Idempotency keys** — clients supply an idempotency key; duplicate booking requests return the cached result without re-running the saga.
- **Points ledger** — `SELECT FOR UPDATE` prevents concurrent updates from corrupting a user's balance.
- **Lifecycle scheduler** — background thread transitions journeys through `PENDING → ACTIVE → COMPLETED` based on departure/arrival times.

### Conflict Service (Go · :8003)
Tracks road-segment capacity using a geographic grid-cell model (`gridResolution = 0.01`, approximately 1 km per cell). Each cell tracks bookings per 30-minute time slot.

- **Atomic capacity check** — the entire check (driver overlap + vehicle overlap + road capacity) runs inside a single `SERIALIZABLE` transaction with `SELECT FOR UPDATE` on affected rows. No two concurrent bookings can both pass capacity when one slot remains.
- **Idempotent consumer** — `journey.cancelled` events release booked slots. If a message is redelivered, the second call finds `is_active = false` and returns silently (ack, no DLQ).
- **DLQ** — unprocessable messages route to `dead_letter_queue` with 24h TTL.

### Notification Service (Go · :8004)
Real-time push to drivers via WebSocket. Notification history stored in Redis per user (7-day TTL, max 50 entries).

- **Consumer deduplication** — checks `notif:processed:{MessageId}` in Redis before processing. If already seen (within 24h), acks and skips. Prevents duplicate push notifications on RabbitMQ redelivery.
- **Auto-reconnect** — on broker disconnect, reconnects with 3-second backoff.
- **DLQ** — failed events route to dead letter queue.

### Enforcement Service (Python · :8005)
Verifies active bookings by vehicle plate or driving licence number. Designed for roadside checks where sub-second response is required.

- **Redis-first** — checks `active_journey:vehicle:{plate}` or `active_journey:user:{user_id}` cache key.
- **License caching** — `license→user_id` mapping cached in Redis for 24h (`license_user_id:{license}`). Eliminates a synchronous call to user-service on every licence verification request.
- **API fallback** — on Redis miss, calls journey-service HTTP API to find active journeys.
- **Event-driven invalidation** — `journey.cancelled` and `journey.completed` events remove cache entries immediately.
- **Staleness header** — adds `X-Cache-Stale: true` when partition manager detects journey-service is unreachable.

### Analytics Service (Go · :8006)
Records every journey event to Postgres and updates Redis counters atomically.

- **Consumer deduplication** — checks `analytics:processed:{MessageId}` in Redis before inserting. No double-counting on redelivery.
- **Hourly rollup** — `runHourlyRollup()` goroutine runs on startup (backfills the previous hour) then every hour, aggregating `event_logs` into `hourly_stats`.
- **Replica lag endpoint** — `GET /api/analytics/replica-lag` queries `pg_stat_replication` on the analytics primary and returns write/flush/replay lag per connected replica.
- **Service health aggregator** — `GET /api/analytics/health/services` pings all 6 services and returns a combined health report.

---

## 4. System Architecture

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
               └─────────┘        ▲ publish              │ consume
               ┌─────────┐        │                      ▼
               │Postgres │   Journey Svc       Notification Svc
               │cnflt_db │   (outbox drain)  + Conflict Svc
               └─────────┘                  + Analytics Svc
               ┌─────────┐                  + Enforcement Svc
               │Postgres │
               │anlyt_db │   ┌─────────────────────────────────────┐
               │+replica │   │  Redis Primary + Replica             │
               └─────────┘   │  3 × Sentinel (quorum = 2)          │
                             │  enforcement cache · notif history   │
                             │  analytics counters · dedup keys     │
                             └─────────────────────────────────────┘
```

**Request path for journey booking:**

```
Client → HAProxy → nginx → journey-service
  → conflict-service (REST, SERIALIZABLE tx, SELECT FOR UPDATE)
  ← conflict approved / rejected
  → DB: write journey row + outbox event (same transaction)
  → background goroutine: drain outbox → RabbitMQ
      → notification-service (WebSocket push to driver)
      → analytics-service (stats + hourly rollup)
      → enforcement-service (cache active journey)
      → conflict-service (free slot on cancel)
```

---

## 5. Infrastructure

| Component | Configuration | Status |
|---|---|---|
| **RabbitMQ** | 3-node cluster configured, topic exchange `journey_events`, DLX/DLQ per consumer queue | Single node stable and functional; cluster nodes (rabbitmq-2, rabbitmq-3) may not join reliably on a single Docker host due to Erlang distribution issues — does not affect app functionality |
| **Redis** | Primary + 1 replica + 3 Sentinel instances (quorum = 2) | Fully operational. `$$REDIS_IP` bug in sentinel startup script fixed. All 6 services connect via `REDIS_SENTINEL_ADDRS` — automatic failover works |
| **Postgres** | Per-service primary + streaming replica (4 pairs), WAL level = replica | All 8 containers healthy. Replica initialised via `pg_basebackup`. Replication lag visible at `/api/analytics/replica-lag` |
| **nginx** | 2 instances, rate limiting (3 zones: auth 5r/m, booking 30r/m, general 60r/m), upstream routing | Working |
| **HAProxy** | Fronts both nginx instances | Working |

---

## 6. Deployment

### Option A — Oracle Cloud Free ARM (recommended for full demo)

Oracle provides a permanently free ARM VM: **4 OCPUs, 24 GB RAM** — enough to run the full 26-container stack comfortably.

```bash
# 1. Provision VM.Standard.A1.Flex (4 OCPU, 24 GB) on cloud.oracle.com
# 2. Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER

# 3. Clone and start
git clone https://github.com/GhoshNet/Distributed-Traffic-Service.git
cd Distributed-Traffic-Service
docker compose up -d

# 4. Open ports in Oracle security list: 8080, 8001-8006, 15672
# 5. Verify
curl http://<VM_IP>:8080/api/analytics/health/services | jq
```

### Option B — Slim stack for low-RAM machines (8 GB or less)

Running all 26 containers on an 8 GB machine pushes RAM to ~80%+. A slim override disables replicas, sentinels, and the RabbitMQ cluster — reducing to 12 containers (~2.5 GB RAM) while keeping all 6 services fully functional.

```bash
# Create slim override (disables replicas, sentinels, cluster nodes)
cat > docker-compose.slim.yml << 'EOF'
version: '3.8'
services:
  postgres-users-replica:
    profiles: [full]
  postgres-journeys-replica:
    profiles: [full]
  postgres-conflicts-replica:
    profiles: [full]
  postgres-analytics-replica:
    profiles: [full]
  redis-replica:
    profiles: [full]
  redis-sentinel:
    profiles: [full]
  redis-sentinel-2:
    profiles: [full]
  redis-sentinel-3:
    profiles: [full]
  rabbitmq-2:
    profiles: [full]
  rabbitmq-3:
    profiles: [full]
  api-gateway-2:
    profiles: [full]
  haproxy:
    profiles: [full]
  enforcement-service:
    environment:
      REDIS_SENTINEL_ADDRS: ""
  notification-service:
    environment:
      REDIS_SENTINEL_ADDRS: ""
  analytics-service:
    environment:
      REDIS_SENTINEL_ADDRS: ""
  user-service:
    environment:
      REDIS_SENTINEL_ADDRS: ""
  journey-service:
    environment:
      REDIS_SENTINEL_ADDRS: ""
  conflict-service:
    environment:
      REDIS_SENTINEL_ADDRS: ""
EOF

docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d --build
```

### Option C — Native mode (lightest, ~400 MB)

Uses Homebrew Postgres, Redis, and RabbitMQ directly — no Docker containers for infrastructure. All distributed logic (saga, outbox, partition detection, circuit breaker) still runs.

```bash
bash scripts/run_local.sh start
conda run -n DS python scripts/demo_local.py
```

### Option D — Two machines (most convincing distributed demo)

```bash
# Machine A — infrastructure only
docker compose -f infra.yml up -d

# Machine B — app services (point at Machine A's IP)
RABBITMQ_URL=amqp://journey_admin:journey_pass@<MACHINE_A_IP>:5672/journey_vhost \
REDIS_URL=redis://<MACHINE_A_IP>:6379/1 \
docker compose -f services.yml up -d
```

Kill Machine A mid-demo → shows circuit breaker opening, outbox buffering, and recovery when it restarts.

---

## 7. Bugs Fixed & Improvements Made

This section documents every concrete problem found and resolved during development and testing.

### Infrastructure Fixes

#### Redis Sentinel — `$REDIS_IP` Docker Compose variable interpolation bug
**Problem:** Sentinel containers were stuck in an infinite loop printing `Waiting for redis DNS...` even though DNS resolved correctly. The actual sentinel process never started.

**Root cause:** Docker Compose treats `$VAR` in YAML as a Compose environment variable and substitutes it before passing to the shell. `$REDIS_IP` was being replaced with an empty string, making the `until` loop condition always false. The `docker compose ps` warning `"REDIS_IP" variable is not set. Defaulting to a blank string.` was the tell.

**Fix:** Escaped all shell variables in sentinel commands with `$$` (double dollar) so Docker Compose passes them through literally to the shell:
```yaml
# Before (broken)
until REDIS_IP=$(getent hosts redis | awk '{print $1; exit}') && [ -n "$REDIS_IP" ]; do

# After (fixed)
until REDIS_IP=$$(getent hosts redis | awk '{print $$1; exit}') && [ -n "$$REDIS_IP" ]; do
```

#### Postgres Replicas — permission and root execution errors
**Problem:** All 4 replica containers failed with two errors in sequence:
1. `data directory has invalid permissions — Permissions should be u=rwx (0700)`
2. `"root" execution of the PostgreSQL server is not permitted`

**Fix:** Added `chown -R postgres:postgres`, `chmod 700`, and `exec gosu postgres postgres` to all 4 replica entrypoints in `docker-compose.yml`.

#### Postgres Replicas — replication connection denied
**Problem:** After fixing permissions, replicas failed with `FATAL: no pg_hba.conf entry for replication connection`.

**Fix:** Created `postgres-init/01_allow_replication.sh` that appends `host replication all all md5` to `pg_hba.conf`. Mounted to `/docker-entrypoint-initdb.d/` on all 4 primary containers so it runs on first initialisation.

#### Redis DB mismatch — enforcement cache never hit
**Problem:** Enforcement service was reading from Redis DB 4 but journey-service was writing active journeys to DB 1. Cache lookups always missed.

**Fix:** Aligned both services to Redis DB 1 for enforcement cache keys.

---

### Correctness Fixes

#### Conflict Service — double-booking race condition
**Problem:** The capacity check (read) and slot reservation (write) were two separate DB operations. Two concurrent booking requests could both pass the capacity check before either incremented the counter — both got confirmed even if one exceeded road capacity.

**Fix:** Wrapped the entire `checkConflicts` function in a single `SERIALIZABLE` transaction. All three sub-checks (`checkDriverOverlap`, `checkVehicleOverlap`, `checkRoadCapacity`) run inside the transaction with `SELECT FOR UPDATE`. Postgres serializes concurrent transactions — the second one either waits or gets a serialization error (returned as a clean rejection).

#### Conflict Consumer — needless DLQ routing on duplicate cancel
**Problem:** If RabbitMQ redelivered a `journey.cancelled` message (at-least-once delivery), the second call to `cancelBookingSlot` returned `ErrNotFound` (slot already inactive). This caused a `Nack`, routing the message to the DLQ unnecessarily.

**Fix:** Added `errors.Is(err, ErrNotFound)` check — treat already-cancelled as success, log, and ack cleanly.

#### Analytics & Notification — event double-counting on redelivery
**Problem:** RabbitMQ guarantees at-least-once delivery. On consumer restart, inflight messages are redelivered. Analytics would double-count events; notification would push duplicate messages to users.

**Fix:** Before processing each message, check `Redis SETNX {service}:processed:{MessageId}` with a 24-hour TTL. If already processed, ack and skip. Uses SHA-256 hash of message body as fallback key if `MessageId` is not set by the publisher.

#### Enforcement — synchronous user-service call on every licence check
**Problem:** Every call to `GET /api/enforcement/verify/license/{number}` made a synchronous HTTP call to user-service to resolve `license_number → user_id`. Under load this is a bottleneck and creates unnecessary coupling.

**Fix:** Cache the `license_user_id:{license}` key in Redis with a 24-hour TTL. Subsequent calls for the same licence skip the user-service call entirely.

---

### Missing Features Added

#### User Service — `user.registered` event
**Problem:** User service was the only service that never published any event to RabbitMQ. Every other service participated in the event bus; user service was a silent writer.

**Fix:** After successful registration, publish `user.registered` with `user_id`, `email`, `full_name`, `license_number`, `registered_at` fields. Published best-effort — if the broker is down, registration still completes and a warning is logged.

#### Analytics — hourly_stats rollup
**Problem:** The `hourly_stats` table existed in the DB schema but was never written to. The rollup story was entirely missing.

**Fix:** Added `runHourlyRollup()` goroutine that fires on startup (backfills the previous completed hour) and then every hour. Aggregates `event_logs` into `hourly_stats` using `ON CONFLICT (hour) DO UPDATE`. Exposed at `GET /api/analytics/hourly`.

#### Analytics — replica lag endpoint
**Problem:** Postgres streaming replication was configured and running, but there was no way to observe it from outside the containers.

**Fix:** Added `GET /api/analytics/replica-lag` which queries `pg_stat_replication` on the analytics primary and returns `write_lag`, `flush_lag`, and `replay_lag` per connected replica.

#### Redis Sentinel wiring
**Problem:** Sentinel was running (after the `$$REDIS_IP` fix) but all services connected directly to `redis:6379`. If the primary failed and Sentinel promoted the replica, services would keep writing to the old (now demoted) primary.

**Fix:**
- Added `REDIS_SENTINEL_ADDRS` and `REDIS_MASTER_NAME` env vars to all 6 services in `docker-compose.yml`
- Python services (enforcement, journey, user): use `redis.asyncio.sentinel.AsyncSentinel` — automatically tracks which node is current primary
- Go services (analytics, notification): use `redis.NewFailoverClient` with `FailoverOptions` — same behaviour in Go

---

## 8. Remaining Gaps

These are known limitations that do not affect the demo but are documented for completeness.

| Gap | Impact | Effort to fix |
|---|---|---|
| Saga compensating transaction | If conflict-service is down, bookings fail permanently with no retry | Medium |
| Audit HMAC chain | `event_logs` has no cryptographic integrity check | Medium |
| WebSocket registry in-memory | Notification connections lost on service restart | Medium |
| Enforcement cache cold start | First request after startup always misses Redis | Low |
| Token blacklisting on logout | Revoked JWTs still valid until expiry | Low |
| RabbitMQ 3-node cluster | cluster nodes 2 and 3 may not join on single Docker host | High (Erlang distribution) |
| Distributed tracing (Jaeger) | Correlation IDs exist but no visual span tree | High |

---

## 9. Demo Script

Two ways to run the demo:

**Automated (recommended):**
```bash
conda run -n DS python scripts/demo_local.py
```
Runs all 11 steps automatically and prints pass/fail for each.

**Manual step-by-step** — each command proves a specific principle:

### Step 1 — Architecture is alive
```bash
curl http://localhost:8006/api/analytics/health/services | jq
```
Shows all 6 services healthy with response times. Open RabbitMQ UI at `http://localhost:15672` (guest/guest) to show exchanges, queues, and consumers.

**Principle:** Service decomposition, independent deployability.

---

### Step 2 — Book a journey (saga)
```bash
# Register and login
curl -s -X POST http://localhost:8001/api/users/register \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@example.com","password":"pass123","full_name":"Demo User","license_number":"DL-DEMO-001"}'

TOKEN=$(curl -s -X POST http://localhost:8001/api/users/login \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@example.com","password":"pass123"}' | jq -r .access_token)

# Register vehicle
curl -s -X POST http://localhost:8001/api/users/vehicles \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"registration":"222-D-99999","vehicle_type":"CAR"}'

# Book journey — watch journey-service call conflict-service in logs
docker compose logs -f journey-service conflict-service &
curl -s -X POST http://localhost:8002/api/journeys/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-001" \
  -d '{"origin":"Dublin","destination":"Cork","origin_lat":53.3498,"origin_lng":-6.2603,"destination_lat":51.8413,"destination_lng":-8.4911,"departure_time":"'$(date -u -v+2H +%Y-%m-%dT%H:%M:%S)'","estimated_duration_minutes":180,"vehicle_registration":"222-D-99999"}'
```

**Principle:** Saga pattern, synchronous cross-service coordination.

---

### Step 3 — Async event fan-out
```bash
docker compose logs -f notification-service analytics-service
curl http://localhost:8006/api/analytics/stats | jq
```
Both services received the same `journey.confirmed` event from RabbitMQ. Stats counter incremented.

**Principle:** Publish-subscribe, eventual consistency.

---

### Step 4 — Kill RabbitMQ, book, recover (outbox)
```bash
docker compose stop rabbitmq
# Book another journey — succeeds, event buffered in outbox table
docker compose start rabbitmq
docker compose logs journey-service | grep -i outbox
# Event drains to RabbitMQ after reconnect
```

**Principle:** Transactional outbox, at-least-once delivery, durability.

---

### Step 5 — Kill conflict-service (circuit breaker)
```bash
docker compose stop conflict-service
# Try 3 bookings — circuit opens after 3 failures, subsequent calls fail instantly
docker compose logs journey-service | grep -i circuit
docker compose start conflict-service
# Next booking succeeds — circuit half-opens, probes, closes
```

**Principle:** Circuit breaker, fail-fast, self-healing.

---

### Step 6 — Enforcement cache
```bash
# First call: cache miss, resolves via journey-service API
curl http://localhost:8005/api/enforcement/verify/vehicle/222-D-99999

# Second call: Redis cache hit (check response time difference)
curl http://localhost:8005/api/enforcement/verify/vehicle/222-D-99999

# Kill Redis primary, Sentinel promotes replica, service reconnects automatically
docker compose stop redis
sleep 15
curl http://localhost:8005/api/enforcement/verify/vehicle/222-D-99999
docker compose start redis
```

**Principle:** Caching, Redis Sentinel HA, automatic failover.

---

### Step 7 — Replica lag
```bash
curl http://localhost:8006/api/analytics/replica-lag | jq
```
Shows write/flush/replay lag from `pg_stat_replication` — live proof that streaming replication is active.

**Principle:** Read/write separation, database replication.

---

### Step 8 — Partition detection
```bash
docker network disconnect excercise2_journey-net excercise2-rabbitmq-1
docker compose logs journey-service | grep -i "PARTITIONED"
docker network connect excercise2_journey-net excercise2-rabbitmq-1
docker compose logs journey-service | grep -i "MERGING\|CONNECTED"
```

**Principle:** Partition detection, CAP theorem, staleness flagging.

---

## 10. API Reference

Direct service ports (no gateway). In Docker, prefix with gateway at `:8080` where nginx routing is configured.

### User Service (:8001)

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/users/register` | — | Register a new driver account |
| `POST` | `/api/users/register/agent` | — | Register an enforcement agent |
| `POST` | `/api/users/login` | — | Login, returns JWT access token |
| `GET` | `/api/users/profile` | ✅ | Get own profile |
| `GET` | `/api/users/license/{number}` | — | Lookup user by licence number |
| `POST` | `/api/users/vehicles` | ✅ | Register a vehicle to your account |
| `GET` | `/api/users/vehicles` | ✅ | List your registered vehicles |

### Journey Service (:8002)

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/journeys/` | ✅ | Book a journey (send `Idempotency-Key` header) |
| `GET` | `/api/journeys/` | ✅ | List own journeys |
| `GET` | `/api/journeys/{id}` | ✅ | Get journey detail |
| `DELETE` | `/api/journeys/{id}` | ✅ | Cancel a journey |
| `GET` | `/api/journeys/user/{user_id}/active` | — | Active journeys for a user (internal) |
| `GET` | `/api/journeys/vehicle/{reg}/active` | — | Active journeys for a vehicle (internal) |
| `GET` | `/health/partitions` | — | Partition detection state |

### Conflict Service (:8003)

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/api/conflicts/check` | — | Check and reserve a booking slot (called by journey-service) |
| `GET` | `/health` | — | Health check |

### Notification Service (:8004)

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/notifications/` | token param | Past notifications (Redis-backed, max 50) |
| `WS` | `/ws/notifications/` | token param | Real-time WebSocket push |
| `GET` | `/health` | — | Health check |

### Enforcement Service (:8005)

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/enforcement/verify/vehicle/{plate}` | — | Verify booking by vehicle plate |
| `GET` | `/api/enforcement/verify/license/{number}` | — | Verify booking by driving licence |
| `GET` | `/health/partitions` | — | Partition detection state |

### Analytics Service (:8006)

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/api/analytics/stats` | — | Real-time stats (Redis counters + DB aggregates) |
| `GET` | `/api/analytics/hourly` | — | Hourly aggregated stats (`?limit=24`) |
| `GET` | `/api/analytics/events` | — | Event log (`?event_type=journey.confirmed&limit=50`) |
| `GET` | `/api/analytics/replica-lag` | — | Postgres streaming replication lag |
| `GET` | `/api/analytics/health/services` | — | Aggregated health of all 6 services |

### Health (all services)

```bash
GET /health  →  {"status": "healthy", "service": "...", "timestamp": "..."}
```
