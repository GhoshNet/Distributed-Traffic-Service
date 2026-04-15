# CS7NS6 Exercise 2 — Viva Preparation Dossier
### Globally-accessible Distributed Traffic Service (Group J)

> A deep, code-grounded walkthrough of the system as described in [FinalReportEx2.tex](FinalReportEx2.tex), for oral defence.
> All claims are grounded to actual files and line numbers. Gaps are called out explicitly.

---

## 0. One-Paragraph Mental Model

Six database-per-service microservices (User, Journey, Conflict, Notification, Enforcement, Analytics), fronted by an HAProxy→nginx gateway. The booking saga is driven synchronously by the Journey Service, which calls the Conflict Service over REST inside a circuit breaker and then writes `journey` + `outbox_event` rows **atomically in one Postgres transaction**. A background drainer publishes outbox rows to a RabbitMQ topic exchange (`journey_events`); Notification, Enforcement and Analytics consume asynchronously with Redis `SETNX` dedup. Strong consistency for double-booking is enforced by a single `SERIALIZABLE` transaction with `SELECT FOR UPDATE` inside the Go Conflict Service. The system runs in two modes: a **slim single-node stack** (12 containers) and a **full multi-node stack** where every laptop runs the whole system; nodes replicate user + slot state over `/internal/*` endpoints and coordinate via consistent-hash sharding, a Redlock-style 2-phase email lock, peer health monitoring, and a resilient `conflict_client.py` that fails over to peer conflict-services.

---

# TASK 1 — Services Explained

## System-level glue you need to speak to first

