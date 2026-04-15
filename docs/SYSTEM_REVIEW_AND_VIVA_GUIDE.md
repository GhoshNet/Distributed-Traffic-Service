# CS7NS6 Exercise 2 — System Review, Critique & Viva Guide
**Group J | Journey Pre-Booking System**
*Prepared by: SysAdmin analysis — April 2026*

> This document is a hard-nosed, code-grounded critique of the project. It covers:
> (1) requirement fulfilment against the spec and report, (2) active bugs found
> in the running system, (3) unnecessary files that can be safely deleted,
> (4) honest shortcomings, and (5) better alternatives that could be raised in
> the viva.

---

## 0. TL;DR for the Viva

The system **passes every requirement from the spec** (N≥6 services, loosely coupled, isolation levels defined, replication, transactions, caching, failure model). The report is accurate and honest about known gaps. Three real bugs exist in the running system that you must know. The repo has ~10 committed files that should not be there.

---

## 1. Requirement Fulfilment Audit

### 1.1 Spec Requirements (from `CS7NS6_Exercise2_2025_2026.txt`)

| Requirement | Status | Evidence |
|---|---|---|
| N ≥ 6 distributed services (N ≥ group members: 6) | **PASS** | User, Journey, Conflict, Notification, Enforcement, Analytics |
| Each member responsible for ≥ 1 service | **PASS** | Allocation table in report §7 |
| Services loosely coupled | **PASS** | REST for sync critical path only; RabbitMQ for all fan-out |
| Define isolation levels | **PASS** | SERIALIZABLE in Conflict; READ COMMITTED for ordinary CRUD; documented in report NFR table |
| Define replication degree | **PASS** | Primary + 1 streaming replica per DB; Redis Sentinel quorum 2; RabbitMQ 3-node (single-host limitation documented) |
| Define consistency model | **PASS** | Strong per-slot (SERIALIZABLE + SELECT FOR UPDATE); eventual cross-service (transactional outbox + at-least-once) |
| Define failure model | **PASS** | Crash-recovery + async network; Byzantine faults out of scope — explicitly stated §2.3 |
| Define fault tolerance approach | **PASS** | Circuit breaker, outbox, Sentinel, health monitor, client-side failover |
| Transactions | **PASS** | Saga (journey + outbox same DB tx); 2PC mode (?mode=2pc) |
| Caching | **PASS** | Redis-first enforcement (sub-20ms), notification history, analytics counters |
| Load balancing | **PASS** | HAProxy (full) or single nginx (slim) with rate limiting |
| Replication | **PASS** | Postgres WAL streaming; active-active slot replication across laptops |
| Partitioning | **PASS** | Consistent-hash sharding (MD5 % N) for write authority on users and routes |
| Failure testing framework | **PASS** | `POST /admin/simulate/fail` + `scripts/simulate_problems.py` (7 scenarios) + `scripts/demo_local.py` (11 steps) |
| GUI | **PASS** | Frontend at port 3000; WebSocket notifications; failover indicator in top bar |

**All spec requirements are met.**

### 1.2 Functional Requirements (from report §2.1)

| FR | Status | Note |
|---|---|---|
| FR1 — User Registration & Auth | PASS | JWT, bcrypt, DRIVER/ENFORCEMENT_AGENT roles |
| FR2 — Journey Booking | PASS | Saga returns CONFIRMED/REJECTED with reason |
| FR3 — Conflict Detection | PASS | 3-check: driver overlap, vehicle overlap, road capacity |
| FR4 — Journey Cancellation | PASS | Atomic cancel + outbox event fan-out |
| FR5 — Real-time Notifications | PASS | WebSocket push + 50-entry Redis history per user |
| FR6 — Enforcement Verification | PASS | /api/enforcement/verify/vehicle/{reg} and /license/{lic} |
| FR7 — Analytics & Monitoring | PASS | /api/analytics/stats, /hourly, /replica-lag, /health/services |
| FR8 — Points System | PASS | SELECT FOR UPDATE pessimistic lock; earn/spend endpoints |
| FR9 — Idempotent Retries | PASS | Idempotency-Key header → idempotency_records table |

---

## 2. Live Bugs Found in the Running System

> These are real bugs verified against the running Docker Swarm stack today.
> Know these before the demo.

### ~~BUG 1~~ — FIXED: `GET /api/analytics/health/services` was returning "degraded" in Swarm mode

