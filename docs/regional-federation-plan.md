# Regional Federation Implementation Plan
## CS7NS6 Distributed Systems — Group J

> **Status:** Proposed — awaiting confirmation before implementation  
> **Date:** 2026-04-08  
> **Branch target:** `testenv`

---

## Table of Contents

1. [Motivation](#1-motivation)
2. [Current vs Proposed System](#2-current-vs-proposed-system)
3. [The 7 Distributed Problems — Before and After](#3-the-7-distributed-problems--before-and-after)
4. [Proposed Architecture](#4-proposed-architecture)
5. [Cross-Region Booking Flow](#5-cross-region-booking-flow)
6. [Files to Create or Modify](#6-files-to-create-or-modify)
7. [New API Endpoints](#7-new-api-endpoints)
8. [Docker Compose Changes](#8-docker-compose-changes)
9. [Demo Script — New Steps](#9-demo-script--new-steps)
10. [What We Are Not Doing (And Why)](#10-what-we-are-not-doing-and-why)
11. [Implementation Order](#11-implementation-order)
12. [Professor's Concerns — Addressed](#12-professors-concerns--addressed)

---

## 1. Motivation

The project prompt and professor's review both identify the same core gap: **the system is logically centralised**. All road capacity data lives in one conflict service, one database. There is no geographic partitioning of data, no inter-region coordination, and no way to demonstrate what happens when one geographic region becomes unavailable.

The team member's proposal (and diagram) describes the correct solution: each geographic region runs its own instance of the conflict service with its own database. Regions speak to each other only when a journey crosses a border. This directly answers every remaining professor concern.

---

## 2. Current vs Proposed System

| Dimension | Current System | Proposed System |
|---|---|---|
| **Conflict service instances** | 1 global instance | N regional instances (one per region) |
| **Road capacity data** | 1 central Postgres table shared by all | Each region owns its own Postgres DB — IE road data never lives in the NI DB |
| **Route knowledge** | All 6 routes in one service | Each region seeds only the routes it owns |
| **Cross-region booking** | Not supported — one service handles everything | Distributed 2-phase saga: each region holds, then commits or rolls back |
| **Service discovery** | Static Docker hostnames, hardcoded config | Registry-based: regions announce themselves to Redis on startup; others find them dynamically |
| **Node join / leave** | Requires manual Docker Compose change | Dynamic — new region starts, registers in registry, peers discover it automatically |
| **Failure simulation** | No mechanism | Per-region HTTP endpoints: simulate delay, failure, network partition, recovery |
| **Data partitioning** | Centralized (professor's specific complaint) | True geographic sharding — grid cells in IE only in IE DB, NI only in NI DB |
| **Cross-border handling** | Dublin→Belfast treated identically to Dublin→Cork | Dublin→Belfast is a 2-phase saga: IE service books IE segment, NI service books NI segment |
| **Graceful degradation** | If conflict-service dies, ALL bookings fail globally | If NI dies, only NI roads and cross-border bookings fail; all IE-only journeys still work |
| **Node recovery** | Manual restart, no reconciliation | Region re-registers on startup, held bookings reconciled, booking resumes |
| **Compensating transactions** | Missing (❌ in README) | Explicitly implemented in Phase 2 rollback of the cross-region saga |

---

## 3. The 7 Distributed Problems — Before and After

### Problem 1: Network Delay
- **Current:** Circuit breaker handles timeout passively. No way to demonstrate or observe delay deliberately.
- **Proposed:** `POST /api/simulate/delay` on any regional node injects N milliseconds into all outbound calls from that region. The demo books a cross-region journey and the observer watches the latency ripple through both services. Analytics records elevated booking time.

### Problem 2: Node Failure
- **Current:** The single conflict-service failing causes a total system outage. All bookings fail.
- **Proposed:** `POST /api/simulate/failure` on the NI node stops it from accepting new requests. Dublin→Belfast returns `REGION_UNAVAILABLE`. Dublin→Galway continues to be confirmed normally by the IE node. The system degrades partially, not completely.

### Problem 3: Data Consistency
- **Current:** SERIALIZABLE transaction within a single database. Consistent, but not distributed.
- **Proposed:** Cross-region 2-phase saga enforces distributed consistency. Phase 1 holds road segments on all involved regions simultaneously. Phase 2 commits all or rolls back all. No journey is ever half-booked.

### Problem 4: Concurrent Data Access / Update
- **Current:** `SELECT FOR UPDATE` within one DB prevents double-booking within that service.
- **Proposed:** Same within each regional DB. Across regions, Phase 1 holds act as distributed locks — a second concurrent cross-border booking on the same segments will find them held and be rejected.

### Problem 5: Splitting Data Across Regions
- **Current:** All `road_segment_capacity` rows in one table. No geographic partitioning. (Professor's primary complaint.)
- **Proposed:** IE grid cells (lat/lng ranges covering Republic of Ireland) live exclusively in `postgres-conflicts-ie`. NI grid cells live exclusively in `postgres-conflicts-ni`. Neither DB has knowledge of the other's road data.

### Problem 6: Node Recovery After Failure
- **Current:** Manual Docker restart. No state reconciliation. Held bookings (if any) are lost.
- **Proposed:** On startup, region re-registers in Redis registry with a fresh TTL. `held_bookings` table is scanned — any holds that expired during downtime are marked `ROLLED_BACK`. The journey service receives a `REGION_RECOVERED` event and may retry held journeys. Other regions see the node appear in the registry and resume routing to it.

### Problem 7: Graceful Degradation
- **Current:** No degradation mode. Full failure or full operation.
- **Proposed:** Three observable degradation states:
  - **IE up, NI up** — all journeys work
  - **IE up, NI down** — IE-only journeys confirmed; cross-border rejected with `REGION_UNAVAILABLE`; cross-border bookings queued or retried after configurable timeout
  - **IE down, NI up** — NI-only journeys work (if any); IE-only and cross-border rejected

---

## 4. Proposed Architecture

```
                    ┌──────────────────────────────────┐
                    │        Journey Service (:8002)    │
                    │   Routing layer in saga.py:        │
                    │   "which regions does this route  │
                    │    cross?" → calls N services     │
                    └────────┬───────────────┬──────────┘
                             │               │
              ┌──────────────▼──┐     ┌──────▼──────────────┐
              │ conflict-ie      │     │ conflict-ni           │
              │ Region: IE       │     │ Region: NI/UK         │
              │ Port: 8003       │     │ Port: 8007            │
              │                  │     │                       │
              │ Owns routes:     │     │ Owns routes:          │
              │  dublin-galway   │     │  dublin-belfast       │
              │  dublin-cork     │     │  (NI segment only)    │
              │  dublin-limerick │◄───►│                       │
              │  galway-limerick │     │ DB: postgres-         │
              │  limerick-cork   │     │   conflicts-ni        │
              │                  │     │                       │
              │ DB: postgres-    │     │ Replica: postgres-    │
              │   conflicts-ie   │     │   conflicts-ni-replica│
              │ Replica: postgres│     │                       │
              │   -conflicts-ie- │     │ Simulation state:     │
              │   replica        │     │   NORMAL / DELAYED /  │
              │                  │     │   FAILED / PARTITIONED│
              │ Simulation state:│     └──────────────────────┘
              │   NORMAL / ...   │
              └──────────────────┘
                       ▲                       ▲
                       └──────── Redis ─────────┘
                           Region Registry
                           Key: region:{id}
                           TTL: 30s heartbeat
                           Fields: url, owned_routes,
                                   status, last_seen
```

### Region Registry (Redis-based, no new container)

Each conflict service instance writes to Redis on startup and every 15 seconds:

```
region:IE  →  { url: "http://conflict-ie:8000",
                owned_routes: ["dublin-galway", "dublin-cork", ...],
                status: "NORMAL",
                last_seen: "2026-04-08T14:00:00Z" }

region:NI  →  { url: "http://conflict-ni:8000",
                owned_routes: ["dublin-belfast"],
                status: "NORMAL",
                last_seen: "2026-04-08T14:00:00Z" }
```

TTL = 45 seconds. If a region stops heartbeating, its key expires and it is considered unavailable. No separate registry service needed — existing Redis handles it.

---

## 5. Cross-Region Booking Flow

### Intra-region (Dublin → Galway, IE only)
```
Journey Service
  1. Queries registry: "who owns dublin-galway?" → IE
  2. Calls conflict-ie /api/conflicts/check (existing flow)
  3. IE confirms atomically
  → CONFIRMED
```
No change from current behaviour. Fully backward compatible.

---

### Cross-region (Dublin → Belfast, IE + NI)

**Phase 1 — Hold**
```
Journey Service
  1. Queries registry: "who owns dublin-belfast?"
     → IE owns the M1 segment south of Newry
     → NI owns the A1 segment north of Newry
  2. POST conflict-ie /api/conflicts/hold  { journey_id, ie_segments }
     → IE reserves IE road cells, writes hold_id to held_bookings table
     → Returns: { hold_id: "abc", status: "HELD", expires_at: T+30s }
  3. POST conflict-ni /api/conflicts/hold  { journey_id, ni_segments }
     → NI reserves NI road cells, writes hold_id
     → Returns: { hold_id: "xyz", status: "HELD", expires_at: T+30s }
```

**Phase 2 — Commit (both held successfully)**
```
  4. POST conflict-ie /api/conflicts/commit/abc  → IE: COMMITTED
  5. POST conflict-ni /api/conflicts/commit/xyz  → NI: COMMITTED
  → Journey CONFIRMED
```

**Phase 2 — Rollback (NI failed in Phase 1)**
```
  3. POST conflict-ni /api/conflicts/hold → [timeout / connection refused]
  4. POST conflict-ie /api/conflicts/rollback/abc → IE releases hold
  → Journey REJECTED: "Region NI unavailable — cross-border booking failed"
  → IE road cells freed immediately
```

**Hold expiry (journey service crashes mid-saga)**
```
  → held_bookings.expires_at = now + 30s
  → Background goroutine scans for expired holds every 10s
  → Expired holds auto-rolled-back, road capacity decremented
  → No permanent ghost bookings
```

---

## 6. Files to Create or Modify

### conflict-service (Go)

| File | Action | Summary of Changes |
|---|---|---|
| `conflict-service/database.go` | Modify | Add `held_bookings` table; `region_peers` cache table; filter seed to owned routes only |
| `conflict-service/service.go` | Modify | Add `holdRoadSegments()`, `commitHold()`, `rollbackHold()`; region-filter on conflict check |
| `conflict-service/handlers.go` | Modify | New handlers: hold, commit, rollback, region info, simulation endpoints |
| `conflict-service/main.go` | Modify | Load region env vars; register with Redis on startup; start heartbeat goroutine; new routes |
| `conflict-service/registry.go` | **New file** | Redis registration, heartbeat loop, peer discovery, TTL management |
| `conflict-service/simulate.go` | **New file** | Simulation state machine: NORMAL → DELAYED → FAILED → PARTITIONED → NORMAL |

### journey-service (Python)

| File | Action | Summary of Changes |
|---|---|---|
| `journey-service/app/saga.py` | Modify | Multi-region saga coordinator: single-region uses existing flow; multi-region runs 2-phase |
| `journey-service/app/registry.py` | **New file** | Query Redis to find which region(s) own a given route_id |

### Infrastructure

| File | Action | Summary of Changes |
|---|---|---|
| `docker-compose.yml` | Modify | Add `conflict-service-ni`, `postgres-conflicts-ni`, `postgres-conflicts-ni-replica`; rename existing conflict service to `conflict-service-ie` |
| `scripts/demo_local.py` | Modify | Add simulation steps: failure, recovery, delay, cross-region saga visible in logs |

---

## 7. New API Endpoints

All new endpoints added to each regional conflict service instance.

### Region Information
```
GET /api/region/info
→ { region_id, region_name, owned_routes, status, peers }

GET /api/region/peers
→ [ { region_id, url, owned_routes, status, last_seen }, ... ]
```

### Distributed Booking (Phase 1 / Phase 2)
```
POST /api/conflicts/hold
Body: { journey_id, route_id, departure_time, estimated_duration_minutes,
        vehicle_registration, user_id }
→ { hold_id, status: "HELD", expires_at }
→ 409 if road capacity exceeded
→ 503 if region in FAILED simulation state

POST /api/conflicts/commit/{hold_id}
→ 204 No Content (hold promoted to active booking)
→ 404 if hold not found or already expired

POST /api/conflicts/rollback/{hold_id}
→ 204 No Content (hold released, road capacity decremented)
→ 404 if hold not found
```

### Simulation Controls
```
POST /api/simulate/delay
Body: { delay_ms: 3000 }
→ All outbound HTTP calls from this region delayed by delay_ms

POST /api/simulate/failure
→ Region stops accepting new /api/conflicts/* requests (returns 503)
→ /health and /api/simulate/* still respond (for control)

POST /api/simulate/recover
→ Region returns to NORMAL state

POST /api/simulate/partition
Body: { target_region_id: "NI" }
→ All outbound calls to NI are dropped (connection refused simulation)

GET /api/simulate/status
→ { state: "NORMAL|DELAYED|FAILED|PARTITIONED",
    delay_ms: 0,
    partitioned_from: [],
    since: "2026-04-08T14:00:00Z" }
```

---

## 8. Docker Compose Changes

### Services renamed / added

```yaml
# Renamed from conflict-service
conflict-service-ie:
  build: ./conflict-service
  environment:
    REGION_ID: IE
    REGION_NAME: "Republic of Ireland"
    REGION_OWNED_ROUTES: "dublin-galway,dublin-cork,dublin-limerick,galway-limerick,limerick-cork"
    DATABASE_URL: postgresql://conflicts_ie_user:conflicts_ie_pass@postgres-conflicts-ie:5432/conflicts_ie_db
    REDIS_URL: redis://redis:6379/0
    PORT: "8000"
  ports:
    - "8003:8000"

# New regional instance
conflict-service-ni:
  build: ./conflict-service       # same image, different env
  environment:
    REGION_ID: NI
    REGION_NAME: "Northern Ireland"
    REGION_OWNED_ROUTES: "dublin-belfast"
    DATABASE_URL: postgresql://conflicts_ni_user:conflicts_ni_pass@postgres-conflicts-ni:5432/conflicts_ni_db
    REDIS_URL: redis://redis:6379/0
    PORT: "8000"
  ports:
    - "8007:8000"

# New DB for NI region
postgres-conflicts-ni:
  image: postgres:16
  environment:
    POSTGRES_DB: conflicts_ni_db
    POSTGRES_USER: conflicts_ni_user
    POSTGRES_PASSWORD: conflicts_ni_pass

postgres-conflicts-ni-replica:
  image: postgres:16
  # WAL streaming from postgres-conflicts-ni (same pattern as existing replicas)
```

### Journey service env update
```yaml
journey-service:
  environment:
    CONFLICT_SERVICE_URL: http://conflict-service-ie:8000   # primary / IE
    CONFLICT_REGISTRY_REDIS_URL: redis://redis:6379/0       # for region lookup
```

The journey service queries Redis to find the correct regional conflict URL for each route. It no longer has a single hardcoded `CONFLICT_SERVICE_URL` for cross-region journeys.

---

## 9. Demo Script — New Steps

The following steps are added after the existing booking steps.

```
Step A: Show region topology
  → GET conflict-ie:8003/api/region/info
  → GET conflict-ni:8007/api/region/info
  → GET conflict-ie:8003/api/region/peers
  Prints: "IE owns 5 routes | NI owns 1 route | Peers: [IE↔NI]"

Step B: Cross-region booking (Dublin → Belfast)
  → Alice books Dublin→Belfast (route_id: dublin-belfast)
  → Logs show Phase 1 hold on IE, Phase 1 hold on NI
  → Logs show Phase 2 commit on both
  → Journey CONFIRMED

Step C: Simulate NI node failure
  → POST conflict-ni:8007/api/simulate/failure
  → Bob tries Dublin→Belfast → REJECTED: "Region NI unavailable"
  → Bob tries Dublin→Galway  → CONFIRMED (IE unaffected)
  Demonstrates: partial failure / graceful degradation

Step D: Node recovery
  → POST conflict-ni:8007/api/simulate/recover
  → NI re-registers in Redis registry (visible in logs)
  → Bob tries Dublin→Belfast again → CONFIRMED
  Demonstrates: node recovery, automatic re-discovery

Step E: Network delay simulation
  → POST conflict-ie:8003/api/simulate/delay {"delay_ms": 2000}
  → Alice books Dublin→Galway → succeeds but takes ~2s
  → Analytics shows elevated booking_time_ms
  → POST conflict-ie:8003/api/simulate/recover
  Demonstrates: network delay impact + circuit breaker response

Step F: Concurrent cross-region booking (race condition demo)
  → Alice and Bob both try Dublin→Belfast at the exact same time
  → Phase 1 holds: one gets HELD, one gets 409 CONFLICT
  → Only one journey CONFIRMED
  Demonstrates: distributed concurrent access control
```

---

## 10. What We Are Not Doing (And Why)

| Prompt Feature | Decision | Reason |
|---|---|---|
| Randomly generated road graph on startup | Not doing — keep 6 predefined Irish routes | Random graphs are unpredictable in a demo. Predefined real roads on named motorways are credible, explainable, and verifiable by an evaluator |
| Terminal menu on each node | Not doing — using HTTP endpoints instead | HTTP endpoints are scriptable, demonstrable via curl/demo script, and work in Docker without interactive TTY |
| Running on separate physical machines | Not doing — Docker Compose network isolation | Docker bridge networks provide equivalent network separation for demonstrating all 7 problems. Multiple physical machines add logistical complexity with no evaluation benefit |
| Region count > 2 | Starting with IE + NI, can add more | Two regions is sufficient to demonstrate all distributed problems. Adding a 3rd (e.g., EU/France) is one env var change once the pattern is established |
| Full Dijkstra/A* pathfinding | Not doing | The piecewise-linear waypoint model already answers the professor's routing question. Full graph pathfinding adds complexity with no DS principle benefit |

---

## 11. Implementation Order

Each phase is independently testable before moving to the next.

### Phase 1 — Region-aware conflict service
- Add region env vars (`REGION_ID`, `REGION_NAME`, `REGION_OWNED_ROUTES`)
- Filter route seeding: only seed routes this region owns
- Add `GET /api/region/info` endpoint
- Add `registry.go`: Redis registration + heartbeat loop
- **Test:** Start conflict-service-ie, check it only seeds IE routes; check Redis key appears

### Phase 2 — Two regional instances in Docker Compose
- Rename existing conflict-service → conflict-service-ie
- Add conflict-service-ni with NI config
- Add postgres-conflicts-ni + replica
- Update journey-service `CONFLICT_SERVICE_URL` to point to IE for now
- **Test:** Both services start, each with their own DB, each showing correct region info

### Phase 3 — Hold / Commit / Rollback
- Add `held_bookings` table to each regional DB
- Implement `holdRoadSegments()`, `commitHold()`, `rollbackHold()` in service.go
- Add handlers and routes
- Add hold expiry background goroutine
- **Test:** Call hold on IE directly, verify hold appears; call commit, verify booking active; call rollback, verify capacity freed

### Phase 4 — Journey service cross-region saga
- Add `registry.py`: queries Redis for region ownership by route_id
- Modify `saga.py`: single-region → existing flow; multi-region → 2-phase saga
- **Test:** Dublin→Galway uses single-region flow; Dublin→Belfast triggers 2-phase visible in both service logs

### Phase 5 — Simulation endpoints
- Add `simulate.go`: state machine + middleware that applies delay/failure/partition
- Add handlers for all simulate endpoints
- **Test:** Trigger NI failure, confirm IE still works; trigger recovery, confirm NI responds

### Phase 6 — Demo script updates
- Add Steps A through F to demo_local.py
- **Test:** Full end-to-end demo run passes all steps

---

## 12. Professor's Concerns — Addressed

| Professor's Concern | How This Addresses It |
|---|---|
| *"System appears centralized"* | Two separate conflict service processes, two separate Postgres databases, two sets of road segment data — visibly decentralized |
| *"No provision for geographic partitioning"* | IE road cells exist only in IE DB. NI road cells exist only in NI DB. Grid cells are geographically assigned at region configuration time |
| *"Multi-national journeys — how is consistency addressed?"* | Dublin→Belfast runs a 2-phase distributed saga across IE and NI services. Both must hold before either commits. Rollback is automatic if either fails |
| *"How do the regions cooperate?"* | Redis registry as shared directory. Regions discover peers on startup. Journey service queries registry to route to correct region. Regions call each other directly for Phase 2 commit/rollback |
| *"How is the road map managed? Where does routing happen?"* | Each regional service owns and seeds its own route waypoints. `GET /api/region/info` returns the region's full road graph. The journey service queries the registry to find which region handles a given route |
| *"What if one system goes down?"* | Demonstrable live: NI failure → cross-border fails, IE-only works. Recovery → cross-border resumes. All observable in the demo script |
| *"Double booking across regions?"* | Phase 1 holds act as distributed locks. Two concurrent Phase 1 requests to the same region for the same road cells will see `SERIALIZABLE` + `SELECT FOR UPDATE` — only one succeeds |

---

*Plan authored: 2026-04-08. Awaiting implementation confirmation.*