| Component | File | What it gives you to talk about |
|---|---|---|
| HAProxy (round-robin over 2 nginx) | [api-gateway/haproxy.cfg](api-gateway/haproxy.cfg) | Layer-7 LB with `httpchk GET /health` health checks; `option httpchk expect status 200`. |
| nginx API gateway | [api-gateway/nginx.conf](api-gateway/nginx.conf#L34-L40) | Three rate-limit zones (`auth 5r/s`, `booking 10r/s`, `general 30r/s`), per-request DNS re-resolution via `resolver 127.0.0.11 valid=5s` + `set $svc;`, `X-Request-ID` propagation. |
| RabbitMQ topic exchange | [shared/messaging.py:25](shared/messaging.py#L25) | `journey_events` topic exchange + `journey_events_dlx` fanout DLX; durable queues; 24h `x-message-ttl` on every service queue. |
| Shared Python modules | [shared/](shared/) | Circuit breaker, partition detection, health monitor, tracing, messaging — imported by all Python services. |
| Compose stack | [docker-compose.yml](docker-compose.yml) | 26 containers (full), 12 (slim) — per-service Postgres + replica, 3 RMQ nodes, Redis + replica + 3 Sentinels. |
| Peer discovery | `PEER_CONFLICT_URLS`, `PEER_USER_URLS` env vars + `POST /admin/peers/register` | Runtime-mutable peer list, gossip via `/internal/peers/register`. |

---

## 1.1 User Service (Python, port 8001)

### Purpose & Role
Stateless JWT auth, vehicle CRUD, entry point for drivers and enforcement agents. Owns identity data. Publishes `user.registered`. In the booking lifecycle, it is the **vehicle-ownership oracle** — [journey-service/app/service.py:290-315](journey-service/app/service.py#L290-L315) calls `GET /api/users/vehicles/verify/{reg}` before running the conflict check.

### API Surface
Public ([FinalReportEx2.tex tab:api](docs/FinalReportEx2.tex)):
- `POST /api/users/register`, `/register/agent`, `/login` → JWT
- `GET /api/users/me`, `/license/{n}`, `/vehicles`, `/vehicles/verify/{reg}`
- `POST/DELETE /api/users/vehicles[/…]`

Internal (for cross-node replication, never exposed on the public path):
- `POST /internal/users/lock` — peer grants/denies email lock (2-phase Redlock)
- `POST /internal/users/unlock`
- `POST /internal/users/replicate`, `/internal/vehicles/replicate`
- `GET /internal/users/all` — full snapshot for catch-up sync
- `POST /internal/users/peers/register` — gossip endpoint

### Data Ownership
`users_db` Postgres primary + streaming replica. Tables: `users` (id, email unique, password_hash bcrypt, license_number unique, role), `vehicles` (id, user_id FK, registration unique, vehicle_type). Why Postgres: strong uniqueness constraints on `email` and `license_number`, ACID transaction for the register flow ([user-service/app/service.py:42-76](user-service/app/service.py#L42-L76) uses `async with db.begin():` wrapping check-exists + insert + flush + refresh). **Read-write split**: separate session pool for primary (writes) and replica (reads), injected via FastAPI dependency.

### Dependencies
- Postgres primary/replica (sync, asyncpg)
- Redis DB 3 (distributed-lock keyspace, different from app caches)
- RabbitMQ (publishes `user.registered`)
- Every peer User Service (sync REST for lock + replication)

### Request Lifecycle: REGISTER
1. `POST /api/users/register` arrives at nginx (`zone=auth 5r/s`).
2. Route handler calls `acquire_distributed_lock(email)` → [user-service/app/replication.py:150-213](user-service/app/replication.py#L150-L213):
   - Local Redis `SETNX user_email_lock:{email} TTL=15s`
   - `POST /internal/users/lock` to every peer; if any rejects → rollback all acquired locks → HTTP 409
3. Local transaction begins ([service.py:42](user-service/app/service.py#L42)): check email/license uniqueness, insert, `flush`, `refresh`, commit.
4. Publish `user.registered` to RabbitMQ.
5. Fire-and-forget `replicate_user()` to all peers via `POST /internal/users/replicate` ([replication.py:236-256](user-service/app/replication.py#L236)).
6. `release_distributed_lock(email)` on local + peers.
7. Return `UserResponse`.

**Failure points:** peer unreachable during lock phase → skipped (availability bias, reconciled by periodic sync). If Postgres write fails after lock acquired, lock TTL (15s) reclaims it; no ghost row because the transaction rolled back.

### Configuration & Deployment
[docker-compose.yml:466-496](docker-compose.yml#L466-L496): one replica. Env: `DATABASE_URL`, `DATABASE_READ_URL`, `REDIS_SENTINEL_ADDRS`, `JWT_SECRET` (**shared across nodes** — that's why a JWT from laptop A is accepted on laptop B; documented in [§ Individual Service Designs → User](docs/FinalReportEx2.tex#L534)), `PEER_USER_URLS`, `MY_USER_URL`. Healthcheck: python urlopen `/health`. Full-node kill: `_node_failed=True` in [user-service/app/main.py] middleware returns 503 for every route except `/health`, `/admin/simulate/*`.

---

## 1.2 Journey Service (Python, port 8002)

### Purpose & Role
The saga orchestrator and the most architecturally complex service in the system ([report §4.3.2](docs/FinalReportEx2.tex#L538)). Owns the booking lifecycle, idempotency, outbox, circuit breaker to conflict-service, peer health monitoring, 2PC coordinator, lifecycle scheduler, and the simulate-failure cascade.

### API Surface
- `POST /api/journeys/` — book a journey (saga or `?mode=2pc`)
- `GET /api/journeys/`, `/all`, `/{id}`, `/vehicle/{reg}/active`, `/user/{id}/active`
- `DELETE /api/journeys/{id}` — cancel
- `GET /api/journeys/points/{balance,history}`, `POST /api/journeys/points/spend`
- `GET /health`, `/health/partitions`, `/health/nodes`
- `POST /admin/simulate/fail|recover`, `/admin/peers/register`, `/admin/recovery/drain-outbox`, `/admin/recovery/rebuild-enforcement-cache`, `/admin/2pc/demo`
- `GET /admin/logs`

### Data Ownership
`journeys_db` Postgres primary + replica. Tables:
- `journeys` (id, user_id, origin/destination + lat/lng, departure_time, estimated_arrival_time, vehicle_registration, status ∈ {PENDING, CONFIRMED, REJECTED, IN_PROGRESS, COMPLETED, CANCELLED}, rejection_reason, route_id, idempotency_key)
- `outbox_events` (id, routing_key, payload JSON, published bool, created_at) — **the transactional outbox**
- `idempotency_records` (key, journey_id) — dedupes client retries
- `driver_points` (user_id, balance, total_earned, total_spent, version) + `points_transactions` ledger ([points.py](journey-service/app/points.py))

Why Postgres: the atomicity of "write journey row + write outbox row in one transaction" is the central pattern the outbox depends on ([service.py:114-118](journey-service/app/service.py#L114-L118)). No Redis for primary state.

### Dependencies
- Conflict Service (sync REST, via `conflict_client.py` — local first then each `PEER_CONFLICT_URLS`)
- User Service (sync REST, vehicle ownership verify)
- Postgres primary/replica
- RabbitMQ (publish via outbox; NO direct consume in the booking path)
- Every peer Journey Service (sync REST for `/internal/users/...`, journey replication)

### Request Lifecycle: CREATE JOURNEY (saga)
Exactly as in [report §5.1](docs/FinalReportEx2.tex#L602) and [app/service.py:35-138](journey-service/app/service.py#L35-L138):

1. Client POSTs with `Idempotency-Key` header → nginx (`zone=booking 10r/s`) → journey-service.
2. `create_journey()` checks `idempotency_records`; if hit, returns cached `journey_id` → **exactly-once from client's POV**.
3. Verifies vehicle ownership by calling User Service (`httpx.AsyncClient`, 10s timeout).
4. Inserts `journey` row as `PENDING`, commits.
5. Persists idempotency record (separate commit).
6. Calls `BookingSaga.execute()` → `_check_conflicts()` → `resilient_conflict_check()` in [conflict_client.py:50-99](journey-service/app/conflict_client.py#L50-L99):
   - Tries `CONFLICT_SERVICE_URL` first, then each `PEER_CONFLICT_URLS`
   - Each URL has its own named circuit breaker `conflict-service:{url}` with `failure_threshold=3`, `reset_timeout=30s` (via [shared/circuit_breaker.py](shared/circuit_breaker.py))
   - 5xx or `TimeoutException`/`ConnectError` → next peer. 4xx passes through unchanged.
7. Conflict service runs a `SERIALIZABLE` tx with `SELECT FOR UPDATE` on three checks (driver overlap, vehicle overlap, road capacity per grid cell along path). If any check fails, the Go service returns `is_conflict=true` and the slot is **not** inserted.
8. Back in Journey Service: updates journey status to CONFIRMED/REJECTED **and writes the outbox event in the SAME `db.commit()`** ([saga.py:122-146](journey-service/app/saga.py#L122-L146) `save_outbox_event`).
9. Awards points (`PointsService.earn_points` with `SELECT FOR UPDATE` lock).
10. Fires an asyncio task to replicate the journey to peers.
11. Returns `JourneyResponse` to client.
12. Background [outbox_publisher.py](journey-service/app/outbox_publisher.py#L28-L70) wakes every 2s, drains `WHERE published=False LIMIT 50`, publishes to RabbitMQ, sets `published=True`, commits. If the broker is down, the outer loop catches and retries.

**Failure points:** ConflictService down → `resilient_conflict_check` exhausts all peers → `None` → saga rejects "Conflict check service temporarily unavailable". Postgres write failure before commit → journey stays PENDING, outbox row never written. Crash between conflict OK and outbox write → no outbox row published, but capacity is already reserved in conflict-service → **capacity leak** (this is the bug 2PC was added to mitigate — [coordinator.py:144-152](journey-service/app/coordinator.py#L144-L152) adds a compensating `CANCEL` on `except`).

### Request Lifecycle: CANCEL
[service.py:190-239](journey-service/app/service.py#L190-L239):
1. Ownership check; must be CONFIRMED or PENDING.
2. Update status to CANCELLED + write `journey.cancelled` outbox event in same tx.
3. **Direct synchronous** `resilient_conflict_cancel(journey_id)` ([service.py:215-220](journey-service/app/service.py#L215)) — bypasses the 2–4s outbox drain delay; comment explains why: "without this direct call a re-booking attempt during that window is incorrectly rejected as a time overlap."
4. Deduct cancellation points.
5. Fire-and-forget peer replication.
6. Downstream consumers react asynchronously: conflict releases capacity (idempotent — already `is_active=false` just acks silently), notification pushes WS, enforcement deletes cache, analytics increments counter.

### Configuration & Deployment
Single replica per node. Env: `DATABASE_URL`, `DATABASE_READ_URL`, `CONFLICT_SERVICE_URL`, `PEER_CONFLICT_URLS`, `RABBITMQ_URL`, `JWT_SECRET`, `REDIS_SENTINEL_ADDRS`. Background tasks started in [main.py lifespan:44-106](journey-service/app/main.py#L44-L106): `transition_journeys`, `run_outbox_publisher`, `PartitionManager.start`, `PeerHealthMonitor.start`, peer catch-up sync + `start_periodic_sync(300, _async_session)` (every 5 min).

---

## 1.3 Conflict Service (Go, port 8003)

### Purpose & Role
Atomic "check-and-reserve" of road-segment capacity and the only strong-consistency boundary in the whole system. The grid-cell model + `SERIALIZABLE + SELECT FOR UPDATE` is the linchpin of the "no double booking" guarantee.

### API Surface
- `POST /api/conflicts/check` — the PREPARE/reserve call from Journey Service
- `POST /api/conflicts/cancel/{journey_id}` — compensating cancel (used both by 2PC and the direct sync cancel in Journey Service)
- `GET /api/routes`, `/api/conflicts/routes` — predefined routes with waypoints
- `POST /internal/slots/replicate`, `/internal/slots/cancel`, `GET /internal/slots/active` — cross-node eventual-consistency replication
- `POST /internal/peers/register` — runtime peer registration + gossip
- `GET /health`

### Data Ownership
`conflicts_db` Postgres primary + replica. Tables:
- `booked_slots` (id, journey_id unique, user_id, vehicle_registration, departure_time, arrival_time, origin/dest lat/lng, route_id, is_active)
- `road_segment_capacity` (id, grid_lat, grid_lng, time_slot_start, time_slot_end, current_bookings, max_capacity) — **unique index on (grid_lat, grid_lng, time_slot_start)**
- `routes` + `route_waypoints` — predefined road paths (e.g. Dublin→Galway via Athlone)

Why Postgres and not a key-value store: the grid-cell increment is an RMW operation that needs `SERIALIZABLE` + row-level locks; Redis INCR would not give you the "fail the whole booking if any cell along the path is full" atomicity.

### Dependencies
- Postgres (primary writes, replica reads)
- RabbitMQ (consumes `journey.cancelled` via `conflict_cancellation_events` queue → [consumer.go](conflict-service/consumer.go#L14-L20))
- Every peer Conflict Service (sync REST, fire-and-forget replication)

### The Check-and-Reserve Algorithm ([service.go:60-143](conflict-service/service.go#L60-L143))
```go
tx, _ := db.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.Serializable})
// Check 1: driver overlap — SELECT ... FOR UPDATE
// Check 2: vehicle overlap — SELECT ... FOR UPDATE
// Check 3: for each grid cell on path — SELECT id FROM road_segment_capacity
//          WHERE current_bookings >= max_capacity LIMIT 1 FOR UPDATE
//     (returns a row ⇒ cell is full ⇒ REJECT)
// If all three pass:
//    INSERT INTO booked_slots ...
//    For each cell: INSERT ... ON CONFLICT (grid,slot) DO UPDATE current_bookings+1
// tx.Commit
// go replicateSlotToPeers(...)  // fire-and-forget
```

Cells are generated by walking the straight-line path (or real-road waypoints if `route_id` is provided) in steps of `gridResolution = 0.01°` ≈ 1 km ([service.go:214-242](conflict-service/service.go#L214-L242)). Time slot is 30 min ([service.go:30-35](conflict-service/service.go#L30-L35)). `defaultMaxCapacity=1` — single-lane road.

**Why SERIALIZABLE and not SELECT FOR UPDATE alone?** The `SELECT FOR UPDATE` at `current_bookings >= max_capacity` returns no rows if the cell is under capacity, so there's nothing to lock. SERIALIZABLE + the retry on deadlock is what actually prevents two transactions both reading "4/5 booked" and both committing "5/5". Postgres `SERIALIZABLE` uses SSI (Serializable Snapshot Isolation) — it detects the read-write dependency cycle and aborts one tx with `40001 serialization_failure`. **Note:** the report doesn't explicitly discuss the retry loop — this is a gap you should flag honestly.

### Cross-Node Slot Replication ([replication.go](conflict-service/replication.go#L161-L202))
Three mechanisms:
1. **Forward push:** after commit, `go replicateSlotToPeers()` POSTs `/internal/slots/replicate` to each peer.
2. **Catch-up sync:** on boot + when a new peer registers, `syncFromPeer(peerURL)` GETs `/internal/slots/active` and applies missing rows via `applyReplicatedSlot` (idempotent — checks `EXISTS WHERE journey_id = $1` first).
3. **Periodic re-sync:** `startPeriodicSync(5 * time.Minute)` — safety net.

This is **eventually consistent**. Two bookings submitted to Node A and Node B within the replication window can both pass. The report flags this openly as "millisecond-window double-booking possible" ([§testing known limitation](docs/FinalReportEx2.tex#L840)).

### Configuration & Deployment
Single replica per node. Env: `DATABASE_URL`, `RABBITMQ_URL`, `MY_CONFLICT_URL`, `PEER_CONFLICT_URLS`. Port 8000 inside the container, 8003 on host.

---

## 1.4 Notification Service (Go, port 8004)

### Purpose & Role
Real-time push to drivers via WebSocket, plus a per-user notification history. No HTTP booking path — purely event-driven.

### API Surface
- `GET /api/notifications/?token=…&limit=…` — REST history (last 50 per user, 7-day TTL)
- `WS /ws/notifications/?token=…` — live push channel
- `GET /health`

### Data Ownership
Redis only (DB 3 in slim, via Sentinel otherwise). Per-user list at key `notifications:{user_id}` implemented via pipelined `LPUSH + LTRIM 0 49 + EXPIRE 7d`. No Postgres.

**In-process state:** `wsConns map[string][]*websocket.Conn` guarded by `sync.RWMutex` ([consumer.go:62-65](notification-service/consumer.go#L62-L65)). **This is the single biggest known gap** — service restart drops all WebSocket connections ([report §3.4 Known gaps](docs/FinalReportEx2.tex#L447)).

### Dependencies
- Redis (history, dedup)
- RabbitMQ (consumer only — does not publish)
- No synchronous deps on any other microservice

### Request Lifecycle: `journey.confirmed` event
[consumer.go:169-211](notification-service/consumer.go#L169-L211):
1. AMQP delivery arrives.
2. `notifIsDuplicate(msg)` — `EXISTS notif:processed:{MessageId or SHA256(body)}` ([consumer.go:138-157](notification-service/consumer.go#L138-L157)). Hit → `msg.Ack(false)` and drop.
3. `handleEvent()` renders the template (`{user_name} has confirmed…`), calls `storeNotification(userID, notification)` which does the pipelined Redis list write, then `pushToWS(userID, notification)` fans out to every live connection for that user ([consumer.go:91-129](notification-service/consumer.go#L91-L129)).
4. Dead connections are cleaned up lazily — write error → appended to `dead` slice → removed from registry under lock.
5. `notifMarkProcessed(msg)` sets the dedup key with 24h TTL.
6. `msg.Ack(false)`.

**Failure points:** process restart → `wsConns` empty → all pushes silently drop until clients reconnect. Broker disconnect handled by `NotifyClose` + reconnect loop ([consumer.go:237-250](notification-service/consumer.go#L237)). DLX routes poison messages to `dead_letter_queue` via `msg.Nack(false, false)`.

### Configuration & Deployment
Env: `REDIS_URL` DB 3, `REDIS_SENTINEL_ADDRS`, `RABBITMQ_URL`, `JWT_SECRET`. Single replica.

---

## 1.5 Enforcement Service (Python, port 8005)

### Purpose & Role
Sub-second roadside verification. Agent queries by vehicle plate or licence number; service must answer within 200 ms hard limit. The only service with a true **read-through cache** pattern.

### API Surface
- `GET /api/enforcement/verify/vehicle/{reg}` — agent-only (JWT role check)
- `GET /api/enforcement/verify/license/{lic_no}` — agent-only
- `GET /health`

Responses include `X-Data-Staleness: STALE` and `X-Partition-Status: journey-service: PARTITIONED` headers when the Journey Service is unreachable.

### Data Ownership
**None authoritatively.** Owns a Redis (DB 4) cache:
- `active_journey:vehicle:{reg}` → JSON of journey metadata; TTL = `(estimated_arrival - now) + 3600s`
- `active_journey:user:{user_id}` → same
- `license_user_id:{license_number}` → user_id string; fixed 24h TTL

Cache populated by the RabbitMQ consumer ([consumer.py:41-91](enforcement-service/app/consumer.py#L41-L91)) on `journey.confirmed`/`journey.started`, and deleted on `journey.cancelled`/`journey.completed`.

### Dependencies
- Redis (primary lookup)
- Journey Service (fallback REST: `GET /api/journeys/vehicle/{reg}/active`)
- User Service (licence→user_id lookup on cache miss)
- RabbitMQ consumer (event-driven cache maintenance)

### Request Lifecycle: verify by plate ([service.py:51-104](enforcement-service/app/service.py#L51-L104))
1. `_check_cache("active_journey:vehicle:{plate}")` → Redis `GET`, JSON decode.
2. If hit and journey window is current (`departure <= now+30min AND arrival >= now`): return `is_valid=true`. Measured <20 ms p95 ([report §2.2 NFR table](docs/FinalReportEx2.tex#L225)).
3. If miss → `_query_journey_service(plate)` → `GET {JOURNEY_SERVICE_URL}/api/journeys/vehicle/{reg}/active`.
4. If hit there: return valid. (Note: [service.py](enforcement-service/app/service.py#L85-L99) does **not** repopulate the cache on this fallback path — the docstring claims it does but the code doesn't. **Gap to be honest about in viva.**)
5. Both miss → `is_valid=false`.

**Failure points:** Journey Service down → no cache repopulation, so every subsequent cache miss sees the same fallback failure. Redis down → service essentially crippled; the partition manager marks `redis: PARTITIONED` and every request falls through to Journey Service. **Cold-start gap:** after restart the cache is empty; there is no boot-time cache warming. Admin endpoint `POST /admin/recovery/rebuild-enforcement-cache` on Journey Service rebuilds it manually ([main.py:302-318](journey-service/app/main.py#L302-L318)).

### Configuration & Deployment
Env: `REDIS_URL` DB 4, `JOURNEY_SERVICE_URL`, `REDIS_SENTINEL_ADDRS`. Single replica. No Postgres.

---

## 1.6 Analytics Service (Go, port 8006)

### Purpose & Role
Immutable audit trail + aggregated counters + system-wide monitoring hub.

### API Surface
- `GET /api/analytics/stats` — mixed real-time + all-time
- `GET /api/analytics/events?event_type=…&limit=…&offset=…` — event history
- `GET /api/analytics/hourly?limit=…` — `hourly_stats` roll-ups
- `GET /api/analytics/replica-lag` — live view over `pg_stat_replication`
- `GET /api/analytics/health/services` — parallel probe of all six `/health` endpoints
- `GET /health`

### Data Ownership
`analytics_db` Postgres primary + replica. Tables:
- `event_logs` (id, event_type, journey_id, user_id, origin, destination, metadata JSON, created_at) — immutable
- `hourly_stats` (hour, total_bookings, confirmed, rejected, cancelled)

Plus Redis (DB 5) for real-time counters: `analytics:daily:{YYYY-MM-DD}` hash with `total_events`, `journey.confirmed`, `journey.rejected`, `journey.cancelled`, 48h TTL. See [consumer.go:229-242](analytics-service/consumer.go#L229-L242) — **this is a dual-write**, explicitly flagged as "best-effort": if Redis pipeline fails, Postgres still has the event.

### Dependencies
- Postgres (primary write, replica read)
- Redis (counters, dedup)
- RabbitMQ consumer (binds to `journey.*` and `user.*`)
- HTTP probes to all six services (for the aggregated `/api/analytics/health/services` endpoint)

### Request Lifecycle: event ingestion ([consumer.go:198-243](analytics-service/consumer.go#L198-L243))
1. Message arrives from RMQ.
2. `isDuplicate(msg)` via `analytics:processed:{MessageId}` key.
3. `insertEvent()` inserts a row into `event_logs` (full JSON metadata).
4. Redis pipeline: `HIncrBy total_events`, `HIncrBy {routing_key}`, `Expire 48h`. Best-effort — a Redis failure logs a warning but the function returns nil.
5. `markProcessed(msg)` sets dedup key 24h.
6. `msg.Ack(false)`.

An hourly rollup goroutine ([main.go](analytics-service/main.go)) fires every 60 min and aggregates the previous hour into `hourly_stats`. First run backfills the prior hour so restart doesn't leave a data gap.

### Configuration & Deployment
Env: `DATABASE_URL`, `DATABASE_READ_URL`, `REDIS_URL/SENTINEL_ADDRS`, `RABBITMQ_URL`, `AUDIT_HMAC_SECRET` (**set but unused** — the planned HMAC chain was never completed; [report §3.5 Limitations](docs/FinalReportEx2.tex#L446)), `SERVICES_BASE_URL: docker`.

---

# TASK 2 — Distributed Systems Concepts

> PRESENT / ABSENT / PARTIAL — with file pointer, why, limits.

## Replication & Consistency

### 1. Replication — **PRESENT (multiple kinds)**
- **Postgres WAL streaming** (CP-leaning): per-service primary + replica, `wal_level=replica`, `max_wal_senders=3`, `hot_standby=on` ([docker-compose.yml:249-254](docker-compose.yml#L249)). Replica is init'd by `pg_basebackup -R`. Primary handles writes; replica handles reads through the `DATABASE_READ_URL` session pool.
- **Redis replica + Sentinel** ([docker-compose.yml:147-234](docker-compose.yml#L147)): 1 primary + 1 replica + 3 Sentinels, quorum 2, `down-after-milliseconds 5000`, `failover-timeout 10000`. Services use Sentinel clients (e.g. [enforcement-service/app/service.py:33-41](enforcement-service/app/service.py#L33-L41)).
- **Application-level active-active replication for User + Conflict services** (AP-leaning): fire-and-forget HTTP push to peers plus periodic catch-up sync ([user-service/app/replication.py](user-service/app/replication.py#L236), [conflict-service/replication.go](conflict-service/replication.go#L161)).
- **RabbitMQ 3-node cluster** configured but marked "partial" — single-host Erlang distribution unreliable.

**Why it matters:** availability under node kill, read scaling, cross-laptop failover.
**Limitations:** active-active Conflict replication is eventually consistent → millisecond-window double-booking possible (explicitly acknowledged).

### 2. Consistency Model — **MIXED**
- **Strong (linearizable per slot):** Conflict Service `SERIALIZABLE + SELECT FOR UPDATE` on a single node ([service.go:67](conflict-service/service.go#L67)).
- **Eventual:** everything involving RabbitMQ fan-out (notification, enforcement cache, analytics counters) and cross-node slot replication.
- **Read-your-writes within a single service:** not explicit — the Journey Service reads from the replica and writes to the primary, so right after a write a follow-up read could miss it if replica lag > round-trip. The report does not call this out. **Gap.**
- **JWT is stateless → monotonic sessions across nodes** via the shared `JWT_SECRET`.

### 3. Update Strategy — **PRESENT**
- **Primary-backup** for Postgres and Redis (sync via WAL / async PSYNC).
- **Multi-primary (active-active) with last-writer-wins** for conflict slots and user records across laptops — no vector clocks, no CRDT, idempotent apply via `journey_id` / `user_id` primary key dedup.

---

## Transactions & Concurrency

### 4. Transactions — **PRESENT, LOCAL ONLY**
- Conflict check+reserve in one `SERIALIZABLE` tx.
- Journey status + outbox in one transaction (the transactional-outbox pattern).
- User register inside `async with db.begin():` block.
- Points earn/spend in one tx with `SELECT FOR UPDATE` pessimistic lock ([points.py:80-128](journey-service/app/points.py#L80-L128)).
- **No XA / distributed two-phase commit at the resource-manager level.** The "2PC" in [coordinator.py](journey-service/app/coordinator.py) is TCC (Try-Confirm-Cancel) at the service level — a compensating cancel call, not an XA protocol.

### 5. Isolation Level — **PRESENT**
- `SERIALIZABLE` on the conflict check tx (Postgres uses Serializable Snapshot Isolation — SSI, [service.go:67](conflict-service/service.go#L67)).
- Default `READ COMMITTED` elsewhere, with application-level row locks via `SELECT FOR UPDATE` (journey points).

### 6. Concurrent Request Handling — **PRESENT**
- Python services are `async`/`await` on FastAPI + uvicorn → single-process event loop handles many concurrent requests.
- Go services use native goroutines + `sync.RWMutex` where shared maps exist (`wsConns`, peer registry).
- Contention on hot slots is resolved by SSI aborting one transaction → caller sees a 500 and **would need to retry**. The report doesn't describe a retry loop — **gap: the Journey Service does not retry a 500 from conflict-service**, so an SSI abort leaks as a user-visible rejection.

### 7. Race-Condition Prevention — **PRESENT (well-covered)**
- **Double-booking:** SERIALIZABLE + SELECT FOR UPDATE on every cell along the path.
- **Double email registration on one node:** Postgres `UNIQUE` constraint.
- **Double email registration across nodes:** Redlock-style 2-phase lock.
- **Double points award for duplicate event delivery:** idempotency check on `journey_id + reason` before the balance update ([points.py:103-114](journey-service/app/points.py#L103)).
- **Double notification send:** Redis `SETNX` on message ID.
- **Known gap:** the **cross-node slot replication window** means Node A and Node B can both confirm the same slot inside ~50–200 ms if submitted simultaneously to different laptops.

---

## Data Management

### 8. Sharding / Partitioning — **PRESENT (write-authority only)**
Consistent hash `MD5(key) % num_nodes`:
- User Service: `shard_for_email` ([replication.py:104-125](user-service/app/replication.py#L104-L125))
- Conflict Service: `shard = MD5(route_id) % num_nodes` ([sharding.go](conflict-service/sharding.go))

Every node still stores every row — sharding decides "home shard" write-authority and is logged for the activity feed. **This is lighter-weight than actual partitioning**: it doesn't localise data, just blame. Effectively a gossip-level "who-should-have-done-it" marker.

### 9. Locality Exploitation — **PRESENT**
- **Spatial locality:** the grid-cell model bounds the lock scope of each booking to the cells its route traverses. Two bookings on non-overlapping road segments never contend for the same row ([service.go:194-291](conflict-service/service.go#L194)).
- **Temporal locality:** 30-minute time slots — bookings whose intervals don't overlap never contend even on the same cell.
- **Per-peer failover locality:** resilient conflict client prefers **local** first ([conflict_client.py:38-47](journey-service/app/conflict_client.py#L38-L47)).

### 10. Caching — **PRESENT**
- **Enforcement Redis read-through cache** ([enforcement-service/app/service.py:51-104](enforcement-service/app/service.py#L51-L104)): key `active_journey:vehicle:{plate}`. TTL = `(arrival - now) + 3600s`. Event-driven invalidation on `journey.cancelled/completed`. Redis eviction policy `allkeys-lru` in slim.
- **Licence→user-id cache**: fixed 24h TTL ([service.py:116-140](enforcement-service/app/service.py#L116)).
- **Analytics Redis daily counters**: 48h TTL, hash.
- **Idempotency record cache** in Postgres (not Redis) — persistent across restarts.
- **No client-side HTTP caching.**

### 11. Data Durability — **PRESENT**
- Postgres `fsync=on` default + WAL replica.
- RabbitMQ `durable=True` exchange, `DeliveryMode.PERSISTENT` on publish ([shared/messaging.py:97-103](shared/messaging.py#L97)).
- Redis AOF `appendonly yes` ([docker-compose.yml:135](docker-compose.yml#L135)).
- **Outbox durability**: writing the outbox row in the same tx as the journey row means the event survives any subsequent crash. The drainer replays after restart.

---

## Fault Tolerance & Recovery

### 12. Communication-Failure Tolerance — **PRESENT**
- **Circuit breaker** ([shared/circuit_breaker.py](shared/circuit_breaker.py)) with CLOSED/OPEN/HALF_OPEN + per-dependency threshold (3 failures) and 30s reset. Used per-URL in the resilient conflict client.
- **Service-level failover:** `resilient_conflict_check` falls through all known URLs.
- **Timeouts:** explicit `httpx.AsyncClient(timeout=30)` on the conflict call, 10s on user verify, 3s on peer pings.
- **Retry on RabbitMQ connect:** 10 attempts × 3s backoff.
- **Auto-reconnect on AMQP disconnect:** `NotifyClose` channel reopens ([consumer.go:237-250](notification-service/consumer.go#L237)).
- **Missing:** no retry on `500` from conflict-service on the saga path (one-shot reject), no exponential backoff inside the circuit breaker.

### 13. Failure Detection — **PRESENT**
- **Dependency probes:** `PartitionManager` every 5s ([shared/partition.py:141-183](shared/partition.py#L141)).
- **Peer liveness:** `PeerHealthMonitor` every 10s, ALIVE → SUSPECT (3 misses) → DEAD (6 misses) ([shared/health_monitor.py:63-202](shared/health_monitor.py#L63)).
- **Container-level:** docker-compose healthchecks on every service (`curl /health` / `wget --spider`).
- **Graceful degradation:** `<50% peers alive → LOCAL_ONLY mode` ([health_monitor.py:186-202](shared/health_monitor.py#L186)).

**Limitations:** fixed thresholds, not φ-accrual. Fixed 10s heartbeat means up to 60s to mark DEAD — not suitable for production outages.

### 14. Recovery — **PRESENT**
- **Outbox drainer** on restart: any `published=false` row drains first ([outbox_publisher.py](journey-service/app/outbox_publisher.py#L28)).
- **Catch-up sync** on boot for User and Conflict services ([replication.py sync_from_peer](user-service/app/replication.py#L277)).
- **Periodic re-sync every 5 min** as safety net.
- **Admin force endpoints**: `POST /admin/recovery/drain-outbox`, `POST /admin/recovery/rebuild-enforcement-cache` for manual recovery.

### 15. Total Failure — **PARTIAL**
- If all journey-service instances die, no new bookings are accepted (no way around the saga). Clients see network errors.
- When restored, Postgres has the full state (WAL replica preserved). Outbox drains on boot. Catch-up sync pulls missed slots from peers.
- **The Notification Service has no recovery** for live WebSockets — history is intact in Redis but live pushes during outage are lost until reconnect.

### 16. Consistency Across Failures — **PARTIAL**
- Durable state is safe (Postgres, durable RMQ, Redis AOF).
- Cross-node slot replication is eventually consistent, so a crashing node that never pushed some writes will let its replacement serve stale data until the periodic re-sync lands.

---

## Network Partitions

### 17. Partition Behaviour — **PRESENT**
- Dependency-level: `PartitionManager` transitions CONNECTED → SUSPECTED → PARTITIONED → MERGING and adds `X-Partition-Status` header on every response.
- Enforcement Service continues from cache with `X-Data-Staleness: STALE` (AP choice).
- Journey Service circuit-breaks and rejects the booking (CP choice — fail-fast rather than accept without conflict check).
- Cross-laptop partition: client-side `resilientFetch` on the frontend retries each peer from its localStorage list.

### 18. CAP Trade-off — **MIXED**
- **Conflict service (local):** CP. Strongly consistent within its Postgres node; if partitioned off, bookings to that node fail.
- **Conflict service (across nodes):** AP. Eventually consistent replication; prefers availability over strict uniqueness.
- **Enforcement:** AP. Serves stale cache during partition.
- **Notification / Analytics:** AP. Lossless eventual delivery via outbox + dedup.
- **User Service:** mixed — local Postgres is CP per node; cross-node active-active is AP. The Redlock-style lock is described as "availability-biased" — unreachable peers are skipped ([replication.py:138](user-service/app/replication.py#L138)), which is a deliberate AP choice for the demo.

This is probably the single most important answer to rehearse: **the report's architecture is not one-point-on-the-CAP-triangle — it is per-service.**

---

## Load Balancing & Scalability

### 19. Load Balancing — **PRESENT**
- **HAProxy** round-robin across 2 nginx instances ([haproxy.cfg:19](api-gateway/haproxy.cfg#L19)) with active health checks.
- **nginx** to upstream services via `set $svc; proxy_pass http://$svc;` with `resolver 127.0.0.11 valid=5s` (per-request DNS re-resolution — fix for the "stale upstream IP after container recreation" bug in testing).
- **Browser `resilientFetch`** is effectively client-side LB across peer nodes.

### 20. Horizontal Scalability — **PARTIAL**
- Services are stateless modulo their DB → can run multiple replicas, but compose file defines one replica each. No `deploy.replicas: N`.
- DB is the bottleneck: each service has one primary. Scale-out reads use the replica.
- The "full-stack per laptop" model is effectively horizontal scaling by replication, not by sharding workload.

---

## Other Concepts

### 21. Checkpointing / Message Logging — **PRESENT (as outbox)**
The `outbox_events` table is a durable message log. There is no formal "checkpoint + replay" for state, but it does log every outbound event for the crash-recovery model.

### 22. Idempotency — **PRESENT (multi-layer)**
- **Client-facing:** `Idempotency-Key` header → `idempotency_records` table returns cached journey.
- **Consumer-side:** Redis `SETNX notif:processed:{msgID}` (24h TTL) in notification, analytics; `SETNX analytics:processed:{msgID}` in analytics.
- **Slot replication:** `applyReplicatedSlot` checks `EXISTS WHERE journey_id` before insert.
- **Cancel:** `cancelBookingSlot` treats `ErrNotFound` (already inactive) as success.

This is the report's cleanest pattern — idempotency is applied at every async boundary.

### 23. Saga Pattern — **PRESENT (and its limits)**
[saga.py:47-77](journey-service/app/saga.py#L47) implements an orchestrated saga: one service drives, calls conflict-service, on failure sets REJECTED. **No compensation loop** — if the conflict reserve succeeded but the journey row commit fails, there is a leaked slot. This is acknowledged and is the reason the 2PC/TCC mode exists as an alternative.

### 24. Compensating Transactions / TCC — **PRESENT**
[coordinator.py TwoPhaseCoordinator](journey-service/app/coordinator.py#L62-L201) — TRY (conflict /check), CONFIRM (commit journey + outbox), CANCEL (POST /api/conflicts/cancel/{id}). Uses the same peer URL that executed PREPARE to avoid phantom-slot leaks. Activated via `POST /api/journeys/?mode=2pc`.

**Limit:** best-effort cancel. If all cancel URLs fail, the log says "capacity may leak, manual cleanup required" — no persistent compensation journal.

### 25. Leader Election — **ABSENT**
No Raft, no Paxos, no leader election. The "shard=0 is home" mapping is deterministic from the peer list, so there is no election step. Report notes this trade-off explicitly: "a lightweight threshold quorum, sufficient for a two-laptop demo without full Paxos/Raft complexity."

### 26. Consensus — **ABSENT (by design)**
No consensus protocol. Sentinel is the only quorum-based decision (2 of 3 agree before promoting Redis replica). The "50%+1 peers alive" rule in `PeerHealthMonitor` is a threshold, not consensus.

### 27. Back-Pressure / Rate Limiting — **PRESENT**
nginx `limit_req_zone` (token bucket) per client IP: auth 5 r/s, booking 10 r/s, general 30 r/s ([nginx.conf:34-36](api-gateway/nginx.conf#L34)). Plus RabbitMQ `ch.Qos(10, 0, false)` prefetch on every consumer.

### 28. Service Discovery — **PRESENT (simple)**
- **Static within Compose:** Docker DNS (`conflict-service:8000`).
- **Dynamic at runtime:** `POST /admin/peers/register` + gossip via `/internal/peers/register`. The peer list lives in process memory, is refreshed by every registration, and is fetched by the browser from `/health/nodes` at login time.

No Consul / etcd / ZooKeeper. Fine for the demo; not production.

### 29. Bulkhead Pattern — **PARTIAL**
Per-URL circuit breakers in the resilient conflict client are a bulkhead (a bad local conflict-service does not take down the peer endpoint's breaker). No thread-pool or async-semaphore bulkheads elsewhere.

### 30. Distributed Tracing — **PARTIAL**
`X-Request-ID` / `X-Correlation-ID` propagates via [shared/tracing.py](shared/tracing.py) + nginx `$request_id`. No Jaeger/Zipkin visualisation — report flags this as a gap.

### 31. Token Revocation — **ABSENT**
No JWT denylist on logout. Known gap ([report §3.5](docs/FinalReportEx2.tex#L449)).

### 32. Audit Integrity Chain — **ABSENT (planned, not completed)**
`AUDIT_HMAC_SECRET` env var is set but never used. `event_logs` records exist but no HMAC links.

---

# TASK 3 — 25 Probable Viva Questions

> Each with **what it tests**, and an **answer sketch** grounded in actual code.
> "Weak spot" means: if asked this, be ready to say "we didn't implement that — here's why."

## Tier 1 — "Explain Your Application" (8)

### Q1. Walk me through end-to-end what happens when a client POSTs to `/api/journeys/` with an idempotency key.
**Tests:** request lifecycle, saga orchestration, transactional outbox.
**Answer:** HAProxy round-robins to an nginx instance → nginx applies `booking 10 r/s` rate limit → `set $svc "journey-service:8000"; proxy_pass` → journey-service `create_journey()`. First the `idempotency_records` table is checked; a hit returns the cached journey. Otherwise vehicle ownership is verified via `GET /api/users/vehicles/verify/{reg}` to the user-service. A journey row is inserted as PENDING. Then `BookingSaga.execute()` runs: it calls `resilient_conflict_check()`, which walks through `CONFLICT_SERVICE_URL` + each `PEER_CONFLICT_URLS` entry, each wrapped in its own named circuit breaker. The conflict-service runs one `SERIALIZABLE` transaction with `SELECT FOR UPDATE` doing (1) driver overlap, (2) vehicle overlap, (3) road-capacity per grid cell, and on success inserts the booked slot + increments per-cell counters + commits. Back in journey-service, the status update and the `outbox_events` row are committed in a **single Postgres transaction** — this is the transactional outbox and is how we avoid the dual-write problem. Points are awarded, a peer-replication task is fired, and the response returns. Meanwhile the outbox publisher polls every 2s, drains `published=false` rows, publishes to RabbitMQ `journey_events`, and downstream services (notification, enforcement, analytics) consume with Redis SETNX dedup.

### Q2. Which service owns which data, and why Postgres vs Redis for each?
**Tests:** database-per-service, storage choice rationale.
**Answer:** User, Journey, Conflict, Analytics → Postgres; Notification and Enforcement → Redis only. Postgres is chosen where we need **ACID, unique constraints, or pessimistic locks** — user uniqueness, journey+outbox atomicity, conflict SSI isolation, analytics audit trail. Redis is chosen where the use case is **TTL-bounded, read-heavy, sub-ms**: notification history (LPUSH/LTRIM capped list with 7-day TTL), enforcement active-journey cache (TTL tied to journey window + 1h buffer). Analytics keeps **both**: Postgres `event_logs` is the immutable audit trail; Redis `analytics:daily:*` is the real-time counter. That dual-write is explicitly best-effort — if Redis fails we log and move on, Postgres remains authoritative.

### Q3. How does your system guarantee no two drivers can book the same road slot at the same time?
**Tests:** concurrency, strong consistency boundary.
**Answer:** Inside a single Postgres node, by a `SERIALIZABLE` transaction wrapping three `SELECT ... FOR UPDATE` checks and the cell-counter upsert ([conflict-service/service.go:60-143](conflict-service/service.go#L60)). The road is modelled as a grid of 0.01° (~1 km) cells × 30-min time slots, with `defaultMaxCapacity = 1`. For each cell the route traverses, we try to lock a "full" row — if no such row, we insert/increment. Two concurrent bookings either (a) the second deadlocks on `FOR UPDATE` and the transaction retries, or (b) Postgres SSI detects the read-write dependency cycle and aborts one with `40001 serialization_failure`. Across nodes, however, we are only **eventually consistent** — replication is fire-and-forget with a 5-minute re-sync safety net, so two bookings submitted to different laptops within the replication window (~50–200 ms on LAN) can both pass. We document this openly in the Known Trade-offs section.

### Q4. Explain the transactional outbox and why you need it.
**Tests:** dual-write problem, at-least-once semantics.
**Answer:** The classical dual-write problem: if a service writes to its DB and then publishes to a broker, a crash between the two loses the event. Without the outbox, a confirmed journey could end up with no `journey.confirmed` event fired, so notifications never reach the driver and analytics/enforcement fall out of sync. We solve this by writing the event into an `outbox_events` table **inside the same transaction** as the journey status update ([journey-service/app/service.py:114-118](journey-service/app/service.py#L114-L118), [saga.py:122-146](journey-service/app/saga.py#L122)). A background publisher ([outbox_publisher.py](journey-service/app/outbox_publisher.py)) polls `published=false` rows every 2 s and publishes to RabbitMQ; on broker outage rows accumulate and drain on reconnect. This gives at-least-once, and consumers are idempotent via Redis `SETNX` on the message ID.

### Q5. What does your circuit breaker actually do and when does it fire?
**Tests:** fault-tolerance pattern knowledge.
**Answer:** [shared/circuit_breaker.py](shared/circuit_breaker.py) is a named, async-safe breaker with CLOSED/OPEN/HALF_OPEN states. Each journey→conflict call site registers one breaker per URL via `get_circuit_breaker("conflict-service:{url}", failure_threshold=3, reset_timeout=30)`. Every consecutive failure increments a counter; on the third, the breaker flips to OPEN and subsequent calls raise `CircuitBreakerOpenError` immediately without hitting the network. After 30 s, the next call is allowed through as a HALF_OPEN probe — success → CLOSED, failure → straight back to OPEN. Because the resilient client iterates all known URLs each request, an open breaker on the local conflict-service is transparently skipped in favour of the next peer. This is bulkhead-style — one bad dependency cannot take down the booking path as long as any peer is healthy.

### Q6. What's inside `shared/` and why did you put it there?
**Tests:** cross-cutting concerns, code reuse.
**Answer:** All Python services import from `shared/`: `circuit_breaker.py`, `partition.py` (dependency probes every 5 s, CONNECTED→SUSPECTED→PARTITIONED→MERGING), `messaging.py` (RabbitMQ wrapper with persistent delivery mode, DLX wiring, reconnect), `tracing.py` (`X-Request-ID`/`X-Correlation-ID` propagation via a FastAPI middleware), `health_monitor.py` (peer ALIVE/SUSPECT/DEAD with LOCAL_ONLY degradation), `schemas.py` (shared Pydantic types), `auth.py` (JWT encode/decode with the shared secret), `recovery.py` (force-drain-outbox, rebuild-enforcement-cache), `config.py` (logging ring buffer surfaced at `/admin/logs`). Go services use hand-written equivalents. Putting them in `shared/` guarantees every Python service handles correlation IDs, DLX routing and circuit breakers identically.

### Q7. How are your services configured and deployed? Walk through the compose file.
**Tests:** infrastructure, deployment model.
**Answer:** Full stack = 26 containers ([docker-compose.yml](docker-compose.yml)): HAProxy → 2 nginx → 6 services → 4 Postgres primary+replica pairs → Redis primary+replica + 3 Sentinels → 3 RabbitMQ nodes → frontend. Slim profile ([docker-compose.slim.yml](docker-compose.slim.yml)) = 12 containers, single primary DBs, single nginx, single RMQ, still real services. Every service is healthchecked on `/health`. Env is driven by `DATABASE_URL`, `DATABASE_READ_URL`, `REDIS_URL`, `REDIS_SENTINEL_ADDRS`, `RABBITMQ_URL`, `JWT_SECRET` (shared across nodes for cross-laptop session continuity), `PEER_CONFLICT_URLS`/`PEER_USER_URLS`, `MY_*_URL`. For multi-laptop we register peers at runtime via `POST /admin/peers/register` + `register_peers.sh`.

### Q8. Describe the notification flow from a confirmed booking to a driver's browser tab.
**Tests:** async fan-out, WebSocket registry, dedup.
**Answer:** After journey-service commits the `outbox_events` row, the 2-second drainer publishes `journey.confirmed` to `journey_events`. Notification-service's `notification_events` queue (bound to all `journey.*` routing keys, with `journey_events_dlx` DLX and 24h `x-message-ttl`) receives the delivery. The consumer runs `notifIsDuplicate(msg)` — `EXISTS notif:processed:{MessageId}` on Redis ([consumer.go:138-157](notification-service/consumer.go#L138)). If fresh, `handleEvent` renders the template, `storeNotification` does a pipelined `LPUSH + LTRIM 0 49 + EXPIRE 7d` on `notifications:{user_id}`, and `pushToWS` fans out to every `*websocket.Conn` for that user from the `wsConns map[string][]*websocket.Conn` guarded by an RWMutex. `notifMarkProcessed` sets the dedup key for 24 h, then `msg.Ack(false)`. Dead connections are lazily removed on the next write error. **Known limit:** the WS registry is in-process; on restart we lose all live connections, though the 7-day history survives.

---

## Tier 2 — "Justify Your Decisions" (9)

### Q9. Why saga instead of distributed 2PC by default?
**Tests:** trade-off between liveness and atomicity.
**Answer:** XA 2PC blocks resources between PREPARE and COMMIT. In a booking saga the "resource" is a SERIALIZABLE row lock on the conflict DB — holding it across the coordinator round-trip would lengthen the critical section and hurt throughput under concurrent booking. The saga collapses check+reserve into one conflict-service transaction, so reservation happens at a single commit point. The downside is compensation: if journey-service crashes after the reserve but before writing the journey row, we leak capacity. We made TCC available as `?mode=2pc` for the strict path ([coordinator.py](journey-service/app/coordinator.py)), but kept saga as the default because (a) the leak window is <100 ms in practice, (b) the periodic lifecycle scheduler would expire the slot anyway if arrival time passes. **Trade-off accepted:** liveness and latency over perfect atomicity.

### Q10. Why SERIALIZABLE on the conflict check but not elsewhere?
**Tests:** isolation-level performance awareness.
**Answer:** SSI adds a read-tracking overhead and is prone to `40001` aborts that need retry logic. We only need it where a full RMW cycle against the same rows happens concurrently — the cell counter check on a busy route. Everywhere else (user CRUD, journey status CRUD, analytics insert) is append-only or single-row and `READ COMMITTED` with row locks (`SELECT FOR UPDATE` on the journey points wallet) is both cheaper and sufficient. Report §4.2 spells this out: "SERIALIZABLE is used only in the Conflict Service to avoid locking overhead on ordinary CRUD paths."

### Q11. Why Redis for notification history but Postgres for event_logs?
**Tests:** data-model rationale.
**Answer:** Notification history is a **per-user capped list with a short TTL** — exactly what Redis `LPUSH + LTRIM + EXPIRE` is optimised for. We don't need to query by time range, join, or audit it. Event_logs in analytics is the **immutable audit trail** — queried by event type, time range, joined against user IDs, and kept forever. That's a Postgres use case. Using Redis for event_logs would force us to reinvent secondary indexes; using Postgres for WS history would waste transaction machinery on a cache-class access pattern.

### Q12. Why are you using RabbitMQ instead of Kafka?
**Tests:** broker choice.
**Answer:** Our event volume is small (booking rate targets are hundreds/s), our consumers are online microservices not streaming pipelines, and we need **per-queue dead letter routing with TTL semantics** — RabbitMQ `x-dead-letter-exchange` + `x-message-ttl` gives us that out of the box. Kafka would force us to own consumer offsets and rebuild DLQ semantics in application code. RabbitMQ's push-based `Consume` also maps cleanly to our `async` consumer loops. Kafka's real strengths — huge replay windows, partition-based throughput — don't apply at our scale.

### Q13. What consistency model does each service provide and why?
**Tests:** CAP per service.
**Answer:** **Conflict on a single node: CP (strong).** Booking is the only operation that can cause double-spending, so we prefer rejecting a booking during a partition over risking an overlap. Cross-node slot replication is **AP** — we accept brief double-booking windows for write availability. **Enforcement: AP.** Roadside checks must never hang; we return cached data with `X-Data-Staleness: STALE`. **Notification, Analytics: AP (eventually consistent).** Outbox + at-least-once + dedup guarantees no events are lost but ordering is per-queue only. **User across nodes: AP with availability-biased lock** — the Redlock-style 2-phase lock skips unreachable peers rather than blocking registration. **User on a single node: strongly consistent via Postgres uniqueness.** This per-service choice is intentional — report §2.2 sets these targets.

### Q14. Why the grid-cell road model instead of graph-based conflict detection?
**Tests:** spatial data structure reasoning.
**Answer:** A graph-based model (segments as edges, bookings as path reservations) is more precise but requires every booking to either (a) match the graph topology exactly, or (b) run a shortest-path query during conflict check. For an academic demo with bespoke lat/lng pairs, that's a lot of infrastructure for marginal precision. The grid is O(1) per cell, locking is O(cells along path), and the unique index on `(grid_lat, grid_lng, time_slot_start)` is cheap to SELECT FOR UPDATE. The known **false conflict** failure mode — two different real roads crossing the same ~1 km cell — is mitigated by the `route_id` + waypoints lookup which walks the real road polyline instead of a straight line ([service.go:258-291](conflict-service/service.go#L258-L291)).

### Q15. Why write the enforcement cache key TTL as `(arrival − now) + 3600 s`?
**Tests:** cache replacement policy rationale.
**Answer:** The journey window is `[departure, arrival]`. If we set TTL = arrival − now, the cache entry would vanish exactly at arrival, creating a window where a vehicle still physically driving shows up as `is_valid=false` to a roadside agent arriving late. Adding a 1-hour cushion absorbs clock skew, traffic delay, and the interpolation buffer ([consumer.py:59](enforcement-service/app/consumer.py#L59)). Because Redis's `allkeys-lru` eviction policy is active, a bloated cache won't OOM — stale entries are evicted under pressure.

### Q16. Why is there no leader election?
**Tests:** consensus understanding, honesty about trade-offs.
**Answer:** We didn't need dynamic leader election because the "primary" for a given shard is derivable deterministically from the known peer list via `MD5(key) % num_nodes`. No runtime agreement is needed — every node computes the same answer. The cost of this is that we can't tolerate a misconfigured peer list or a silent peer drop without manual intervention. The "Local-only mode" threshold (<50% peers alive) is a crude liveness guard but is not a consensus protocol. Report §5.4.2: "lightweight threshold quorum, sufficient for a two-laptop demo without full Paxos/Raft complexity." The honest rehearsed answer: **Raft would be the right thing in production; we did not implement it because it would dwarf the rest of the exercise.**

### Q17. Why outbox polling at 2 s and not push via LISTEN/NOTIFY?
**Tests:** simplicity vs latency.
**Answer:** `LISTEN/NOTIFY` would give us millisecond latency but tie us to Postgres client internals (pgbouncer in transaction mode drops `NOTIFY`). A 2-second poll adds up to 2 s of fan-out latency, well below the booking latency budget of 400 ms p95 **for the synchronous confirmation** — notifications are async and the driver doesn't need them before the POST returns. The trade-off is measurable cost on the synchronous confirm (it's unaffected) for massive simplicity on the async path. If we cared about sub-second fan-out we'd set up a persistent AMQP channel from the publisher, but for the demo 2 s is invisible.

---

## Tier 3 — "Break Your Application" (8)

### Q18. Node A and Node B both receive a booking for the same slot at exactly the same millisecond. Prove which one wins and what happens to the loser.
**Tests:** cross-node consistency guarantees (deliberately adversarial).
**Answer:** **Neither wins deterministically.** Each local conflict-service independently runs its SERIALIZABLE tx and commits locally — both succeed because the peer slot has not yet been replicated ([conflict-service/replication.go:161](conflict-service/replication.go#L161) is fire-and-forget). Within milliseconds, A's `POST /internal/slots/replicate` arrives at B and vice versa. `applyReplicatedSlot` checks `EXISTS WHERE journey_id = $1` and because journey_ids differ, both inserts succeed. The result: two active bookings on the same cell — a silent double-booking. This is the **millisecond-window double-booking** the report openly flags ([§testing limitation](docs/FinalReportEx2.tex#L840)). **What we would do in production:** move the reservation into a single shared store (Postgres primary that all laptops write to), or add Raft-replicated state on top of the slot table. **Don't bluff — admit the gap.**

### Q19. What happens if RabbitMQ is down for 30 minutes during peak booking load?
**Tests:** durable messaging, recovery path.
**Answer:** The **synchronous booking path keeps working** — it never touches RMQ directly. `save_outbox_event` writes to `outbox_events` inside the journey tx. The outbox publisher's 2-second poll runs, tries `broker.publish`, catches the exception, `break`s the loop, sleeps 2 s, retries. On reconnect, `get_broker()` reinitialises via `aio_pika.connect_robust` and the publisher drains the accumulated backlog. **What breaks downstream:** drivers receive no live notifications during the outage (WebSocket history also won't update), enforcement cache entries stop being invalidated on cancel (a cancelled booking stays valid-looking until the event replays), analytics counters freeze. On recovery, all of this converges as the backlog drains and idempotent consumers process each event once. **Hard bound:** if the RMQ queue's 24h `x-message-ttl` expires first, events fall into the DLX and are preserved there for manual replay via `replay_dlq()` in [shared/messaging.py](shared/messaging.py#L207).

### Q20. I kill `journey-service` 50 ms after a SERIALIZABLE commit in `conflict-service` but before the journey row's status is updated to CONFIRMED. What state is the system in?
**Tests:** atomicity gap in the saga, 2PC motivation.
**Answer:** **Leaked reservation.** `conflict-service` has committed the `booked_slots` insert and the cell capacity increment. `journey-service` never got to update the journey from PENDING to CONFIRMED or write the outbox event, so no downstream consumer will ever be told — enforcement will never cache, notification will never push, analytics will never count. The driver sees a failed request, retries, and gets rejected because the first reservation is still occupying the slot. **Mitigation available:** `?mode=2pc` mode wraps the reserve in a try/except and emits a compensating `POST /api/conflicts/cancel/{journey_id}` on any failure after PREPARE, against the same peer URL that did PREPARE ([coordinator.py:144-152](journey-service/app/coordinator.py#L144)). **But** it's best-effort: if journey-service crashes before even reaching the except block, there is no saga compensator. The lifecycle scheduler eventually moves the slot past its departure time anyway, so the leak is bounded but not recovered. **Honest answer: yes, this is a real window, and the only production-grade fix is a persistent saga log.**

### Q21. Can you guarantee a driver who books successfully will always see a WebSocket notification?
**Tests:** reliability of async fan-out.
**Answer:** **No, we can only guarantee the event is produced.** We can prove the event is persisted (outbox + durable AMQP) and at-least-once-delivered to notification-service. We **cannot** guarantee the user's WebSocket is connected at the moment of delivery. If the user's browser is closed, the push drops. The 7-day Redis notification history is the **recovery path** — next time the user connects, they `GET /api/notifications/` and fetch the last 50. If notification-service restarts mid-delivery, the redelivery pulls from RMQ (at-least-once) and dedup via `SETNX notif:processed:{msgID}` avoids duplication. **Weak spot:** if the browser is offline AND the user never reconnects within 7 days, the notification is lost forever. A production system would retain a persistent notifications table or emit email/SMS as a backup channel.

### Q22. What happens if the Redis primary dies during a booking?
**Tests:** Sentinel failover behaviour.
**Answer:** Sentinel (quorum 2 of 3) detects `down-after-milliseconds 5000`, runs the election, promotes the replica, notifies all Sentinel clients. Services using `sentinel.master_for(...)` transparently reconnect to the new master ([enforcement-service/app/service.py:33-41](enforcement-service/app/service.py#L33-L41)). Expected failover window ~15 s. During that window: **idempotency records** are still durable in Postgres so booking retries still work, **enforcement cache** lookups return None → fall through to journey-service REST, **notification history writes** fail (best-effort; history may have a gap), **consumer dedup** fails open (may re-deliver a message — but downstream is idempotent). No data loss; brief staleness.

### Q23. Can you guarantee `/api/enforcement/verify/vehicle/…` under 200 ms even when `journey-service` is dead?
**Tests:** latency under partial failure.
**Answer:** **Yes if the cache is warm, no if it isn't.** A cache hit is <20 ms p95 ([report §2.2](docs/FinalReportEx2.tex#L225)). A cache miss triggers the `journey-service` fallback which will hit `httpx.AsyncClient(timeout=10)` → with the journey-service dead, the connection error returns within ~100–500 ms depending on DNS + TCP. The partition manager adds `X-Data-Staleness: STALE` and returns `is_valid=false`. The 200 ms SLA holds only for cache hits. **Known gap:** after a cold start the cache is empty until the first event arrives, so the first N lookups all miss and pay the fallback cost. The `POST /admin/recovery/rebuild-enforcement-cache` endpoint exists to backfill from `journeys_db` but is not run automatically at boot.

### Q24. If both laptops drop the same hotspot for 60 s, what does the system look like when they reconnect?
**Tests:** partition healing, reconciliation.
**Answer:** During the partition: each laptop is running its full stack, so all reads and writes continue locally. Booking still works on each side independently — but replication pushes are failing. `PartitionManager` flips `conflict-service` dependency to PARTITIONED on the peer's URL after ~15 s (3 failed probes × 5 s). `PeerHealthMonitor` flips peer nodes to SUSPECT then DEAD. On heal, three mechanisms converge the state: (1) in-flight `replicateSlotToPeers` resumes on the next booking, (2) `PeerHealthMonitor` sees a successful probe and transitions DEAD→ALIVE, (3) the 5-minute `startPeriodicSync` calls `/internal/slots/active` on each peer and applies missing rows idempotently. **What remains broken:** bookings made to both laptops for the same slot during the partition will both be kept in both DBs (no conflict resolution beyond "journey_id differs"). A driver could show up with two valid bookings for the same slot. We accept this because no consensus protocol is in the loop.

### Q25. Walk me through simulating a full node kill and prove the system continues.
**Tests:** fault injection, recovery, session continuity.
**Answer:** `POST /admin/simulate/fail` on journey-service ([main.py:162-184](journey-service/app/main.py#L162)) sets `_node_failed=True` so `/health` returns 503 and every booking/admin endpoint also returns 503. It then cascades via `POST /admin/simulate/fail` to user-service on the same node so login/register are also killed. Peer `PeerHealthMonitor` on the other laptop sees 3 missed pings → SUSPECT, 6 missed → DEAD. When >50% of registered peers are DEAD, `_local_only_mode=True`. In the browser, `resilientFetch` catches the 5xx on its current URL, iterates through the `localStorage` peer list, and retries on the next URL. Because `JWT_SECRET` is shared across nodes, the same JWT is accepted on the new node without re-login. Active WebSocket drops and reconnects to the peer after 2 consecutive disconnects. **What doesn't survive:** any in-flight booking on the dying node whose outbox has not yet drained — the outbox is on the local primary DB, which is still running (only the service layer died), so on `POST /admin/simulate/recover` the drainer resumes and catches up. **Recovery is idempotent** — redelivered events hit Redis SETNX and no-op.

---

## Hit-list of Gaps — Know these cold before the viva

| Gap | Where in code | If asked, say |
|---|---|---|
| Millisecond-window double-booking across nodes | [replication.go:161](conflict-service/replication.go#L161) fire-and-forget push | Acknowledged trade-off; fix = shared store or Raft. |
| No retry on SSI abort | [saga.py:48-77](journey-service/app/saga.py#L48) catches `Exception` → REJECTED | Would add bounded retry with jittered backoff in production. |
| No saga compensation loop (only one-shot TCC) | [coordinator.py:184-200](journey-service/app/coordinator.py#L184) "best-effort" | Known gap §3.5. |
| Enforcement cache cold start | [enforcement-service/app/service.py](enforcement-service/app/service.py) — no boot warm | Manual `POST /admin/recovery/rebuild-enforcement-cache` exists. |
| No JWT denylist | Relying on expiry only | Known limitation §3.5. |
| Audit HMAC chain not completed | `AUDIT_HMAC_SECRET` env var unused | Honest about incomplete feature. |
| RabbitMQ cluster unreliable on single host | [docker-compose.yml:100-129](docker-compose.yml#L100) | "Single-node deployment demonstrates all messaging semantics." |
| No Jaeger/Zipkin — only correlation IDs | [shared/tracing.py](shared/tracing.py) | Propagation works; visualisation skipped for scope. |
| Fallback path doesn't repopulate cache | [enforcement-service/app/service.py:85-99](enforcement-service/app/service.py#L85) — code doesn't SETEX on fallback hit despite docstring | Noted discrepancy; fix is a one-line SETEX on fallback success. |
| WebSocket registry in-memory | [notification-service/consumer.go:62-65](notification-service/consumer.go#L62) | Service restart drops live conns; Redis history is the recovery. |

---

## Final Rehearsal Tips

1. **Always answer "per service, not per system"** when asked about CAP, consistency, or replication. Different parts of our stack sit on different points of the triangle — this is explicitly the architectural decision.
2. **When asked "what about X?" where X is missing, say so immediately and give the mitigation** (either an existing one in code, or the production-grade thing you'd add). The professor is testing calibration, not encyclopedic knowledge.
3. **The transactional outbox, SERIALIZABLE check-and-reserve, and resilient peer failover are your three signature patterns** — rehearse them to sentence-level fluency.
4. **Don't defend the active-active double-booking window.** It's documented. Say: "this is the cost of not having a consensus protocol, and we chose latency over strict uniqueness for this scale."
5. **Know at least one file+line citation for every major claim.** If you can say "`service.go:67`, the `pgx.TxOptions{IsoLevel: pgx.Serializable}`", the examiner will trust everything else you say.