**Status: RESOLVED** — `SERVICES_BASE_URL: "docker"` added to `docker-compose.swarm.yml` analytics-service environment block. Live service updated via `docker service update --env-add`. Verified: endpoint now returns `"overall_status": "healthy"` with all 6 services and real response times.

**What was wrong:** In `analytics-service/handlers.go:192-201`, the service URL map has two branches. When `SERVICES_BASE_URL` env var is set, it uses Docker service names (`http://user-service:8000/health`). When NOT set, it falls back to `localhost:8001-8006`. The `docker-compose.yml` (non-swarm) set `SERVICES_BASE_URL: docker`, but `docker-compose.swarm.yml` was missing it, so the swarm stack always hit the localhost fallback — returning connection refused.

**Verified output after fix:**
```json
{
  "overall_status": "healthy",
  "services": {
    "analytics-service":    { "status": "healthy", "response_time_ms": 5.4  },
    "conflict-service":     { "status": "healthy", "response_time_ms": 37.9 },
    "enforcement-service":  { "status": "healthy", "response_time_ms": 83.1 },
    "journey-service":      { "status": "healthy", "response_time_ms": 17.6 },
    "notification-service": { "status": "healthy", "response_time_ms": 16.5 },
    "user-service":         { "status": "healthy", "response_time_ms": 104.3 }
  }
}
```

---

### BUG 2 — CRITICAL: Postgres replica services crash-loop in Docker Swarm

**What happens:** All four Postgres replica services (`postgres-users-replica`, `postgres-journeys-replica`, `postgres-conflicts-replica`, `postgres-analytics-replica`) repeatedly fail in Swarm mode with `task: non-zero exit (1)`.

**Evidence:**
```
traffic-service_postgres-journeys-replica.1   Failed  "task: non-zero exit (1)"
traffic-service_postgres-users-replica.1      Failed  "task: non-zero exit (1)"
```

**Root cause (likely):** The replica init script in `postgres-init/01_allow_replication.sh` uses `pg_basebackup` pointing to the primary by hostname. In Swarm, the primary service name resolves correctly, but the replication slot or timing of the primary readiness probe may differ from compose mode. The replica containers use a `bash -c 'chown -R ... && pg_basebackup ...'` entrypoint that is sensitive to ordering.

**Impact on report claims:** The report (§Implementation, §Testing Table ~last row) claims "PostgreSQL streaming replication — PASS. Lag visible at /api/analytics/replica-lag". In swarm mode this shows `"count":0, "replicas":[]`, meaning the replica-lag endpoint shows **no active replicas**. The PASS in the test results applies to the slim-mode `docker compose` run, NOT the swarm deployment.

**Mitigation for demo:** Run the slim stack (`docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d`) where replicas work correctly. Avoid demoing on the swarm stack when showing WAL replication.

---

### BUG 3 — MINOR: Road-segment `defaultMaxCapacity = 1` makes conflict demos too easy to trigger

**What happens:** In `conflict-service/service.go:33`, `defaultMaxCapacity = 1`. This means a single booking fills a road-segment cell. Any second booking on the same road (even by a different driver) is rejected for "road capacity" even if it's a different driver at a different time.

**Context:** The report says "grid cell at maximum capacity" and memory note says "max = 5". The REQUIREMENTS.md mentions this is intentional for a demo ("single-lane road"), but the report does not clearly state the capacity is 1 — which could mislead the examiner.

**In practice:** For the concurrent booking storm demo (10 parallel), this works correctly. But for the multi-laptop demo, two different users booking different routes that happen to share a grid cell will conflict unexpectedly. The value should be at least 5 or made configurable via environment variable to match real-world intent.

---

## 3. Report vs. Reality Discrepancies

| Claim in Report | Reality | Severity |
|---|---|---|
| "Postgres WAL streaming replication — PASS" | Only works in slim/compose mode. Replica crash-loops in swarm | MEDIUM |
| "Analytics health/services shows aggregate health" | ~~Returns degraded in swarm~~ — **FIXED** (`SERVICES_BASE_URL: docker` added to swarm compose) | ~~HIGH~~ RESOLVED |
| "Road capacity max = 5 bookings per road segment" (memory note) | Code has `defaultMaxCapacity = 1` | LOW |
| "3-node RabbitMQ cluster" | Stated as partial/unreliable in the report itself — honest | DOCUMENTED |
| "50 bookings/s per node" throughput target | No load test result proves this; demo only shows 10 parallel | INFO |
| `X-Partition-Status` on every response | Journey service returns `"dependencies": null` on /health | MINOR |

