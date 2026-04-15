# Middleware Decisions & Motivation
**CS7NS6 — Journey Pre-Booking System | Group J**

---

## Overview

This document explains every middleware component used in the system, why it was chosen over the alternatives, and what trade-offs were accepted. Each decision is motivated by the system's specific requirements: sub-second enforcement lookups, atomic conflict detection, at-least-once event delivery, and graceful degradation during node failures.

---

## 1. RabbitMQ — Async Message Broker

### What it does
All inter-service events (journey confirmed/rejected/cancelled, user registered) are published to a RabbitMQ **topic exchange** (`journey_events`). Services subscribe using routing keys (e.g. `journey.confirmed`, `journey.cancelled`). This decouples producers from consumers — the journey service does not know or care which services consume its events.

### Why RabbitMQ over the alternatives

| Alternative | Why not chosen |
|---|---|
| **Kafka** | Kafka is optimised for high-throughput log streaming (millions of events/sec). Our peak load is ~300 events/sec — Kafka would be significant operational overhead (ZooKeeper or KRaft cluster, topic partition management, offset management) for no throughput benefit. Kafka also has no concept of per-message acknowledgement with DLQ routing; dead-letter handling requires custom consumer logic. |
| **Direct HTTP calls (no broker)** | Synchronous service-to-service calls couple availability — if the notification service is down when a booking is confirmed, the notification is lost. A broker decouples this: the journey service publishes once, the notification service consumes when it recovers. |
| **Redis Pub/Sub** | Redis Pub/Sub is fire-and-forget — if a subscriber is offline when a message is published, the message is lost. It provides no delivery guarantees, no DLQ, and no persistence. Unsuitable for a system that requires at-least-once delivery of booking events. |
| **AWS SQS / Google Pub/Sub** | Managed cloud services require an internet connection and incur cost. The system must run on a local laptop for the demo. RabbitMQ runs identically in Docker. |

### Specific features used

- **Topic exchange**: routing keys allow fine-grained subscription. The enforcement service only subscribes to `journey.cancelled` and `journey.completed` (for cache invalidation), not to `journey.confirmed` (no cache action needed).
- **Durable queues + persistent messages**: messages survive a RabbitMQ restart. Combined with the transactional outbox in the journey service, no event is ever lost.
- **Manual acknowledgement**: consumers explicitly `ack` after successful processing, or `nack` on failure. Unacknowledged messages are requeued and redelivered.
- **Dead-letter queue (DLQ)**: messages that fail processing 3 times are routed to `dead_letter_queue` with a 24h TTL. This prevents a poison message from blocking the queue indefinitely.
- **Consumer deduplication**: because RabbitMQ guarantees at-least-once delivery (not exactly-once), all four consumers implement idempotency via Redis `SETNX` on the `MessageId` header before processing.

### Sizing rationale

- Single node selected for the demo (3-node cluster configured but Erlang distribution is unreliable on a single Docker host with shared network namespaces).
- Single-node RabbitMQ fully demonstrates all messaging principles: routing, persistence, DLQ, consumer acknowledgement.
- 3-node cluster would add mirrored queue HA — appropriate for production but not required for the exercise demo.

---

## 2. Redis — Cache & Sentinel HA

### What it does
Redis serves three distinct roles in the system:

1. **Enforcement cache**: active journeys cached by vehicle registration and user ID (24h TTL). Sub-millisecond lookup for roadside verification.
2. **Notification history**: per-user notification list (7-day TTL, max 50 entries per user).
3. **Consumer deduplication**: `SETNX` on `MessageId` with 24h TTL prevents duplicate event processing across all four consumers.
4. **Idempotency keys**: journey-service stores idempotency key → response mappings (1h TTL) to handle duplicate booking requests from clients.
5. **Active journey index**: enforcement and journey services maintain `active_journey:vehicle:{plate}` keys for O(1) lookup.

Redis Sentinel (3 sentinel nodes) provides automatic failover: if the Redis primary fails, sentinels vote (majority of 3 = 2) and promote the replica.

### Why Redis over the alternatives

| Alternative | Why not chosen |
|---|---|
| **Memcached** | Memcached is pure cache — no persistence, no replication, no Pub/Sub, no Lua scripting, no Sentinel. It cannot serve the deduplication role (no atomic `SETNX`) or the notification history role (no list data structure with TTL). Redis does everything Memcached does and more. |
| **In-process cache (dict/map)** | Lost on service restart. Cannot be shared across multiple instances of the same service. Enforcement cache populated by the journey service must be readable by the enforcement service — they are separate processes. |
| **Database-backed cache (PostgreSQL)** | DB reads are 10–100× slower than Redis for cache lookups. Enforcement lookup must be < 5ms (cache hit target) — Postgres cannot reliably achieve this under load. |
| **Hazelcast / Infinispan** | Distributed Java caches with complex clustering. Overkill for our use case; adds JVM dependency. |

### Cache design decisions

**LRU eviction (`allkeys-lru`)**: chosen over `volatile-lru` (only evict keys with TTLs) because all keys in Redis are operationally equivalent — the oldest-accessed entries are the least likely to be needed. `volatile-lru` would fail to evict keys without TTLs (e.g. dedup keys) and could OOM.

