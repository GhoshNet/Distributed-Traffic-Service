# Group J — Demo Guide
## CS7NS6 Distributed Systems — Exercise 2

---

## What You Built — The Big Picture

**Problem:** A nationally-scaled road-journey booking system. Drivers must pre-book journeys. No booking = no driving. Enforcement agents roadside-check in real-time. At peak: ~167 bookings/second nationally.

**Core challenge:** How do you make this reliable when nodes crash, networks partition, and the same road segment gets double-booked from two laptops simultaneously?

---

## Architecture Flowchart

```
CLIENT (Browser)
    │
    │  HTTP / WebSocket
    ▼
┌─────────────────────────────────────────────────┐
│  ENTRY LAYER                                    │
│  HAProxy :8080  ──── load balances ────────┐   │
│  nginx GW-1 :80       nginx GW-2 :80 ◄─────┘   │
│  (rate limit: auth 5r/s, booking 10r/s)         │
└─────────────────────────────────────────────────┘
    │
    │  Route by URL prefix (/api/users, /api/journeys, etc.)
    ▼
┌────────────┐    ┌─────────────┐    ┌──────────────┐
│User Service│    │Journey Svc  │───►│Conflict Svc  │
│ Python 8001│    │ Python 8002 │    │ Go 8003      │
│ users_db   │    │ journeys_db │    │ conflicts_db │
│ + replica  │    │ + replica   │    │ + replica    │
└────────────┘    └──────┬──────┘    └──────────────┘
                         │ outbox
                         ▼
                   ┌───────────┐
                   │ RabbitMQ  │  topic exchange: journey_events
                   │ cluster×3 │
                   └─────┬─────┘
          ┌──────────────┼───────────────┐
          ▼              ▼               ▼
   ┌────────────┐ ┌────────────┐ ┌──────────────┐
   │Notification│ │Enforcement │ │  Analytics   │
   │  Go 8004   │ │ Python 8005│ │   Go 8006    │
   │  Redis     │ │  Redis     │ │  analytics_db│
   └────────────┘ └────────────┘ └──────────────┘
          │
          ▼
    WebSocket push to browser


MULTI-NODE (each laptop runs this entire stack)
Laptop A ◄──── HTTP peer calls ────► Laptop B
  └─ PEER_CONFLICT_URLS               └─ PEER_CONFLICT_URLS
  └─ PEER_USER_URLS                   └─ PEER_USER_URLS

Browser resilientFetch: tries Laptop A → if 5xx → tries Laptop B
```

---

## Why Each Component Was Chosen

| Component | Why |
|---|---|
| **Microservices** | Each service has different scaling needs. Enforcement is read-heavy, Journey is write-heavy. Separate databases means one service's load can't starve another. |
| **HAProxy + nginx** | HAProxy gives a single external port and load-balances between 2 nginx instances. nginx does rate-limiting (auth 5r/s, booking 10r/s). Decouples the frontend from knowing about individual services. |
| **RabbitMQ (async fan-out)** | After a booking is CONFIRMED, 3 services need to know: Notification (push alert), Enforcement (cache update), Analytics (counters). Calling all 3 synchronously would slow down the booking response. RabbitMQ lets them consume at their own pace. |
| **Transactional Outbox** | The dual-write problem: if you write to DB then crash before publishing to RabbitMQ, the event is lost forever. By writing the event into the same DB transaction as the booking row, you guarantee either both happen or neither does. A background thread drains it. |
| **Redis Sentinel** | Enforcement needs <20ms lookups. Postgres would be ~50-100ms. Redis gives sub-millisecond cache hits. Sentinel (3 instances, quorum 2) means if the Redis primary dies, a replica is promoted automatically within ~15 seconds. |
| **PostgreSQL WAL Replication** | Each service has a primary (writes) and replica (reads). Read/write separation offloads read traffic. WAL streaming keeps the replica near-synchronous. |
| **SERIALIZABLE + SELECT FOR UPDATE** | The conflict service must guarantee no double-booking. "Eventually consistent" isn't enough here. SERIALIZABLE isolation means two concurrent bookings for the same slot can't both pass — the second sees the first's lock and waits or retries. |
| **Circuit Breaker** | If the conflict service is slow/dead, without a circuit breaker every booking would hang for 30 seconds on a timeout. After 3 failures, the breaker opens and bookings fail immediately (<1ms), protecting the system from cascading failure. |
| **Client-side failover (resilientFetch)** | HAProxy only load-balances within one laptop's stack. Cross-laptop failover can't go through a single point — that point would itself be a failure. The browser handles it: if Laptop A returns 5xx, transparently retry Laptop B. |

---

## Distributed Systems Principles Applied

### 1. CAP Theorem Trade-off
Chose **AP over CP** for cross-node booking. Within one node it's CP (SERIALIZABLE — strong consistency). Across nodes it's AP (eventually consistent slot replication — two laptops can briefly both accept the same booking during the replication window). Documented as a known trade-off.