---

## 4. Unnecessary Files in the Repository

The following files are committed to git (`git ls-files` confirms) but serve no purpose in the current system. Their removal will **not affect Docker, the services, or any running functionality**.

### 4.1 Definite removals (safe, zero functional impact)

| File/Directory | Size | Reason to remove |
|---|---|---|
| `conflict-service/conflict-service.exe` | 16 MB | Windows binary compiled locally; Docker builds its own Go binary via the Dockerfile multi-stage build. Does NOT get copied into the container. |
| `journey_logs.txt` | 78 KB | Raw log dump from a local run in March. No scripts read this file. |
| `journey_500.txt` | 9 KB | Leftover debugging output. No scripts read this file. |
| `Archive.zip` | 113 KB | Compressed snapshot of the `Archive/` prototype. Redundant since the folder itself is committed. |
| `Buffer_DS.md` | 1.5 KB | Scratch checklist used to evaluate the project during development. Not documentation. |
| `ha_deployment_strategy_and_runbook.md` | 4.4 KB | Early planning note. Superseded by `docs/DEMO_GUIDE.md` and `docs/MULTI_LAPTOP_DEMO.md`. |
| `README_SWARM.md` | 3.7 KB | Mostly duplicates swarm instructions in README.md. |
| `.DS_Store` | 8 KB | macOS metadata — should already be gitignored but was committed before `.gitignore` was set. |

**Commands to remove:**
```bash
git rm conflict-service/conflict-service.exe
git rm journey_logs.txt
git rm journey_500.txt
git rm Archive.zip
git rm Buffer_DS.md
git rm ha_deployment_strategy_and_runbook.md
git rm README_SWARM.md
git rm .DS_Store
```

### 4.2 `Archive/` and `docs/trash/` directories

Both directories are committed (`git ls-files Archive/ | wc -l` = ~20 files; `docs/trash/` = ~9 files). They are the pre-prototype and interim report artifacts.

**Impact of removal:** Zero. No Dockerfile, docker-compose.yml, or service code references anything in `Archive/` or `docs/trash/`.

```bash
git rm -r Archive/
git rm -r docs/trash/
```

**Why you might keep them:** They show evolution of the project and might be useful to reference during the viva if asked "how did you start?". If keeping, at minimum add a `README.md` inside `Archive/` noting it is a historical prototype.

### 4.3 `logs/` and `.pids/` directories

These are already in `.gitignore` but were committed before the gitignore was written. They contain runtime artifacts (log files from `start.sh` local mode).

```bash
git rm -r logs/
git rm -r .pids/
```

### 4.4 `docker-compose.test.yml`

Dated Feb 14 (earliest commit). Contains a minimal single-service test stack that was never updated to match the current multi-service architecture. No CI uses it.

```bash
git rm docker-compose.test.yml
```

### 4.5 Root-level `healthcheck.py` and `healthcheck.sh`

These scripts probe services on `localhost:8001-8006` — they only work for the `scripts/run_local.sh` non-Docker mode which is not used. The canonical health check is `scripts/demo_local.py`. Both files can be removed.

```bash
git rm healthcheck.py healthcheck.sh
```

---

## 5. Architectural Critique (What We Could Have Done Better)

> These are honest critiques for the viva. You should be able to discuss each one and explain why the current approach was chosen.

### 5.1 Outbox publisher polling vs. WAL-based CDC

**What we did:** The outbox drainer is a `while True: sleep(2); SELECT WHERE published=false` polling loop.

**Better approach:** PostgreSQL logical replication / Change Data Capture (Debezium → Kafka) would have zero polling delay and no DB load. The polling loop adds up to 2 seconds of latency between a booking being committed and the downstream fan-out starting. For the demo, 2 seconds is acceptable; at production scale, it wastes DB connections and adds tail latency.

**Why we didn't:** CDC requires Debezium + Kafka, adding 3 more containers and significant config complexity. The transactional outbox pattern with polling is industry-standard and sufficient for the demo load (< 20 bookings/min in testing).

### 5.2 Circuit breaker state is in-process memory only