**256mb maxmemory**: derived from capacity planning — 200,000 active journey entries × ~1KB average value = ~200MB, with 56MB headroom for dedup keys and notification lists. If this limit is reached, LRU eviction removes the least recently used cache entries, which is acceptable (they will be reconstructed from Postgres on the next cache miss).

**AOF persistence (`appendonly yes`)**: every write is appended to the AOF log. On restart, Redis replays the log. This means enforcement cache survives a Redis restart — no cold-start penalty.

**Sentinel quorum of 2 (3 sentinels, majority = 2)**: a single sentinel is a SPOF for failover decisions. Two sentinels cannot form a majority if they disagree. Three sentinels with a majority threshold of 2 means: one sentinel can fail completely, the remaining two still agree and can promote the replica. This is the minimum viable HA configuration.

---

## 3. PostgreSQL — Persistent Relational Storage

### What it does
Each of the four stateful services (user, journey, conflict, analytics) has its own independent PostgreSQL database. No service shares a database. Each database runs with a primary instance and one WAL streaming replica.

### Why PostgreSQL over the alternatives

| Alternative | Why not chosen |
|---|---|
| **MySQL / MariaDB** | PostgreSQL has superior support for `SERIALIZABLE` isolation (used in conflict-service), `SELECT FOR UPDATE SKIP LOCKED` (used in the outbox drain), and advisory locks. MySQL's SERIALIZABLE uses shared locks that cause significant contention. |
| **MongoDB** | Document stores do not provide ACID transactions across multiple documents without multi-document transactions (added in v4.0, with significant performance overhead). The points ledger requires atomic `SELECT FOR UPDATE` which is idiomatic in SQL but complex to replicate in MongoDB. |
| **SQLite** | File-based, single-writer, no network access. Cannot be shared between service instances or accessed from a replica. Not suitable for a replicated, multi-instance deployment. |
| **CockroachDB / Spanner** | Distributed SQL with automatic sharding and multi-region replication. Significant operational complexity. The geographic grid partitioning implemented in the conflict-service achieves the same data locality property with standard Postgres. |

### Replication setup

**WAL streaming replication**: the primary writes every change to its Write-Ahead Log. The replica streams this log and applies it continuously, maintaining a near-real-time copy. Lag is typically < 100ms on the same host; < 1s on a LAN.

```
postgres -c wal_level=replica          # Enable WAL streaming
         -c max_wal_senders=3          # Up to 3 replicas per primary
         -c max_replication_slots=3    # Persistent slots survive replica reconnect
         -c hot_standby=on             # Replica accepts read-only queries
```

**Read/write split**: services route write operations (INSERT, UPDATE, DELETE) to `DATABASE_URL` (primary) and read-heavy operations (list queries, dashboard queries, enforcement lookups) to `DATABASE_READ_URL` (replica). This doubles effective read throughput without adding primary load.

**Why not auto-failover for Postgres**: automatic primary promotion (Patroni, repmgr) was not implemented. If the primary fails, the replica is promoted manually using the documented runbook. This is an accepted limitation for the exercise scope — Redis uses Sentinel for automatic failover because it is the more frequently accessed and more operationally critical component (enforcement lookups hit Redis on every request).

---

## 4. nginx — API Gateway & Rate Limiting

### What it does
Two nginx instances sit behind HAProxy. Each nginx instance terminates HTTP connections from the frontend and the enforcement API, applies rate limiting, and proxies requests to the appropriate backend service.

### Rate limiting zones

```nginx
limit_req_zone $binary_remote_addr zone=auth:10m    rate=5r/s;   # Login/register
limit_req_zone $binary_remote_addr zone=booking:10m rate=10r/s;  # Journey bookings
limit_req_zone $binary_remote_addr zone=general:10m rate=60r/s;  # All other endpoints
```

**Motivation:** The `auth` zone (5 req/s per IP) prevents brute-force password attacks. The `booking` zone (10 req/s per IP) prevents a single client from monopolising the saga pipeline during peak load. The `general` zone (60 req/s per IP) allows fast polling (enforcement checks, dashboard refreshes) without being overly restrictive.

### Why nginx over the alternatives

| Alternative | Why not chosen |
|---|---|
| **Traefik** | Automatic service discovery via Docker labels is convenient but adds non-determinism — routing configuration is distributed across every service's compose labels. nginx's centralised config file is easier to audit and reason about. |
| **Caddy** | No stable rate limiting module in open source version without plugins. |
| **HAProxy as sole gateway** | HAProxy operates at L4 (TCP) and L7 (HTTP) but does not support request body parsing or URL-path-based rate limiting zones by client IP as cleanly as nginx. HAProxy is used for TCP load balancing between the two nginx instances — each layer does what it does best. |
| **Kong / AWS API Gateway** | Managed API gateways. Not suitable for local Docker deployment. |

---

## 5. HAProxy — Load Balancer

### What it does
HAProxy receives all external HTTP traffic on port 8080 and round-robins it across the two nginx instances. It performs health checks every 2 seconds (`check inter 2s`); if a nginx instance fails, HAProxy removes it from rotation immediately.

