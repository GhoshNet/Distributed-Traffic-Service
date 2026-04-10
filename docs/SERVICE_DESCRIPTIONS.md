# Distributed Journey Booking System — Service Descriptions
**CS7NS6 Distributed Systems — Exercise 2**

---

## Architecture Overview

The system is a distributed microservices application for pre-booking road journeys. It consists of **6 backend microservices**, an **Nginx API gateway**, and a **browser frontend**. All communication between services is either synchronous REST (for booking flows requiring consistency) or asynchronous RabbitMQ events (for notifications, analytics, and cache population).

```
Browser (Frontend)
      │
      ▼
 Nginx Gateway :8080
      │
      ├── /api/users/       → User Service :8001
      ├── /api/journeys/    → Journey Service :8002
      ├── /api/conflicts/   → Conflict Service :8003
      ├── /api/notifications/→ Notification Service :8004
      ├── /ws/notifications/ → Notification Service (WebSocket)
      ├── /api/enforcement/ → Enforcement Service :8005
      └── /api/analytics/   → Analytics Service :8006

RabbitMQ (journey_events exchange, topic routing)
      │
      ├── journey.confirmed / rejected / cancelled / started / completed
      │         ├── Notification Service (consume → WebSocket push)
      │         ├── Enforcement Service  (consume → Redis cache update)
      │         └── Analytics Service   (consume → counters + event log)
      └── user.registered
                └── Analytics Service

PostgreSQL (one database per service)
Redis (shared instance, separate DBs per service)
```

---

## 1. User Service

**Port:** 8001 | **Language:** Python 3.12 | **Framework:** FastAPI

### What It Does
Manages all user accounts and vehicle registrations. It is the identity authority for the entire system — every JWT token is issued here and validated against a shared secret by all other services. It is the only service that needs to work across multiple laptops during demos, because a user registered on Node A must be able to log in from Node B.