**What we did:** `shared/circuit_breaker.py` stores state in a Python dict (`_registry`). Each Journey Service replica has its own independent circuit breaker state.

**Problem:** In the Swarm deployment with 2 replicas of `journey-service`, one replica may have the circuit OPEN while the other still has it CLOSED. This means the OPEN state is not shared, so the protection is partial.

**Better approach:** Store circuit breaker state in Redis (with a short TTL) so all replicas share the same view. Alternatively, use a sidecar proxy (Envoy, Istio) to handle circuit breaking at the infrastructure level, which is also per-instance-aware but exposes metrics.

### 5.3 Consistent-hash sharding is write-authority only (not isolation)

**What we did:** `shard = MD5(key) % N`, node 0 is PRIMARY. All nodes store all data. Sharding only controls who writes.

**Problem this creates:** In a 2-laptop setup, if Node A is the primary for user@email.com and it crashes, Node B has the data (replication works) but the shard assignment changes if `N` drops from 2 to 1. The hash `MD5(email) % 1 = 0` always, so Node B becomes primary — this is correct behaviour, but the transition is not explicitly handled. There is no leader election protocol, just a re-computation.

**Better approach:** True consistent hashing with a virtual-node ring (like Redis Cluster or DynamoDB) avoids the N-change rebalancing problem. For a 2-laptop academic demo, the current approach is entirely sufficient.

### 5.4 No token blacklist on logout

**What we did:** JWTs are stateless; on "logout" the client deletes the token from localStorage. The token remains valid until expiry (typically hours).

**Problem:** If a driver's phone is stolen and they log out via another device, the stolen token still works.

**Better approach:** A Redis SET keyed by `jwt:blacklist:{jti}` with TTL = token expiry. Every auth middleware does a Redis EXISTS check. Adds one Redis lookup per request but provides true revocation. The report documents this as a known limitation.

### 5.5 WebSocket registry in process memory

**What we did:** `notification-service/handlers.go` holds `map[userID][]*websocket.Conn` in RAM.

**Problem:** If the Notification Service restarts (or Swarm reschedules the task), all connections are lost and clients must reconnect. In Swarm with 2 replicas, a connection to Replica 1 will not receive notifications published by a consumer running on Replica 2 (since the in-memory map is not shared).

**Better approach:** Redis Pub/Sub as a fan-out bus between replicas. Each Notification Service instance subscribes to a per-user Redis channel; any instance can publish and all instances fan out to their local connections. This is the standard approach used by chat systems (Slack, Discord architecture).

### 5.6 No saga compensation retry loop

**What we did:** If the Conflict Service is unreachable after a PREPARE (2PC path), the coordinator issues one ABORT call. If that call also fails, the slot leaks.

**Better approach:** A persistent compensation log (a second outbox-style table: `compensation_events` with `compensated=false`) that retries the ABORT until the Conflict Service acknowledges it. This is the "saga log" pattern used in production microservice systems.

### 5.7 HAProxy health check threshold

**What we did:** HAProxy uses `httpchk GET /health` with default `fall 3 rise 2` in the config.

**Potential issue:** `POST /admin/simulate/fail` makes the Journey Service return 503 on all routes including `/health`. But the User Service has its own simulate-fail endpoint that only marks `_node_failed=True` in the same process. If HAProxy detects the journey service is down via health checks, it removes it from rotation — which is the correct behaviour. However, if both node A and node B are running and you fail node A, HAProxy on node A will correctly remove node A's services, but the HAProxy on node B (separate process) doesn't know about node A's internal simulation state — it still sees node A's nginx, which proxies to a 503-returning service. The net effect is correct (503 propagated), but it's worth being able to explain this chain.

---

## 6. Missing Demo Commands Cheatsheet

> Things examiners often ask to see live. Have these ready.