### Why HAProxy

HAProxy is the industry standard for high-performance L4/L7 load balancing. It is lightweight (< 50MB container), has a well-understood configuration model, and supports health-check-based removal without manual intervention. In the Swarm deployment, HAProxy's VIP (Virtual IP) serves as the stable entry point regardless of which Swarm node is running nginx.

**Alternative — nginx upstream**: nginx can load-balance to upstream backends. Using nginx for both load balancing and gateway logic in the same process means a nginx bug or misconfiguration takes down both layers simultaneously. HAProxy + nginx gives separate failure domains.

---

## 6. Custom Circuit Breaker (`shared/circuit_breaker.py`)

### What it does
Wraps the journey-service's synchronous HTTP call to the conflict-service. Tracks consecutive failures; after 3 failures the circuit opens and subsequent calls fail immediately (returning `CircuitOpenError`) without waiting for a timeout.

```
CLOSED (normal) ──[3 consecutive failures]──▶ OPEN (fast-fail)
                                                    │
                                              [30s timeout]
                                                    │
                                                    ▼
                                             HALF-OPEN (one probe)
                                             ──[success]──▶ CLOSED
                                             ──[failure]──▶ OPEN
```

### Why a custom implementation

Libraries exist (e.g. `pybreaker`, `tenacity`). A custom implementation was chosen because:
1. It integrates directly with the `PartitionManager` state — the circuit breaker open/close events are reported on the `/health/partitions` endpoint.
2. The threshold values (3 failures, 30s timeout) are hard-coded to match the saga timeout (5s) and the expected conflict-service restart time. A library would require configuration adapters.
3. The implementation is ~80 lines and transparent — the behaviour is auditable in code review.

**Why a circuit breaker at all**: without it, a conflict-service outage causes the journey service to accumulate threads waiting for a 5s timeout on every booking request. Under load (112 bookings/sec), this exhausts the asyncpg connection pool in ~2 seconds, taking down the journey service itself. The circuit breaker prevents this cascade.

---

## 7. Custom Partition Manager (`shared/partition.py`)

### What it does
Each service runs a background `PartitionManager` that probes its dependencies (Postgres, RabbitMQ, peer services) every 5 seconds. It maintains a state machine per dependency:

```
CONNECTED ──[1 failure]──▶ SUSPECTED ──[3 consecutive failures]──▶ PARTITIONED
                                                                         │
                                                                   queue writes
                                                                         │
CONNECTED ◀──[successful probe]──────────────────────────── MERGING
                                                             (replay queued writes)
```

During `PARTITIONED` state, responses include the header `X-Partition-Status: partitioned` and `X-Data-Staleness: <seconds>` so clients know responses may be stale.

### Why 5-second probe interval

- Too short (< 1s): false positives during GC pauses or transient network congestion
- Too long (> 30s): a real partition is not detected until 30+ seconds have passed, during which the service continues attempting writes to an unreachable dependency
- **5s** was chosen as a balance: three consecutive failures (15s total) before declaring `PARTITIONED` avoids false positives while detecting real failures within 15 seconds

### Why 1,000-operation queue limit

The queue is bounded to prevent unbounded memory growth during a long partition. 1,000 operations at ~500 bytes each = ~500KB, negligible memory overhead. If the queue fills, new operations are rejected rather than accepted silently (fail-fast). For a partition lasting longer than the queue can absorb, manual intervention is required — this is documented in the runbook.

---

## 8. Middleware Architecture Summary

```
Internet / LAN
      │
   HAProxy :8080
   (L7 load balancer, health-checked)
      │
   ┌──┴──┐
nginx-1  nginx-2
(rate limiting, routing)
      │
┌─────┼────────────────────────────────────┐
│     │                                    │
user-svc  journey-svc  conflict-svc  notification-svc  enforcement-svc  analytics-svc
│         │            │             │                  │                │
│         │            │             └──────────────────┴────────────────┘
│         │            │                                |
│    asyncpg     pgx (Go)                           go-redis
│         │            │                                |
PG-users  PG-journeys  PG-conflicts  PG-analytics    Redis
(+replica)(+replica)   (+replica)    (+replica)      (+replica)
                                                     Sentinel ×3
│         │
└─────────┴── RabbitMQ (topic exchange: journey_events)
              └── notification-svc consumer
              └── enforcement-svc consumer
              └── analytics-svc consumer
              └── conflict-svc consumer (journey.cancelled)
```

| Component | Role | Key property |
|---|---|---|
| HAProxy | External load balancer | L7 health checks, VIP stability |
| nginx (×2) | API gateway | Rate limiting, path routing |
| RabbitMQ | Async event bus | At-least-once, DLQ, durable |
| Redis + Sentinel | Cache + HA | LRU eviction, auto-failover |
| PostgreSQL (×4) + replicas | Primary storage | ACID, WAL replication, read scale |
| Circuit breaker | Cascading failure prevention | Fast-fail, state machine |
| Partition manager | Degraded-mode operation | Probing, write queue, merge |

---

*CS7NS6 Distributed Systems — Exercise 2 | Group J | TCD 2025–2026*