### Technology Stack
| Component | Choice |
|-----------|--------|
| Framework | FastAPI 0.115 + uvicorn |
| Database | PostgreSQL via SQLAlchemy (async, asyncpg driver) |
| Auth | JWT (PyJWT) with bcrypt password hashing |
| Locking | Redis DB 3 (distributed lock) |
| Replication | HTTP (httpx async client) |
| Messaging | RabbitMQ via aio-pika |

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/users/register` | Register new driver (distributed lock + replication) |
| `POST` | `/api/users/register/agent` | Register enforcement agent (no lock, local only) |
| `POST` | `/api/users/login` | Authenticate, return JWT token |
| `GET` | `/api/users/me` | Get current user's profile |
| `GET` | `/api/users/license/{license}` | Lookup user by license number (used by enforcement) |
| `POST` | `/api/users/vehicles` | Register a vehicle to the current user |
| `GET` | `/api/users/vehicles` | List current user's vehicles |
| `DELETE` | `/api/users/vehicles/{id}` | Remove a vehicle |
| `GET` | `/api/users/vehicles/verify/{reg}` | Confirm vehicle belongs to user (internal) |
| `POST` | `/internal/users/lock` | Acquire distributed email lock (peer-to-peer) |
| `POST` | `/internal/users/unlock` | Release distributed email lock (peer-to-peer) |
| `POST` | `/internal/users/replicate` | Receive a replicated user record from a peer |
| `POST` | `/internal/vehicles/replicate` | Receive a replicated vehicle record from a peer |
| `GET` | `/internal/users/all` | Export all users + vehicles for catch-up sync |
| `POST` | `/internal/peers/register` | Register a new peer at runtime |
| `GET` | `/admin/logs` | Recent log entries (for activity feed) |
| `POST` | `/admin/simulate/fail` | Simulate node crash (returns 503 for all endpoints) |
| `POST` | `/admin/simulate/recover` | Recover from simulated crash |

### Distributed Systems Concepts

**Consistent-Hash Sharding**
Every email is hashed: `shard_id = MD5(email.lower()) % num_nodes`. The node where `shard_id == 0` is the *home shard* — the authoritative writer for that user. This is logged on every registration so the Distributed Activity Feed shows which node is PRIMARY vs REPLICA for each account. All nodes still replicate all data; sharding is about write authority, not data isolation.

**Distributed Lock (Redlock-style, 2-phase)**
Before inserting a new user, the registering node:
1. Acquires `user_email_lock:<email>` via Redis SETNX with a 15s TTL on its local Redis (DB 3)
2. POSTs `/internal/users/lock` to every peer — each peer checks email uniqueness + acquires its own SETNX
3. If ANY peer rejects (because the email already exists there, or lock contention), the requesting node rolls back all acquired locks and returns HTTP 409
4. If all peers agree, the user is written locally and the lock is released on all nodes

This prevents the split-brain scenario where two nodes simultaneously register the same email address.

**Active-Active Replication**
After a successful registration, the node fires off an async HTTP push to every peer (`/internal/users/replicate`). Peers apply the record idempotently — if the user ID or email already exists, the insert is silently skipped. Same pattern for vehicles.

**Catch-up Sync**
On startup (after a 5-second delay to let its own DB settle), the service fetches the full user + vehicle snapshot from every peer via `GET /internal/users/all`. On rejoin after downtime it fills any gap that accumulated while it was offline. A background loop repeats this every 5 minutes.

**Node Failure Simulation**
A middleware intercepts all requests. When the failure flag is set, every endpoint except `/health` and `/admin/simulate/recover` returns HTTP 503, making the entire node appear dead to clients and health monitors.

### Key Files
```
user-service/app/
├── main.py           — FastAPI app, lifecycle hooks, failure simulation
├── routes.py         — Public API (register, login, vehicles)
├── service.py        — Business logic (transactions, bcrypt, JWT)
├── database.py       — SQLAlchemy models: User, Vehicle
├── replication.py    — Distributed lock, sharding, push/pull sync
└── internal_routes.py— Peer coordination endpoints
```

---

## 2. Journey Service

**Port:** 8002 | **Language:** Python 3.12 | **Framework:** FastAPI

### What It Does
The core booking service. Accepts journey requests, orchestrates the booking flow (either a Saga or a Two-Phase Commit), manages journey lifecycle from PENDING through IN_PROGRESS to COMPLETED, and awards points for good behaviour. It is the health authority for the whole node — its `/health` endpoint is what the nginx gateway exposes to peer nodes for liveness detection.

### Technology Stack
| Component | Choice |
|-----------|--------|
| Framework | FastAPI 0.115 + uvicorn |
| Database | PostgreSQL (Journey, OutboxEvent, IdempotencyRecord tables) |
| Caching | Redis DB 1 (points balances) |
| Messaging | RabbitMQ (publisher + Transactional Outbox) |
| Conflict check | HTTP call to conflict-service |

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/journeys/?mode=saga` | Book journey via Saga (default) |
| `POST` | `/api/journeys/?mode=2pc` | Book journey via Two-Phase Commit |
| `GET` | `/api/journeys/` | List current user's journeys (paginated) |
| `GET` | `/api/journeys/all` | Admin: list all journeys in system |
| `GET` | `/api/journeys/{id}` | Get a specific journey |
| `DELETE` | `/api/journeys/{id}` | Cancel a journey (releases capacity) |
| `GET` | `/api/journeys/points/balance` | Driver's points balance |
| `GET` | `/api/journeys/points/history` | Points transaction log |
| `POST` | `/api/journeys/points/spend` | Spend points (SELECT FOR UPDATE) |
| `GET` | `/api/journeys/vehicle/{reg}/active` | Active journeys for a vehicle (enforcement) |
| `GET` | `/api/journeys/user/{id}/active` | Active journeys for a user (enforcement) |
| `GET` | `/health` | Service health (also gates the node's ALIVE status) |
| `GET` | `/health/nodes` | All known peer nodes and their ALIVE/SUSPECT/DEAD state |
| `GET` | `/health/partitions` | Current partition detection status |
| `POST` | `/admin/peers/register` | Register a peer health monitor URL at runtime |
| `POST` | `/admin/simulate/fail` | Simulate node crash (cascades to user-service) |
| `POST` | `/admin/simulate/recover` | Recover (cascades to user-service) |
| `POST` | `/admin/recovery/drain-outbox` | Manually replay unpublished outbox events |

### Distributed Systems Concepts

**Saga Pattern (default booking mode)**
```
Client → POST /api/journeys/
          ↓
  [1] Write journey to DB as PENDING (committed)
          ↓
  [2] Call conflict-service: POST /api/conflicts/check
          ↓
      CONFLICT? → Update to REJECTED → publish journey.rejected
      NO CONFLICT? → Update to CONFIRMED → publish journey.confirmed
      TIMEOUT? → Update to REJECTED (circuit breaker trips after 3 failures)
```
Advantages: simple, fast, always returns a definitive answer. The compensating action (rejection) is the only rollback needed since capacity is reserved atomically in conflict-service.

**Two-Phase Commit (optional `?mode=2pc`)**
```
PREPARE:  POST /api/conflicts/check  →  capacity locked in conflict-service DB
           (Serializable TX — no other booking for same slot can proceed)

COMMIT:   UPDATE journey SET status=CONFIRMED + INSERT outbox event
           (single local DB transaction — atomic)

ABORT:    if commit fails after PREPARE:
           POST /api/conflicts/cancel/{journey_id}  ← compensating transaction
           releases the reserved capacity on conflict-service
```
Stronger consistency guarantee — capacity reservation and journey confirmation are made atomic. Aborts trigger explicit capacity release so no phantom slots accumulate.

**Transactional Outbox Pattern**
Journey status update and the outbox event are written in the same PostgreSQL transaction. A background task polls the outbox table every 5 seconds and publishes any unpublished events to RabbitMQ. This guarantees at-least-once delivery even if RabbitMQ is temporarily unreachable at the moment of booking.

**Idempotency**
Clients may pass an `idempotency_key` header. If the same key arrives twice, the second request returns the existing journey instead of creating a duplicate. This handles network retries safely.

**Points System**
Points balances are stored in Redis with `SELECT FOR UPDATE` semantics (WATCH/MULTI/EXEC) to prevent race conditions on concurrent spend requests. Points are earned on confirmation (+10) and deducted on late cancellation (-5).

**Peer Health Monitor (ALIVE/SUSPECT/DEAD)**
A background task pings every registered peer's `/health` endpoint every 10 seconds. After 2 consecutive failures a peer moves to SUSPECT; after 5 it moves to DEAD. The frontend reads this state from `/health/nodes` and uses it to decide which nodes are eligible for API failover.

**Node Failure Simulation**
`POST /admin/simulate/fail` sets an in-process flag AND forwards the same call to user-service. Both services then return 503, making the entire node appear dead. Recovery reverses this on both.

### Key Files
```
journey-service/app/
├── main.py            — FastAPI app, health monitor, partition manager
├── routes.py          — All journey endpoints
├── service.py         — JourneyService (create, cancel, list, points)
├── saga.py            — BookingSaga (conflict check, circuit breaker, event publish)
├── coordinator.py     — TwoPhaseCoordinator (PREPARE/COMMIT/ABORT)
├── points.py          — PointsService (earn, spend, history)
├── database.py        — Journey, OutboxEvent, IdempotencyRecord models
├── outbox_publisher.py— Background drain of unpublished events
└── scheduler.py       — Lifecycle transitions (PENDING→IN_PROGRESS→COMPLETED)
```

---

## 3. Conflict Service

**Port:** 8003 | **Language:** Go 1.20+ | **Framework:** Chi v5

### What It Does
The capacity police of the system. Every booking request must pass through here before it can be confirmed. It checks three things in a single Serializable transaction: (1) does the driver already have an active journey that overlaps in time? (2) does the vehicle? (3) are any road segments along the entire route already at capacity? It also maintains a full replica of all active booking slots from all peer nodes for cross-node conflict detection.

### Technology Stack
| Component | Choice |
|-----------|--------|
| Framework | Go + Chi v5 router |
| Database | PostgreSQL (Serializable isolation, pgx driver) |
| Spatial | Custom grid-cell partitioning |
| Replication | HTTP push/pull (goroutines) |
| Messaging | RabbitMQ (AMQP) |
| Logging | Custom ring buffer |

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/conflicts/check` | Check journey, reserve capacity if clear |
| `POST` | `/api/conflicts/cancel/{journey_id}` | Release capacity (compensating transaction) |
| `GET` | `/api/routes` | List all 26 predefined routes with waypoints |
| `GET` | `/api/conflicts/routes` | Alias (via nginx) |
| `GET` | `/internal/slots/active` | Export all active slots (for peer catch-up) |
| `POST` | `/internal/slots/replicate` | Receive a replicated slot from a peer |
| `POST` | `/internal/slots/cancel` | Receive a peer cancellation |
| `POST` | `/internal/peers/register` | Add peer at runtime + trigger sync |
| `GET` | `/internal/shard/info` | Shard assignment for all known routes |
| `GET` | `/admin/logs` | Recent log ring buffer (for activity feed) |
| `GET` | `/health` | Service health |

### Distributed Systems Concepts

**Serializable Transactions for Conflict Detection**
The entire check-and-reserve is wrapped in a single PostgreSQL Serializable transaction. This is the highest isolation level — it prevents phantom reads where two concurrent bookings both see "no conflict" and both proceed. Under the hood PostgreSQL uses Serializable Snapshot Isolation (SSI) which detects conflicting concurrent transactions and aborts one of them.

**Three-Part Conflict Check (SELECT FOR UPDATE)**
Within the Serializable transaction:
1. **Driver overlap**: `SELECT … FROM booked_slots WHERE user_id = ? AND time_ranges_overlap(…) FOR UPDATE` — locks any row that could conflict, blocking a concurrent booking for the same driver
2. **Vehicle overlap**: Same pattern keyed on `vehicle_registration`
3. **Road capacity**: For every grid cell along the path at the interpolated time slot: `SELECT … FROM road_segment_capacity WHERE grid_lat=? AND grid_lng=? AND time_slot=? FOR UPDATE` — if `current_bookings >= max_capacity` (default 5) then CONFLICT

**Grid-Cell Spatial Partitioning**
The entire route is decomposed into ~1km grid cells (0.01° resolution). For straight-line paths a step-by-step walk is used so no cell is ever skipped. For predefined routes the actual road waypoints are used for more accurate path coverage. Each cell is checked and reserved independently, so a journey from Dublin→Galway can conflict with a journey from Athlone→Galway on their shared segment even if their total paths differ.

**Consistent-Hash Sharding**
Routes are assigned to shard nodes: `shard = MD5(route_id) % num_nodes`. The node where `shard == 0` is PRIMARY for that route's bookings. This is logged on every conflict check: `[shard] conflict-check journey=… route=… shard=1/2 role=REPLICA`. All nodes still check and store all slots — sharding is for write authority visibility, not data isolation.

**Cross-Node Replication**
When a booking slot is committed locally, the service asynchronously pushes the slot to every peer via `POST /internal/slots/replicate`. Peers apply it idempotently (SELECT EXISTS before INSERT). This ensures that when a user on Node B tries to book the same slot that was just booked on Node A, Node B's conflict check finds the slot and rejects it.

Catch-up sync runs on startup (after a 3-second delay) and every 5 minutes to fill any gaps from missed pushes while a node was offline.

**RabbitMQ Consumer**
Listens for `journey.cancelled` events. When a journey is cancelled through the journey-service, the conflict-service deactivates the slot locally, decrements road capacity for every grid cell that was reserved, and gossips the cancellation to all peers asynchronously.

### Key Files
```
conflict-service/
├── main.go        — Server setup, peer loading, startup sync
├── handlers.go    — HTTP handlers
├── service.go     — Core logic: conflict detection, grid walk, capacity
├── sharding.go    — MD5 consistent-hash shard assignment
├── replication.go — Peer management, push/pull sync
├── consumer.go    — RabbitMQ journey.cancelled handler
├── logbuffer.go   — Ring buffer (for /admin/logs activity feed)
├── database.go    — PostgreSQL init, schema creation
└── config.go      — Environment config loading
```

---

## 4. Notification Service

**Port:** 8004 | **Language:** Go 1.20+ | **Framework:** Chi v5

### What It Does
Delivers real-time push notifications to drivers. It subscribes to all journey events on RabbitMQ, formats them into human-readable messages, and pushes them immediately to any connected browser via WebSocket. If the browser is not connected, the notification is stored in Redis and retrieved when the user polls or reconnects.

### Technology Stack
| Component | Choice |
|-----------|--------|
| Framework | Go + Chi v5 |
| WebSocket | Gorilla websocket library |
| Storage | Redis (notification history + deduplication) |
| Messaging | RabbitMQ (all journey.* events) |
| Auth | JWT validation (same secret as all services) |

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health |
| `GET` | `/api/notifications/?token=X&limit=20` | Fetch stored notification history |
| `GET` | `/ws/notifications/?token=X` | WebSocket — real-time push |

### Distributed Systems Concepts

**WebSocket Connection Registry**
An in-memory map keyed by `user_id` holds all active WebSocket connections. A single user can have multiple open connections (multiple browser tabs). On message delivery, the service fans out to all of them. Dead connections are cleaned up lazily on next write error.

**At-Least-Once Delivery with Deduplication**
RabbitMQ delivers events at-least-once. The service deduplicates using a Redis key `notif:processed:{MessageId}` with a 24-hour TTL. If the same event is delivered twice (e.g. after a broker restart), the second delivery is silently dropped.

**Event Templates**
Every journey event is mapped to a user-friendly message:
- `journey.confirmed` → "Your journey from {origin} to {destination} on {date} has been **confirmed**"
- `journey.rejected` → "...was **rejected**. Reason: {rejection_reason}"
- `journey.cancelled` → "...has been **cancelled**"
- `journey.started` → "...has **started**. Drive safely!"
- `journey.completed` → "...is **complete**"

### Key Files
```
notification-service/
├── main.go      — Server setup, Redis, RabbitMQ consumer start
├── handlers.go  — /health, /api/notifications, /ws/notifications
├── consumer.go  — RabbitMQ subscriber, template rendering, dedup
├── redis.go     — Redis client init
├── auth.go      — JWT validation
└── config.go    — Config loading
```

---

## 5. Enforcement Service

**Port:** 8005 | **Language:** Python 3.12 | **Framework:** FastAPI

### What It Does
Provides sub-500ms roadside verification for enforcement agents. Given a vehicle registration or driver's license number, it instantly confirms whether the driver has a valid active journey. It uses a Redis cache as the primary lookup, populated in real time by RabbitMQ events, with a fallback to the journey-service REST API if the cache is cold. It continues to serve stale cached data when the journey-service is partitioned, and adds warning headers so callers know the data may be outdated.

### Technology Stack
| Component | Choice |
|-----------|--------|
| Framework | FastAPI |
| Cache | Redis DB 4 (enforcement namespace) |
| Backup | HTTP calls to journey-service + user-service |
| Messaging | RabbitMQ (journey.confirmed, started, cancelled, completed) |
| HA | Redis Sentinel (optional) |

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/enforcement/verify/vehicle/{reg}` | Verify vehicle has active journey (agent role required) |
| `GET` | `/api/enforcement/verify/license/{license}` | Verify driver by license (agent role required) |
| `GET` | `/health` | Service health |
| `GET` | `/health/partitions` | Partition status for dependencies |

### Distributed Systems Concepts

**Layered Cache Strategy**
```
Request arrives
      ↓
[1] Redis cache   key: active_journey:vehicle:{reg}
    HIT  → return immediately (< 1ms)
    MISS ↓
[2] Journey Service API  GET /api/journeys/vehicle/{reg}/active
    HIT  → populate cache, return
    MISS → return is_valid: false
```

**Cache Population via Events**
The RabbitMQ consumer keeps the cache current without any polling:
- `journey.confirmed` / `journey.started` → `SETEX active_journey:vehicle:{reg} {json} {ttl_seconds}`
- `journey.cancelled` / `journey.completed` → `DEL active_journey:vehicle:{reg}`

TTL is set to `(estimated_arrival_time - now) + 3600` seconds so the cache entry outlasts the journey slightly.

**License → User ID Cache**
A separate Redis key `license_user_id:{license}` caches the mapping with a 24-hour TTL, avoiding repeated calls to user-service for every license plate check.

**Partition Tolerance**
When journey-service is detected as partitioned, the service returns whatever is in Redis (potentially stale) with response headers:
- `X-Data-Staleness: STALE`
- `X-Partition-Status: journey-service:PARTITIONED`

This allows roadside enforcement to continue operating during network splits, accepting that some data may be a few minutes old.

### Key Files
```
enforcement-service/app/
├── main.py     — FastAPI app, partition manager, endpoints
├── service.py  — EnforcementService (cache lookup, API fallback, license mapping)
└── consumer.py — RabbitMQ event handler (cache population/eviction)
```

---

## 6. Analytics Service

**Port:** 8006 | **Language:** Go 1.20+ | **Framework:** Chi v5

### What It Does
Aggregates all journey and user events into counters, hourly rollups, and audit logs. It also monitors PostgreSQL replication lag across all replica databases and provides a cross-service health dashboard. It is the observability backbone of the system — the frontend activity feed indirectly uses its data via the per-service `/admin/logs` endpoints.

### Technology Stack
| Component | Choice |
|-----------|--------|
| Framework | Go + Chi v5 |
| Database | PostgreSQL (event_logs, hourly_stats tables) |
| Counters | Redis (daily hash counters, deduplication) |
| Messaging | RabbitMQ (journey.* and user.* events) |
| HA | Redis Sentinel (optional) |

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health |
| `GET` | `/api/analytics/stats` | Real-time counters (daily + all-time + last hour) |
| `GET` | `/api/analytics/events?event_type=X&limit=50` | Full event history with filter |
| `GET` | `/api/analytics/hourly?limit=24` | Hourly booking aggregations |
| `GET` | `/api/analytics/replica-lag` | PostgreSQL replication lag per replica |
| `GET` | `/api/analytics/health/services` | Health check of all 6 services |

### Distributed Systems Concepts

**Event Sourcing**
Every `journey.*` and `user.*` event from RabbitMQ is written to the `event_logs` table with full metadata (event_type, journey_id, user_id, origin, destination, timestamp). This is an immutable audit trail — nothing is ever deleted from it.

**Dual-Write Counters**
On each event, the service simultaneously:
1. Inserts the full event into `event_logs` (PostgreSQL — durable)
2. Increments a Redis hash `analytics:daily:{YYYY-MM-DD}` (fast, ephemeral — 48h TTL)

The stats endpoint reads from Redis for real-time counters and from PostgreSQL for all-time totals.

**Event Deduplication**
Redis key `analytics:processed:{MessageId}` with 24h TTL. Duplicate RabbitMQ deliveries (at-least-once semantics) are silently dropped after the first processing.

**Hourly Rollup Job**
A background goroutine fires every hour and aggregates the past hour's events from `event_logs` into the `hourly_stats` table (columns: hour, total_bookings, confirmed, rejected, cancelled). The `/api/analytics/hourly` endpoint reads this table and returns the last N hours as a time series.

**PostgreSQL Replication Lag Monitoring**
The service queries `pg_stat_replication` on the primary PostgreSQL node. It returns `write_lag`, `flush_lag`, and `replay_lag` for every connected replica. This exposes the replication health of the entire database tier in one API call.

**Multi-Service Health Aggregation**
`GET /api/analytics/health/services` probes all 6 backend services' `/health` endpoints in parallel, records the response time for each, and returns a single JSON object with `overall_status: healthy` or `degraded`. This is used by the frontend to render the service health panel.

### Key Files
```
analytics-service/
├── main.go      — Server setup, hourly rollup background job
├── handlers.go  — stats, events, hourly, replica-lag, service-health
├── consumer.go  — RabbitMQ consumer, event insert, daily counter update
├── database.go  — PostgreSQL init, event insert, query helpers
└── config.go    — Config loading
```

---

## 7. API Gateway (Nginx)

**Port:** 8080 | **Software:** Nginx 1.25

### What It Does
The single entry point for all browser traffic. Routes requests to the correct backend service, applies per-zone rate limiting to protect against abuse, handles WebSocket upgrades for the notification channel, and forwards CORS headers. Crucially, it uses dynamic DNS resolution so it never caches stale container IPs — any backend service can be restarted or recreated without requiring a gateway restart.

### Configuration Details

**Rate Limiting Zones**
| Zone | Rate | Burst | Applied to |
|------|------|-------|------------|
| `auth` | 5 req/s | 10 | `/api/users/` |
| `booking` | 10 req/s | 20 | `/api/journeys/` |
| `general` | 30 req/s | 10-20 | Everything else |

**Route Table**
| Path Prefix | Backend Service | Notes |
|-------------|----------------|-------|
| `/health` | journey-service | Node liveness check |
| `/api/users/` | user-service | Auth zone |
| `/api/journeys/` | journey-service | Booking zone |
| `/api/conflicts/` | conflict-service | General zone |
| `/api/notifications/` | notification-service | General zone |
| `/ws/notifications/` | notification-service | WebSocket (86400s timeout) |
| `/api/enforcement/` | enforcement-service | General zone |
| `/api/analytics/` | analytics-service | General zone |
| `/internal/users/` | user-service | Peer replication (no auth) |
| `/internal/vehicles/` | user-service | Peer replication |
| `/internal/peers/` | user-service | Peer registration |
| `/health/nodes` | journey-service | Peer health state |
| `/health/partitions` | journey-service | Partition status |
| `/admin/` | journey-service | Admin + simulation |

**Dynamic Upstream Resolution (key design choice)**
```nginx
location /api/users/ {
    set $svc "user-service:8000";
    proxy_pass http://$svc;    # no path — passes full original URI
}
```
Using `set $svc` forces Nginx to re-resolve `user-service` via Docker's embedded DNS (127.0.0.11) on every request with a 5-second TTL. This means:
- No stale IP after any container is recreated
- No `depends_on` needed — nginx can start before backends and will retry DNS on first request
- No gateway restart needed after service restarts

### Key File
```
api-gateway/nginx.conf  — Full routing, rate limiting, WebSocket config
```

---

## 8. Frontend

**Port:** 3000 | **Language:** Vanilla JavaScript | **Served by:** Nginx static

### What It Does
Single-page application for booking journeys, monitoring node health, and demonstrating distributed systems behaviours. It implements client-side failover so that if the primary API node goes down, the browser transparently reroutes all calls to a peer node — the user never sees a failure, just a brief "Failover" indicator in the topbar.

### Technology Stack
| Component | Choice |
|-----------|--------|
| Language | Vanilla JavaScript (no framework) |
| Maps | Leaflet.js + OpenStreetMap tiles |
| Auth storage | localStorage (JWT token + user profile) |
| Peer discovery | localStorage (peer URLs from /health/nodes) |
| Real-time | Browser WebSocket API |

### Key Features

**Resilient API Client (`resilientFetch`)**
Every API call goes through `resilientFetch`. On any 5xx or network error it immediately tries the next ALIVE peer from the peer list. This list is fetched from `/health/nodes` at login time and persisted in localStorage so it survives page refreshes and even works at the login screen before any authenticated call is made.

```
resilientFetch("/api/journeys/", options)
  → try primary node (localhost:8080)
    success → return
    5xx/error → try peer 1 (192.168.0.249:8080)
      success → update topbar to "⚡ Failover: 192.168.0.249"
      5xx/error → try peer 2 …
```

**WebSocket Failover**
The WebSocket to `/ws/notifications/` also fails over. After 2 consecutive disconnects from the primary, `connectWS()` cycles through the peer URL list to maintain the live data stream.

**Journey Booking Flow**
1. User selects origin/destination (geocoded via OpenStreetMap Nominatim, debounced 200ms)
2. Or picks a Quick Route from the predefined route dropdown (loaded from `/api/conflicts/routes`)
3. Selects vehicle from their registered vehicles
4. Chooses Saga or 2PC mode
5. Submits → green/red toast with outcome

**Distributed Activity Feed**
The Simulate tab shows a live merged log from all nodes. Every 5 seconds it fetches `/admin/logs` from the primary node and all registered peer nodes, merges all entries by UTC timestamp, and renders them with colour coding:
- Purple: `[replication]` — cross-node slot push/receive
- Blue: `[sync]` — catch-up sync
- Green: `CONFIRMED`
- Red: `REJECTED` or `SIMULATION`

**Node Health Panel**
The Simulate tab also shows ALIVE/SUSPECT/DEAD cards for each registered peer node, auto-refreshing from `/health/nodes` every 10 seconds.

**Node Failure Simulation**
- `💀 Kill Node` → POST `/admin/simulate/fail` on primary → both journey-service and user-service return 503
- `💚 Recover Node` → POST `/admin/simulate/recover` → both services return to normal
- Laptop B's peer card transitions: `ALIVE → SUSPECT (~30s) → DEAD (~60s)`

### Key Files
```
frontend/
├── index.html  — All markup: auth screen, booking form, map, simulate tab
├── app.js      — All logic (~1200 lines): login, booking, resilientFetch,
│                  WebSocket, geocoding, Leaflet map, activity feed
└── style.css   — Styling
```

---

## Shared Modules (Python)

**Path:** `shared/` — imported by all four Python services

| Module | What It Provides |
|--------|-----------------|
| `auth.py` | `create_access_token()`, `get_current_user()` FastAPI dependency, `require_role()` |
| `messaging.py` | `MessageBroker` — async RabbitMQ client with auto-reconnect, publish, subscribe, dead-letter setup |
| `schemas.py` | All Pydantic DTOs: `UserRegisterRequest`, `JourneyCreateRequest`, `ConflictCheckRequest`, `VerificationResponse`, `HealthResponse`, etc. |
| `config.py` | `setup_logging()` with in-memory ring buffer; `get_recent_logs(limit)` for `/admin/logs` endpoints |
| `partition.py` | `PartitionManager` — probes dependencies every 5s, transitions CONNECTED→SUSPECTED→PARTITIONED |
| `health_monitor.py` | `PeerHealthMonitor` — ALIVE/SUSPECT/DEAD state machine for peer nodes |
| `circuit_breaker.py` | `CircuitBreaker` — CLOSED/OPEN/HALF_OPEN, protects conflict-service calls |
| `recovery.py` | `drain_outbox_backlog()`, `rebuild_enforcement_cache()` — post-partition recovery helpers |
| `tracing.py` | `CorrelationIDMiddleware` — propagates `X-Correlation-ID` across service calls |

---

## Data Flow: Complete Booking Request

```
1. Browser                POST /api/journeys/?mode=saga
                               (with JWT token, vehicle, route, times)
      ↓ nginx
2. Journey Service        Validates JWT, checks vehicle ownership
                          Creates Journey(status=PENDING) in DB
                          Writes OutboxEvent(unpublished) in same TX
      ↓ HTTP
3. Conflict Service       Serializable TX:
                          - Check driver time overlap (SELECT FOR UPDATE)
                          - Check vehicle time overlap (SELECT FOR UPDATE)
                          - Walk route grid cells, check capacity (SELECT FOR UPDATE)
                          - If all clear: INSERT booked_slot, increment capacity
                          Returns: {conflict: false}
      ↑ HTTP
4. Journey Service        Updates Journey(status=CONFIRMED)
                          Marks OutboxEvent(published=true)
      ↓ Background
5. Outbox Publisher       Publishes journey.confirmed to RabbitMQ
      ↓ RabbitMQ (fanout to 3 consumers)
6a. Notification Service  Formats message, pushes to WebSocket, stores in Redis
6b. Enforcement Service   SETEX active_journey:vehicle:{reg} in Redis (TTL-based)
6c. Analytics Service     Inserts event_log row, increments daily counter in Redis
      ↓ WebSocket
7. Browser                Shows green toast: "Journey booked! (CONFIRMED)"
                          Map updates with new booking marker
      ↓ Async goroutine (conflict-service)
8. Conflict Service       Replicates slot to all peer nodes:
                          POST /internal/slots/replicate → Peer Node B
                          (Peer B now has the slot in its DB for cross-node conflict checks)
```

---

## Port Reference

| Port | Service | Purpose |
|------|---------|---------|
| 3000 | Frontend | Browser UI |
| 8080 | Nginx Gateway | All browser API calls |
| 8001 | User Service | Direct (debug) |
| 8002 | Journey Service | Direct (debug) |
| 8003 | Conflict Service | Direct + cross-node replication |
| 8004 | Notification Service | Direct (debug) |
| 8005 | Enforcement Service | Direct (debug) |
| 8006 | Analytics Service | Direct (debug) |
| 5672 | RabbitMQ | AMQP (internal) |
| 15672 | RabbitMQ UI | `journey_admin` / `journey_pass` |
| 6379 | Redis | Internal |
| 5432 | PostgreSQL (users) | Internal |
| 5433 | PostgreSQL (journeys) | Internal |
| 5434 | PostgreSQL (analytics) | Internal |
| 5435 | PostgreSQL (conflicts) | Internal |