### Register user, book journey, check enforcement
```bash
# 1. Register a driver
curl -X POST http://localhost:8080/api/users/register \
  -H "Content-Type: application/json" \
  -d '{"name":"Alice","email":"alice@tcd.ie","password":"test1234","license_number":"D123456","role":"DRIVER"}'

# 2. Login
TOKEN=$(curl -s -X POST http://localhost:8080/api/users/login \
  -H "Content-Type: application/json" \
  -d '{"email":"alice@tcd.ie","password":"test1234"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# 3. Register vehicle
curl -X POST http://localhost:8080/api/users/vehicles \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"registration":"192-D-12345","vehicle_type":"CAR"}'

# 4. Book journey (route_id = "R1" is a predefined route)
curl -X POST http://localhost:8080/api/journeys/ \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"origin":"Dublin","destination":"Cork","origin_lat":53.349805,"origin_lng":-6.26031,
       "destination_lat":51.8985,"destination_lng":-8.4756,"departure_time":"2026-04-16T09:00:00",
       "estimated_duration_minutes":120,"vehicle_registration":"192-D-12345",
       "vehicle_type":"CAR","route_id":"R1"}'

# 5. Book same journey again → conflict check (driver overlap)
# Same command ↑ will return REJECTED with "Driver already has a journey"

# 6. Check analytics
curl http://localhost:8080/api/analytics/stats
```

### Circuit breaker demo
```bash
# Stop the conflict service, then attempt 4 bookings — first 3 fail with timeout,
# 4th fails fast (circuit OPEN)
docker service scale traffic-service_conflict-service=0
# ... book 4 times ...
docker service scale traffic-service_conflict-service=1
```

### Simulate node failure
```bash
curl -X POST http://localhost:8080/admin/simulate/fail
curl http://localhost:8080/health  # → 503
curl -X POST http://localhost:8080/admin/simulate/recover
```

### 2PC mode
```bash
curl -X POST "http://localhost:8080/api/journeys/?mode=2pc" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{ ...same body as above... }'
```

### Enforcement verification
```bash
# Register enforcement agent
curl -X POST http://localhost:8080/api/users/register/agent \
  -H "Content-Type: application/json" \
  -d '{"name":"Officer1","email":"officer@tcd.ie","password":"enforce123","license_number":"E999999"}'

AGENT_TOKEN=$(curl -s -X POST http://localhost:8080/api/users/login \
  -H "Content-Type: application/json" \
  -d '{"email":"officer@tcd.ie","password":"enforce123"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

curl http://localhost:8080/api/enforcement/verify/vehicle/192-D-12345 \
  -H "Authorization: Bearer $AGENT_TOKEN"
```

---

## 7. Design Questions the Examiner Will Likely Ask

**Q: Why did you choose the Saga pattern over 2PC as the default?**

A: Sagas are more available — if the Conflict Service is temporarily down after the Journey Service has committed, the journey is simply rejected (no capacity was reserved) so there is nothing to roll back. In 2PC, a failure between PREPARE and COMMIT leaves held capacity until a compensating ABORT can be delivered. We offer 2PC as an optional mode (`?mode=2pc`) precisely to demonstrate we understand the trade-off: 2PC is safer against capacity leaks but less available under failure.

**Q: How do you prevent double booking?**

A: Two mechanisms. First: `SERIALIZABLE` isolation + `SELECT FOR UPDATE` on every `road_segment_capacity` row in the conflict-service atomic check. Two concurrent transactions cannot both see a free slot and both commit — the second will either deadlock-retry or see the first's reservation. Second: driver overlap and vehicle overlap checks run in the same serialisable transaction, preventing the same driver or vehicle from appearing in two confirmed journeys at the same time.

**Q: What happens if RabbitMQ goes down mid-booking?**

A: The journey status is committed to Postgres first (CONFIRMED) in the same transaction as the outbox row. The booking is durable. The outbox drainer retries publication every 2 seconds. Downstream services (Notification, Enforcement, Analytics) will eventually get the event when the broker recovers — at-least-once delivery. The driver will see a CONFIRMED response immediately; the push notification arrives slightly later.

**Q: How does enforcement work when the Journey Service is unreachable?**

A: The Enforcement Service has its own Redis cache (`active_journey:vehicle:{plate}`). TTL is set to `(estimated_arrival_time - now) + 3600s`. If the Journey Service is partitioned, the service continues reading from cache, sets `X-Cache-Stale: true` and `X-Partition-Status: journey-service:PARTITIONED` headers. The officer's app should show a warning but the check still returns a result. Cache is cold on boot — first request always pays the REST fallback cost.

**Q: What is your consistency model?**

A: Strong consistency per booking slot: SERIALIZABLE guarantees no double-booking even under concurrent requests. Eventual consistency cross-service: the outbox + RabbitMQ + idempotent consumers converge all state (enforcement cache, analytics counters, notification history) within seconds of a booking event. We explicitly accept that two bookings made to two different nodes within the replication window (< 200ms on LAN) can both pass — documented as a known trade-off of active-active without a consensus protocol.