### 2. The Saga Pattern
A booking spans two services (Journey + Conflict). Rather than a distributed transaction that would require both to be up simultaneously, the saga calls Conflict synchronously for the check-and-reserve, then commits locally. If Conflict is down, the saga rejects cleanly. No orphaned state.

### 3. Eventual Consistency via Outbox
The booking saga guarantees strong local consistency. Cross-service propagation (notification, enforcement cache, analytics) is eventually consistent via RabbitMQ. Consumers are idempotent so redeliveries are safe.

### 4. Failure Detection
```
ALIVE → (3 missed pings) → SUSPECT → (6 missed pings) → DEAD
                                ↑________________ping OK___|
```
Quorum: if <50% peers alive → LOCAL_ONLY mode. Simplified version of the φ-accrual failure detector.

### 5. Active-Active Replication (no master)
Every laptop runs the full stack and can accept bookings. There's no primary node — any node can serve any request. Conflict slots are replicated peer-to-peer. Users are replicated with Redlock-style distributed locking to prevent split-brain email registration.

### 6. Idempotency
Every booking has an `Idempotency-Key`. Clients can safely retry on network failure — the second call returns the first response without re-running the saga. RabbitMQ consumers deduplicate via Redis SETNX on message IDs.

---

## Demo Flow — Best Order for the Professor

### Phase 1: Orient (2 min)
> "We built a nationally-scaled road-journey booking system. Drivers must pre-book or they can't drive. Enforcement agents verify roadside in real-time. The challenge is making this reliable across multiple nodes — if one laptop dies, sessions stay active and bookings still work."

Open `http://localhost:3000` and show the architecture diagram. Point to HAProxy → nginx → 6 microservices.

---

### Phase 2: Core Happy Path (3 min)

1. Register a driver account → show JWT returned
2. Register a vehicle
3. Book a journey → show **CONFIRMED** response ~250ms
4. Explain: "The booking just did: idempotency check → insert PENDING → call Conflict Service (SERIALIZABLE lock) → CONFIRMED + outbox event written — all in one transaction"
5. Open WebSocket tab → book again → show real-time notification pushed to browser

---

### Phase 3: Conflict Detection (2 min)

1. Book the same slot (same route, same time window) again → show **REJECTED**
2. Explain: "The Conflict Service uses a geographic grid — 1km cells, 30-min slots. Two bookings on the same cell in the same window can't both pass. SERIALIZABLE + SELECT FOR UPDATE."
3. Run 10 concurrent bookings for the same slot → exactly 1 CONFIRMED, 9 REJECTED

---

### Phase 4: Fault Tolerance — Node Kill (3 min)

```bash
# Kill this node
curl -X POST http://localhost:8080/admin/simulate/fail
```

1. Show all routes now return 503
2. Show browser topbar switches to "Failover: \<Peer IP\>" automatically
3. Book a journey — it succeeds (routed to the peer node)
4. Show the user is still logged in — no re-login needed (shared JWT secret across nodes)

```bash
# Recover
curl -X POST http://localhost:8080/admin/simulate/recover
```

---

### Phase 5: Distributed Patterns (pick 2–3, ~2 min each)

**Circuit Breaker:**
```bash
docker service scale traffic-service_conflict-service=0
# Try to book — fails fast after 3 attempts, circuit opens
docker service scale traffic-service_conflict-service=2
# Circuit half-opens on next probe, closes, bookings resume
```

**Outbox Durability:**
```bash
docker service scale traffic-service_rabbitmq=0
# Book a journey — returns CONFIRMED (stored in outbox)
docker service scale traffic-service_rabbitmq=1
# Check analytics — event appears after RabbitMQ reconnects
```

**Enforcement Sub-second Lookup:**
```bash
curl http://localhost:8080/api/enforcement/verify/vehicle/PLATE123
# First call: cache miss → REST fallback ~80ms
# Second call: cache hit → <10ms
# Show the latency difference
```

---

### Phase 6: Observability (1 min)

```bash
curl http://localhost:8080/health                           # node health
curl http://localhost:8080/health/nodes                     # peer ALIVE/SUSPECT/DEAD
curl http://localhost:8080/health/partitions                # X-Partition-Status
curl http://localhost:8080/api/analytics/replica-lag        # live WAL replication lag
curl http://localhost:8080/api/analytics/health/services    # all 6 services at once
```

---

## Q&A — Likely Professor Questions

**Q: Why not use a single distributed database like CockroachDB instead of per-service databases?**

Database-per-service is a core microservices principle. Each service owns its schema and can evolve independently. CockroachDB would create a single coupling point — a schema change in the conflict service would require coordinating with all other services. Also, different services have different storage needs: Notification uses Redis (in-memory, TTL-based), Enforcement needs sub-millisecond cache, Analytics needs an append-only event log with rollups.

