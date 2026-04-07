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
7. [Prioritized Action Checklist](#7-prioritized-action-checklist)
8. [Implementation Plan — Overnight Enhancements](#8-implementation-plan--overnight-enhancements)
9. [Demo Script](#9-demo-script)
10. [API Reference](#10-api-reference)

---

## 1. Current Status

| Service | State | Gaps |
|---|---|---|
| **User Service** | Clean, stateless, works. Primary/replica DB routing, JWT auth. | No Redis caching, no `user.registered` event published, no token blacklisting. |
| **Journey Service** | Strongest service. Transactional outbox, saga orchestration, circuit breaker, partition detection, idempotency keys, `SELECT FOR UPDATE` on points ledger, background lifecycle scheduler. | Non-atomic scheduler commit, no saga compensation. |
| **Conflict Service (Go)** | Functional, RabbitMQ consumer + DLQ properly set up. | Real race condition: capacity check → increment is not atomic. Two concurrent bookings to the same road segment can both pass. No distributed lock. |
| **Notification Service (Go)** | Solid. WebSocket push, Redis-backed notification history, DLQ, auto-reconnect. | No deduplication — at-least-once delivery means duplicate notifications. WebSocket registry is in-memory only. |
| **Enforcement Service** | Best caching story. Redis-first, fallback to journey-service HTTP, event-driven cache invalidation, partition staleness headers. | License→user_id lookup hits user-service synchronously every time. Cache is cold on startup. |
| **Analytics Service (Go)** | Dual-write to Postgres + Redis. Health aggregator endpoint is real. | `hourly_stats` table scaffolded but never written. No event deduplication. Audit HMAC referenced but not implemented. |

**Infrastructure:** 3-node RabbitMQ cluster (broken — Erlang distribution not set up correctly in Docker), Redis Sentinel with replica, Postgres streaming replication per service, HAProxy + 2 nginx. The RabbitMQ cluster is the only infrastructure piece that doesn't actually work.

**Overall verdict:** This is a legitimately distributed system, not a monolith with HTTP calls. The saga, outbox, circuit breaker, partition detection, and replication are real. The gap is at the edges — idempotency on consumers, compensation logic, and the broken RabbitMQ cluster undermining the HA story.

---

## 2. Distributed Systems Principles Coverage

| Principle | Status | Where |
|---|---|---|
| Service decomposition | ✅ Demonstrated | 6 independent services, separate DBs |
| Async messaging | ✅ Demonstrated | RabbitMQ topic exchange, routing keys |
| Saga pattern | ✅ Demonstrated | journey-service orchestrates conflict check |
| Transactional outbox | ✅ Demonstrated | journey-service outbox + background drain |
| Circuit breaker | ✅ Demonstrated | `shared/circuit_breaker.py`, used by journey |
| Read/write separation | ✅ Demonstrated | Primary + replica on users, journeys, analytics |
| Pessimistic locking | ✅ Demonstrated | `SELECT FOR UPDATE` in points ledger |
| Caching | ✅ Demonstrated | Enforcement Redis-first lookup |
| Dead-letter queue | ✅ Demonstrated | All consumers, 24h TTL, proper DLX |
| Correlation IDs / tracing | ✅ Demonstrated | `shared/tracing.py`, X-Request-ID propagated |
| Rate limiting | ✅ Demonstrated | nginx: 3 zones (auth / booking / general) |
| Health checks | ✅ Demonstrated | All services + analytics aggregator |
| Graceful shutdown | ✅ Demonstrated | All services handle SIGTERM |
| Partition detection | ⚠️ Partial | Detects partition, flags staleness — doesn't change behavior |
| Database replication | ⚠️ Partial | Configured in compose; replica lag not exposed |
| RabbitMQ clustering | ⚠️ Partial | Configured but Erlang distribution broken in Docker |
| Redis HA (Sentinel) | ⚠️ Partial | Configured; app services don't use Sentinel URL |
| Event sourcing / audit log | ⚠️ Partial | `event_logs` table exists; no dedup, HMAC incomplete |
| Idempotency | ⚠️ Partial | journey-service has idempotency keys; consumers don't |
| Eventual consistency | ⚠️ Partial | Claimed; no demo showing it recovering |
| At-least-once delivery | ⚠️ Partial | Outbox guarantees publish; no consumer dedup |
| Load balancing | ⚠️ Partial | HAProxy + nginx configured; no traffic to show it |
| Compensating transactions | ❌ Missing | Saga rejects but never compensates/retries |
| Distributed locking | ❌ Missing | Conflict-service capacity check is a race |
| Leader election | ❌ Missing | — |
| Backpressure | ❌ Missing | QoS=10 set but no rejection/throttling logic |
| Distributed tracing (spans) | ❌ Missing | Correlation IDs exist; no span tree (no Jaeger/Zipkin) |
| Idempotent consumers | ❌ Missing | Analytics, notification, conflict consumers all re-process |
| Cache warming | ❌ Missing | Enforcement starts cold |
| Rollup / aggregation jobs | ❌ Missing | `hourly_stats` never populated |

---

## 3. Service Breakdown

### User Service (Python · :8001)
Stateless, JWT-based auth. Routes reads to a Postgres replica, writes to primary. No events published on registration — the only service that does not participate in the event bus on the write side.

### Journey Service (Python · :8002)
The most complete distributed systems implementation in the project.

- **Saga orchestration** — synchronously calls conflict-service to check slot availability before confirming a booking.
- **Transactional outbox** — the `journey_events` row is written in the same DB transaction as the journey row; a background thread drains it to RabbitMQ. If RabbitMQ is down, the event is not lost.
- **Circuit breaker** — wraps the conflict-service call via `shared/circuit_breaker.py`. After 3 failures the circuit opens; bookings fail fast rather than hanging.
- **Partition detection** — `shared/partition.py` detects when the service cannot reach RabbitMQ or Postgres and flags responses with a `X-Partition-Detected` header.
- **Idempotency keys** — clients supply an idempotency key; duplicate requests return the cached result.
- **Points ledger** — `SELECT FOR UPDATE` prevents concurrent updates from corrupting the balance.
- **Lifecycle scheduler** — background thread transitions journeys through `PENDING → ACTIVE → COMPLETED`.

### Conflict Service (Go · :8003)
Tracks road-segment capacity via a grid-cell model. Consumes `journey.cancelled` events to free slots. Has a proper RabbitMQ DLQ. **Known race condition:** capacity check and increment are two separate DB operations — concurrent bookings can both pass.

### Notification Service (Go · :8004)
WebSocket push to connected clients. Notification history in Redis (7-day TTL). DLQ on the RabbitMQ consumer. Auto-reconnect loop on broker failure. **Gap:** no event deduplication — redelivered messages produce duplicate notifications.

### Enforcement Service (Python · :8005)
Verifies active bookings by vehicle plate or driving licence. Redis-first with a TTL-based cache; on miss, calls journey-service over HTTP. Cache is invalidated via `journey.cancelled` events consumed from RabbitMQ. Adds `X-Cache-Stale` header when partition is detected. **Gap:** license→user_id resolution is synchronous to user-service on every request.

### Analytics Service (Go · :8006)
Dual-write to Postgres and Redis on every journey event. Exposes a `/health/services` endpoint that aggregates the health of all other services. **Gap:** `hourly_stats` table is never populated; no consumer deduplication.

---

## 4. System Architecture

```
                    ┌─────────────────────────────┐
                    │    Client (Web / Mobile)     │
                    └──────────────┬──────────────┘
                                   │ HTTP / WebSocket
                                   ▼
                    ┌──────────────────────────────┐
                    │   Nginx API Gateway (:8080)   │
                    │  Rate limiting · JWT routing  │
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
        │+replica │  │jrny_db  │   │  topic exchange          │
        └─────────┘  │+replica │   │  journey_events          │
                     └─────────┘   └──────────────────────────┘
                     ┌─────────┐        ▲ publish       │ consume
                     │Postgres │        │               ▼
                     │cnflt_db │   Journey Svc    Notification Svc
                     └─────────┘   (outbox)     + Conflict Svc
                     ┌─────────┐               + Analytics Svc
                     │Postgres │               + Enforcement Svc
                     │anlyt_db │
                     │+replica │   ┌──────────────────────────┐
                     └─────────┘   │  Redis (+ Sentinel)      │
                                   │  enforcement cache        │
                                   │  notification history     │
                                   │  analytics counters       │
                                   └──────────────────────────┘
```

**Request path for journey booking:**

```
Client → nginx → journey-service
  → conflict-service (REST, saga)
  ← conflict approved
  → DB: write journey + outbox row (same transaction)
  → background: drain outbox → RabbitMQ
  → notification-service (WebSocket push)
  → analytics-service (stats update)
  → enforcement-service (cache update)
```

---

## 5. Infrastructure

| Component | Config | Status |
|---|---|---|
| RabbitMQ | 3-node cluster, topic exchange, DLX/DLQ per queue | Cluster broken in Docker (Erlang distribution); single node works |
| Redis | Primary + 1 replica + Sentinel | Sentinel running; services connect directly to `:6379`, not Sentinel URL |
| Postgres | Per-service primary + streaming replica | Replication configured; replica lag not surfaced to clients |
| nginx | 2 instances, rate limiting (3 zones), JWT routing | Working |
| HAProxy | Front of nginx | Working |

---

## 6. Deployment

**Recommended: Docker Compose on two machines.**

Split the compose into two files — `infra.yml` (RabbitMQ, Redis, Postgres) on machine A and `services.yml` (app services) on machine B. Set the host IP of machine A in service env vars. Kill machine A mid-demo to show circuit breaker and outbox behaviour.

```bash
# Machine A — infrastructure
docker compose -f infra.yml up -d

# Machine B — services (set RABBITMQ_URL, REDIS_URL, DATABASE_URL to machine A's IP)
docker compose -f services.yml up -d
```

**Alternatives:**

| Option | Pros | Cons |
|---|---|---|
| Railway free tier | Real distributed deployment, public URLs, 6 services fit the free tier | Cold starts |
| fly.io | Better always-on free tier, small Go binaries | Python services need more RAM |
| minikube / k3s | Readiness/liveness probes, rolling deploys, namespaces | Hard to set up |

**Avoid:** Do not attempt to make the 3-node RabbitMQ cluster work in Docker Compose — Erlang cookie synchronisation and hostname resolution are unreliable on the Docker bridge network without a dedicated init script. A single RabbitMQ node demonstrates all messaging principles just as well.

**Start everything locally:**

```bash
docker compose up -d
```

Wait ~30s for all services to be healthy, then verify:

```bash
curl http://localhost:8080/api/analytics/health/services | jq
```

---

## 7. Prioritized Action Checklist

### Critical — System feels broken or unconvincingly distributed without these

- [ ] **Fix RabbitMQ cluster** — Remove `rabbitmq-2`/`rabbitmq-3` or fix the clustering script. Single node is honest; a broken cluster is worse than no cluster.
- [ ] **Conflict-service: atomic capacity check** — Replace the check → increment sequence with `SELECT FOR UPDATE` or a Redis `SETNX` lock keyed on `user_id:departure_time`. Two concurrent bookings on the same segment both pass today.
- [ ] **Analytics: idempotent consumers** — Track processed event IDs in Redis. Every RabbitMQ redelivery currently doubles event counts.
- [ ] **Wire services to Redis Sentinel URL** — Sentinel is running but services connect directly to `redis:6379`. Failover does nothing because clients don't reconnect to the new primary.

### High impact, low effort

- [ ] **Analytics: periodic rollup job** — `hourly_stats` is wired up but empty. 50 lines of Go adds a real time-series aggregation story.
- [ ] **Notification: consumer idempotency** — Mirror the analytics fix; prevents duplicate notifications on redelivery.
- [ ] **User Service: publish `user.registered` event** — The only service that doesn't publish events; closes the event bus gap.
- [ ] **Enforcement: cache license → user_id in Redis** — Every `/verify/license` call hits user-service synchronously; one `SET`/`GET` eliminates this.
- [ ] **Journey: compensating transaction on saga failure** — If conflict-service is down, journeys are rejected and never retried. Add a `RETRY` status and a backoff task.
- [ ] **Conflict-service: Redis distributed lock (`SETNX`)** — Demonstrably prevents the double-booking race condition.
- [ ] **All consumers: DLQ reprocessing endpoint** — An admin endpoint that drains the DLQ and requeues messages shows you understand poison messages.

### High impact, higher effort

- [ ] **OpenTelemetry + Jaeger** — Correlation IDs exist but there is no span tree. Adding OTEL + Jaeger (free, runs in Docker) gives a visual proof of request paths across services. Single biggest demo upgrade.
- [ ] **Persist partition queue to Redis** — The in-memory partition queue in journey-service is lost on restart; persisting it makes the partition recovery story real.
- [ ] **Readiness vs liveness probes** — Split `/health` into `/health/live` and `/health/ready` (ready = dependencies connected).

### Nice to have

- [ ] **Token blacklisting on logout** — Redis `SET` on logout, check on every request.
- [ ] **Enforcement: circuit breaker on user-service call** — `shared/circuit_breaker.py` already exists; wire it in.
- [ ] **Remove `version: '3.8'` from compose** — Eliminates the deprecation warning on every `docker compose` command.

### Suggested additions not on the wishlist

**Chaos testing** — A 5-line bash loop that `docker stop`s a random service every 30s and restarts it after 10s proves your circuit breakers, auto-reconnect, and outbox actually work under failure. This is the single most impressive demo technique.

```bash
while true; do
  svc=$(docker compose ps --services | shuf -n 1)
  docker compose stop "$svc" && sleep 10 && docker compose start "$svc"
  sleep 30
done
```

**Idempotency key propagation across the saga** — Journey-service deduplicates on idempotency key, but conflict-service gets a second HTTP call with a different journey ID on retry. Forwarding the idempotency key through the saga makes the whole flow truly idempotent end-to-end.

**Versioned event contracts** — Add a `"schema_version": 1` field to events and show what happens when a consumer gets a v2 event it doesn't understand. Demonstrates evolutionary schemas, one of the hardest real-world distributed problems.

**Backpressure demo** — Add an `/admin/pause` endpoint to analytics that sets `prefetch=0` (pauses consumption), lets messages queue up in RabbitMQ, then resumes. Live demo of backpressure using QoS.

**Blue-green deploy** — Add an `analytics-service-v2` entry to compose pointing at the same image with a different config, and show nginx switching between them without downtime.

---

## 8. Implementation Plan — Overnight Enhancements

These are high-impact changes scoped for a single overnight session. Each builds on the existing codebase without infrastructure overhauls.

### Phase 1 — Distributed Trace Propagation (Cross-Language)

Building on existing `shared/tracing.py` and `CorrelationIDMiddleware`.

**Python services (User, Journey, Enforcement)**

Goal: ensure `X-Correlation-ID` is propagated in all outgoing `httpx` and `aio-pika` calls.

- Update `shared/messaging.py` to always set `correlation_id` from the current context when publishing.
- Add a helper to inject the ID into `httpx.AsyncClient` headers for inter-service calls (used by journey→conflict and enforcement→user calls).

**Go services (Conflict, Notification, Analytics)**

Goal: equivalent tracing in Go.

- Create a `shared/tracing` Go package (this does not exist yet).
- Implement a `chi` middleware that extracts `X-Correlation-ID` from incoming requests and sets it on the context.
- Inject the ID into `log.Printf` output and into RabbitMQ consumer message metadata for cross-service log correlation.

**Dependencies:** None new — `chi` is already used in all Go services.

---

### Phase 2 — Resilience and Retries

**Python — add `tenacity` retries**

- **Journey Service:** Wrap the synchronous conflict-service HTTP call with `@retry(stop=stop_after_attempt(3), wait=wait_exponential())`. This handles transient timeouts without opening the circuit breaker immediately.
- **User Service:** Add retry logic around the DB connection initialization on startup so the service waits for Postgres rather than exiting.

**Go — startup retry loop**

- **Conflict Service:** Replace the single-attempt `sql.Open` at startup with a retry loop (up to 10 attempts, 2s backoff). Standard Go pattern, no library needed.
- **Notification Service:** Add retry logic in the RabbitMQ consumer's `ack` call — if the broker restarts mid-delivery the channel is closed; reconnect and nack rather than panic.

**Dependencies:** Add `tenacity` to Python `requirements.txt` (already present in some services — verify before adding).

---

### Phase 3 — Outbox Pattern Completeness

> **Note:** The journey service already has a transactional outbox — `journey_events` table written in the same DB transaction as the journey row, drained by a background thread. This phase is not about adding the outbox; it is about closing gaps in the existing implementation.

**Gaps to close:**

- The background drain thread commits each event in a separate DB transaction from the RabbitMQ publish. If the process crashes after publish but before the DB commit, the event is re-published on restart (at-least-once is preserved but the gap should be documented).
- Add a `published_at` timestamp column to `journey_events` so you can query unpublished events on startup and drain them immediately rather than waiting for the poll interval.
- Store event payloads as JSON directly in the `payload` column (already the case — confirm this is consistent across all event types).

**If extending to other services:** Apply the same outbox pattern to the user-service for the `user.registered` event (Phase 1 of the action checklist).

---

### Phase 4 — Idempotency on User Registration

**User Service:** Add a duplicate-registration guard.

- On `POST /register`, before inserting, check if a row with the same `license_number` or `email` already exists.
- If it does and an `Idempotency-Key` header is present, return `200 OK` with the existing user rather than `409 Conflict`. This makes the endpoint safe to retry.
- If no idempotency key is present, return `409` as today.

This is a DB-level check, no new infrastructure needed. Use `INSERT ... ON CONFLICT DO NOTHING RETURNING *` and check whether a row was returned.

---

### Open Questions

- **Outbox payload format:** Storing event payloads as JSON directly in the `outbox` table column is recommended. This is already the case in journey-service — confirm consistency.
- **`tenacity` dependency:** Already present in journey-service `requirements.txt`. Check user-service and enforcement-service before adding again to avoid version conflicts.
- **Go retry library:** `avast/retry-go` is one option but standard Go retry loops are sufficient here and add no dependency. Prefer the standard pattern.

---

## 9. Demo Script

Run these steps in order — each proves a specific distributed systems principle. Total time: ~17 minutes. Stop after step 4 if time is short; the outbox demo is the most impressive single thing in the project.

### Step 1 — Show the architecture is alive (2 min)

```bash
curl http://localhost:8080/api/analytics/health/services | jq
```

Point at: multiple independent services, each with its own DB. Open the RabbitMQ management UI at `http://localhost:15672` — show exchanges, queues, and consumers connected.

**Principle demonstrated:** Service decomposition, independent deployability.

---

### Step 2 — Register, log in, book a journey — trace the saga (3 min)

```bash
# Register
curl -X POST http://localhost:8080/api/users/register \
  -H "Content-Type: application/json" \
  -d '{"username":"demo","email":"demo@example.com","password":"password123","license_number":"ABC123"}'

# Login
TOKEN=$(curl -s -X POST http://localhost:8080/api/users/login \
  -H "Content-Type: application/json" \
  -d '{"username":"demo","password":"password123"}' | jq -r .token)

# Book a journey
curl -X POST http://localhost:8080/api/journeys/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-booking-001" \
  -d '{"start_location":"Dublin","end_location":"Cork","departure_time":"2025-06-01T09:00:00Z","route_points":[[53.3498,-6.2603],[51.8985,-8.4756]]}'
```

Open two terminals watching logs:

```bash
docker compose logs -f journey-service
docker compose logs -f conflict-service
```

Show journey-service calling conflict-service over REST (saga call), receiving approval, then writing the DB row.

**Principle demonstrated:** Saga pattern, synchronous coordination.

---

### Step 3 — Show async event fan-out (2 min)

After the booking completes:

```bash
docker compose logs -f notification-service analytics-service
```

Both services received the same `journey.confirmed` event from RabbitMQ. Show the stats endpoint updating:

```bash
curl http://localhost:8080/api/analytics/stats | jq
```

**Principle demonstrated:** Async messaging, publish-subscribe, eventual consistency.

---

### Step 4 — Kill RabbitMQ, book a journey, restart RabbitMQ (3 min)

```bash
docker compose stop rabbitmq

# Book a journey via the API — it succeeds (outbox buffers the event)
curl -X POST http://localhost:8080/api/journeys/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: demo-booking-002" \
  -d '{"start_location":"Dublin","end_location":"Galway","departure_time":"2025-06-02T10:00:00Z","route_points":[[53.3498,-6.2603],[53.2707,-9.0568]]}'

docker compose start rabbitmq

# Watch the outbox drain
docker compose logs -f journey-service | grep outbox
```

The journey was booked, the event was written to the outbox in the same DB transaction, and when RabbitMQ came back the event was delivered.

**Principle demonstrated:** Transactional outbox, at-least-once delivery, durability.

---

### Step 5 — Kill conflict-service, show circuit breaker opening (2 min)

```bash
docker compose stop conflict-service

# Try to book 3 journeys — each fails fast after the circuit opens
# (check logs for circuit state transitions)
docker compose logs journey-service | grep "circuit"

docker compose start conflict-service
# Next booking succeeds — circuit half-opens, probes, then closes
```

**Principle demonstrated:** Circuit breaker, fail-fast, self-healing.

---

### Step 6 — Show enforcement cache (2 min)

```bash
# First call: cache miss, calls user-service
curl http://localhost:8080/api/enforcement/verify/license/ABC123 -H "Authorization: Bearer $TOKEN"

# Second call: sub-millisecond Redis cache hit (check X-Cache header)
curl -v http://localhost:8080/api/enforcement/verify/license/ABC123 -H "Authorization: Bearer $TOKEN"

# Cancel the journey, watch cache invalidation via event
curl -X DELETE http://localhost:8080/api/journeys/<journey_id> -H "Authorization: Bearer $TOKEN"
curl http://localhost:8080/api/enforcement/verify/license/ABC123 -H "Authorization: Bearer $TOKEN"
```

**Principle demonstrated:** Caching, cache invalidation via events, eventual consistency.

---

### Step 7 — Show read replicas (1 min)

Point at the `DATABASE_READ_URL` env vars in the compose file. Show the replica has the same data by querying it directly:

```bash
docker exec -it <postgres-replica-container> psql -U user -d users_db -c "SELECT id, username FROM users LIMIT 5;"
```

**Principle demonstrated:** Read/write separation, database replication.

---

### Step 8 — Simulate a network partition (2 min)

```bash
docker network disconnect journey-net distributed-traffic-service-rabbitmq-1

# Watch partition detection in journey-service logs
docker compose logs journey-service | grep "PARTITIONED"

docker network connect journey-net distributed-traffic-service-rabbitmq-1

# Watch recovery
docker compose logs journey-service | grep "recovered"
```

**Principle demonstrated:** Partition detection, CAP theorem trade-offs, staleness flagging.

---

## 10. API Reference

All endpoints go through the nginx gateway at `http://localhost:8080`. All protected endpoints require `Authorization: Bearer <token>`.

### Auth

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/users/register` | Register a new user |
| `POST` | `/api/users/login` | Login, returns JWT |
| `GET` | `/api/users/profile` | Get own profile |

### Journeys

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/journeys/` | Book a journey (supply `Idempotency-Key` header) |
| `GET` | `/api/journeys/` | List own journeys |
| `GET` | `/api/journeys/{id}` | Get journey detail |
| `DELETE` | `/api/journeys/{id}` | Cancel a journey |

### Enforcement

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/enforcement/verify/plate/{plate}` | Verify booking by plate |
| `GET` | `/api/enforcement/verify/license/{number}` | Verify booking by licence |

### Analytics

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/analytics/stats` | Current booking stats |
| `GET` | `/api/analytics/health/services` | Aggregated health of all services |
| `GET` | `/api/analytics/events` | Recent event log |

### Notifications

| Method | Path | Description |
|---|---|---|
| `WS` | `/ws/notifications` | WebSocket stream for real-time push |
| `GET` | `/api/notifications/history` | Past notifications (Redis-backed) |

### Health

Every service exposes `GET /health` returning `{"status": "healthy", "service": "..."}`.