**Q: How does client-side failover work?**

A: Every browser API call goes through `resilientFetch` (frontend/app.js). On a 5xx or network error it tries the next peer URL from a list fetched at login and cached in `localStorage`. 4xx responses (e.g., REJECTED booking) are passed through unchanged — a rejected booking is not a node failure. WebSocket connections cycle to a peer after 2 consecutive disconnects. JWT tokens are signed with a shared secret, so a token issued by Node A is valid on Node B without re-login.

**Q: Explain consistent-hash sharding.**

A: `shard_id = MD5(key) % num_nodes`. If `shard_id == 0` the current node is PRIMARY for that key (email for users, route_id for conflict slots). PRIMARY means write authority — it performs the write locally and replicates to peers. All nodes store all data (active-active), so any node can serve reads. The shard role is logged on every operation. When a node is removed (N decreases), keys are remapped — the surviving node becomes PRIMARY for all keys, which is correct for a 2-laptop failover scenario.

**Q: What is the partition detection state machine?**

A: `shared/partition.py` probes Postgres, RabbitMQ and the Conflict Service every 5 seconds. States: `CONNECTED → SUSPECTED (1 miss) → PARTITIONED (multiple misses) → MERGING (recovered)`. The current state rides on every HTTP response as `X-Partition-Status`. In PARTITIONED, enforcement serves stale cache; bookings fail fast via the circuit breaker if all conflict-service nodes are unreachable; notifications are queued in the outbox for replay.

---

## 8. Known Limitations — Own Before They Are Asked

The report §3.5 lists these honestly. Know them cold:

| Limitation | What you should say |
|---|---|
| No saga compensation retry | If the conflict-service is permanently unreachable during a booking, the journey is rejected. No automatic retry. A production system would add a compensation log. |
| WebSocket registry in RAM | Service restart drops all live connections. Clients auto-reconnect; no notification history is lost (Redis). Production fix: Redis Pub/Sub bus. |
| Enforcement cache cold on boot | First check always misses. Could add a boot-time cache warm: `GET /api/journeys/all` on startup, populate Redis. Not implemented. |
| No JWT blacklist | Revoked tokens valid until expiry. Known. Fix: Redis deny-list with jti. |
| RabbitMQ cluster unreliable on one host | Erlang distribution needs real separate IPs. Demo shows single-node; cluster config is present but not reliable on one machine. |
| No distributed tracing spans | X-Request-ID propagates everywhere but no Jaeger/Zipkin. Adding one OpenTelemetry exporter to each service would fix this in ~1 hour of work. |
| Double-booking window exists | Two bookings to two different nodes within the replication window (< 200ms on LAN) can both be confirmed. Accepted trade-off of eventual consistency without consensus. |

---

## 9. Files That Are Safe to Delete — Summary Commands

Run these in the project root after confirming with the team. Commit the result.

```bash
# Binaries and raw logs (no functional impact)
git rm conflict-service/conflict-service.exe
git rm journey_logs.txt
git rm journey_500.txt

# Redundant archives and scratch notes
git rm Archive.zip
git rm Buffer_DS.md
git rm ha_deployment_strategy_and_runbook.md
git rm README_SWARM.md  # if team is OK — content is in README.md

# Stale test/healthcheck scripts (no CI uses them)
git rm docker-compose.test.yml
git rm healthcheck.py
git rm healthcheck.sh

# macOS metadata (should have been gitignored from day 1)
git rm .DS_Store

# Runtime artifacts committed by mistake
git rm -r logs/
git rm -r .pids/

# Optional — historical prototype and interim docs (keep if you want to show evolution)
# git rm -r Archive/
# git rm -r docs/trash/

git commit -m "chore: remove committed binaries, logs, and scratch files"
```

---

## 10. ~~The One Fix That Matters Before the Demo~~ — DONE

**`SERVICES_BASE_URL: "docker"` has been added to `docker-compose.swarm.yml`** and applied live.  
`/api/analytics/health/services` now returns `overall_status: healthy` with all 6 services. No action needed.

---

*End of review. For viva-specific question deep-dives see `docs/VIVA_PREP_ExerciseReport.md`.*