---

**Q: You said SERIALIZABLE isolation — doesn't that kill performance at scale?**

We only use SERIALIZABLE in the Conflict Service, specifically for the check-and-reserve operation. All other services use READ COMMITTED (the Postgres default). Conflict checks are short transactions (~5ms), and the lock scope is bounded to specific grid cells and time slots, so two bookings on different road segments never contend. At 50 bookings/second per node, the bottleneck would be network, not transaction serialization.

---

**Q: How do you handle the case where Laptop A accepts a booking and Laptop B hasn't received the replication yet, and Laptop B accepts the same slot?**

We document this as a known trade-off. After a slot is confirmed on Node A, it's replicated to Node B asynchronously (<200ms on LAN). During that window, a concurrent booking on Node B for the same slot can pass. This is the classic AP trade-off. For a fully CP system you'd need Paxos/Raft consensus on every booking, which would add latency and require a majority quorum — we decided that was out of scope for this demo scale.

---

**Q: Why not use Kubernetes instead of Docker Swarm?**

Docker Swarm was chosen for simplicity on demo hardware (student laptops). Swarm lets us deploy using a docker-compose-style file with minimal changes. Kubernetes would add significant operational overhead (etcd, control plane, CNI plugins) for a 4-node demo. The core distributed systems principles demonstrated are the same either way.

---

**Q: What happens to in-flight bookings when a node crashes?**

Three things: (1) The journey row is already committed to Postgres before the crash, so the booking isn't lost. (2) If the outbox event wasn't published yet, the background drainer picks it up on restart. (3) If the client is mid-request, `resilientFetch` retries on a peer node — because bookings are idempotent via `Idempotency-Key`, the retry won't create a duplicate.

---

**Q: Why does the browser hold the peer list instead of a load balancer?**

Because the load balancer itself would be a single point of failure. HAProxy load-balances between two nginx instances on the same laptop — it can't route to a different laptop without itself being redundant. By keeping the peer list in `localStorage`, the browser can fail over even if the entire Laptop A stack (including HAProxy) is down. This is the same principle used by Cassandra's client drivers — client-side load balancing for true multi-node resilience.

---

**Q: Why three Redis Sentinel instances?**

Sentinel uses a majority quorum to elect a new master. With 3 sentinels, you need 2 to agree (quorum = 2). This means you can lose one sentinel and still promote a new Redis primary. With 2 sentinels, losing one means no quorum — you'd be stuck with a dead primary and no promotion.

---

**Q: What does "transactional outbox" solve that a normal publish/subscribe doesn't?**

The dual-write problem. If you write to DB and then publish to RabbitMQ as two separate operations, you can crash between them. Either the DB write succeeds but the event is never published (downstream services never hear about the booking), or the event is published but the DB write fails (downstream services think there's a booking that doesn't exist). The outbox writes the event into the same DB transaction as the booking row — you get atomicity from Postgres for free, and a background thread handles the RabbitMQ publish with unlimited retries until it succeeds.

---

**Q: What is the 2PC mode and how is it different from the saga?**

In the default saga mode, the Conflict Service only creates a slot record when the booking is fully committed — so there's nothing to roll back on failure. In 2PC mode (`?mode=2pc`), the PREPARE phase tentatively reserves a slot before the journey row is committed. If anything fails between PREPARE and COMMIT, an explicit ABORT is sent to release the slot. The key difference: saga has no compensation needed on failure; 2PC requires an explicit rollback. 2PC is safer against phantom slots but adds a round-trip and requires the same peer URL to handle both PREPARE and COMMIT.

---

**Q: How does consistent-hash sharding work in your system?**

Both the User Service and Conflict Service assign write authority via `shard = MD5(key) % num_nodes`. For users the key is the email address; for routes it's the route ID. The node where `shard == 0` is the PRIMARY writer for that key. All nodes still store all data (active-active replication) — sharding only governs write authority, not data isolation. This means reads can be served locally from any node for low latency, while writes are coordinated through the shard-0 node to prevent split-brain.

---

## Known Limitations (be upfront about these)

| Gap | What it means |
|---|---|
| No consensus protocol | Two simultaneous bookings on different nodes can both pass within the ~200ms replication window |
| No saga compensation retry | If the conflict service permanently fails mid-booking, the booking is rejected with no auto-retry |
| Enforcement cache cold on boot | First enforcement check after restart is always a cache miss |
| No token blacklisting | Logged-out JWTs remain valid until natural expiry |
| RabbitMQ cluster unreliable on single host | Erlang distribution is unstable when all 3 nodes share one Docker host; single-node RabbitMQ is stable |
| No distributed tracing UI | Correlation IDs (`X-Request-ID`) propagate across all services, but no Jaeger/Zipkin view |
