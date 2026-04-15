# System Requirements Specification
**CS7NS6 — Journey Pre-Booking System | Group J**

---

## 1. Problem Context & Load Model

The system pre-books road journeys for vehicle drivers before they travel. No driver may start a journey without a confirmed booking. The system must handle real-time conflict detection, enforcement verification, and event-driven notifications across six independent services.

### 1.1 Load Pattern Assumptions

The Irish road network serves approximately **2.2 million licensed drivers** (RSA 2023). Peak usage follows commute patterns:

| Period | Journeys/hour | Booking requests/sec | Concurrent users |
|---|---|---|---|
| Off-peak (midnight–6am) | ~5,000 | ~1–2 | ~200 |
| Daytime (9am–4pm) | ~80,000 | ~22 | ~3,000 |
| **Peak (7–9am, 4–6pm)** | **~200,000** | **~56** | **~8,000** |
| Enforcement spot-checks | — | ~5 | ~50 agents |

These figures inform the infrastructure sizing decisions throughout this document. The system is designed to handle **2× peak load** (112 bookings/sec) to accommodate traffic spikes (sporting events, school terms, bank holidays).

---

## 2. Performance Requirements

### 2.1 Latency Targets

| Operation | p50 | p95 | p99 | Hard Limit |
|---|---|---|---|---|
| Journey booking (end-to-end saga) | < 150ms | < 400ms | < 800ms | 5s (saga timeout) |
| Conflict check (conflict-service only) | < 30ms | < 80ms | < 150ms | 500ms |
| Enforcement vehicle lookup (cache hit) | < 5ms | < 20ms | < 50ms | 200ms |
| Enforcement vehicle lookup (cache miss) | < 80ms | < 200ms | < 400ms | 1s |
| User login / token issue | < 50ms | < 120ms | < 250ms | 2s |
| Analytics event ingest | < 10ms | < 40ms | < 100ms | 1s |
| WebSocket notification delivery | < 100ms | < 300ms | < 600ms | 3s |

**Motivation:** Enforcement officers perform roadside checks against live traffic. A 1s hard limit ensures the officer's device does not appear to freeze. The booking saga timeout of 5s is derived from the UX requirement that drivers see immediate confirmation; beyond 5s the driver experience degrades to an unacceptable level (derived from Google's research showing >3s causes 53% abandonment).

### 2.2 Throughput Targets

| Service | Target RPS | Burst capacity |
|---|---|---|
| API Gateway (HAProxy + nginx) | 500 RPS | 1,000 RPS (30s) |
| Journey bookings | 112 RPS | 200 RPS |
| Conflict checks | 200 RPS | 400 RPS (conflict-service called for every booking plus cancellations) |
| Enforcement lookups | 20 RPS | 50 RPS |
| Analytics event ingest (async) | 300 events/sec | 1,000 events/sec |

**Motivation:** At peak, 56 bookings/sec arrive. Each booking triggers one conflict check, and ~30% of bookings also result in a cancellation eventually, giving ~200 conflict-service calls/sec. The 2× burst figures account for load test spikes and retry storms during node recovery.

---

## 3. Scalability Requirements

### 3.1 Data Volume Projections

| Dataset | Current (demo) | 6-month projection | 2-year projection |
|---|---|---|---|
| Registered users | ~100 | ~500,000 | ~2,000,000 |
| Journeys (total) | ~1,000 | ~50,000,000 | ~500,000,000 |
| Active booked slots (conflict DB) | ~50 | ~2,000,000 | ~20,000,000 |
| Analytics events | ~5,000 | ~300,000,000 | ~3,000,000,000 |
| Redis cache entries | ~200 | ~200,000 | ~1,000,000 |

### 3.2 Horizontal Scaling Strategy

Each service is **stateless** — session state lives in JWT tokens (signed with a shared secret) and Redis, not in service memory. This means any service can be replicated to N instances by changing a single environment variable, with zero code changes.

| Service | Scale-out mechanism | Bottleneck |
|---|---|---|
| User, Journey, Enforcement | Replicate containers; HAProxy round-robins | DB connection pool (20 per instance) |
| Conflict service | Replicate; road capacity data is geographically partitioned by grid cell | SERIALIZABLE transaction contention on hot cells |
| Notification service | Replicate; WebSocket registry is per-instance (known limitation) | Redis connection pool |
| Analytics service | Replicate; dual-write Postgres + Redis; hourly rollup uses read replica | Rollup job — must be leader-elected (not yet implemented) |

### 3.3 Geographic Partitioning

Road capacity data is stored with a **1km geographic grid** (`gridResolution = 0.01` degrees ≈ 1.1km at 53°N). Each `(grid_lat, grid_lng, time_slot)` tuple is an independent row. In a multi-region deployment:

- EU traffic (Ireland, France, Germany) writes to EU-WEST conflict-service and Postgres
- APAC traffic writes to APAC conflict-service and Postgres
- Grid cell rows never overlap across regions — no cross-region locking

This means the conflict-service scales horizontally by geography with no shared state between regions.

---

## 4. Availability Requirements

### 4.1 Uptime Targets

| Service tier | Target uptime | Allowed downtime/month |
|---|---|---|
| Journey booking (user-facing critical path) | **99.9%** | 43 minutes |
| Enforcement lookup (roadside critical) | **99.95%** | 21 minutes |
| Analytics, notifications | **99.5%** | 3.6 hours |

### 4.2 Recovery Targets

| Metric | Target |
|---|---|
| RTO (Recovery Time Objective) — Redis failure | < 10s (Sentinel auto-promotes replica) |
| RTO — single service container crash | < 5s (Docker restart policy: `unless-stopped`) |
| RTO — Postgres primary failure | < 30s (manual promotion; auto-promotion not yet wired) |
| RPO (Recovery Point Objective) — Redis | 0 (replica is synchronous via AOF) |
| RPO — Postgres | < 1s (WAL streaming lag; configurable via `synchronous_commit`) |
| RPO — RabbitMQ messages | 0 (persistent messages, durable queues, mirrored cluster) |

### 4.3 Redundancy

| Component | Redundancy | Failover |
|---|---|---|
| Postgres (×4 databases) | Primary + WAL streaming replica | Manual (documented in runbook) |
| Redis | Primary + replica + 3-node Sentinel quorum | Automatic (Sentinel: 2-of-3 majority) |
| RabbitMQ | 3-node cluster, durable queues | Automatic (consumers reconnect on broker change) |
| nginx | 2 instances behind HAProxy | Automatic (HAProxy health checks every 2s) |
| Service containers | `restart: unless-stopped` | Automatic (Docker daemon) |

---

## 5. Reliability Requirements

### 5.1 Message Delivery

| Guarantee | Mechanism | Where |
|---|---|---|
| At-least-once delivery | Transactional outbox + RabbitMQ `manual_ack` | Journey → all consumers |
| Exactly-once processing | Idempotency keys via Redis `SETNX` on `MessageId` | All four consumers |
| No silent event loss | Outbox table persists events even if RabbitMQ is down | Journey service |
| Dead-letter queue | 24h TTL; unprocessable messages routed to `dead_letter_queue` | All consumers |

### 5.2 Transaction Reliability

| Scenario | Behaviour |
|---|---|
| Conflict-service unreachable during booking | Circuit breaker opens after 3 failures; booking immediately rejected (fail-safe) |
| Saga timeout (conflict check > 5s) | Journey set to REJECTED; client receives 503 |
| DB write fails after conflict check passes | Saga rolls back; conflict slot released on next `journey.cancelled` event |
| Duplicate booking request (same idempotency key) | Returns cached result; no second saga run |
| Points ledger concurrent update | `SELECT FOR UPDATE` serialises access; no negative balance possible |

### 5.3 Failure Model

The system assumes a **crash-recovery fault model** — nodes fail by stopping (not by sending incorrect data). Byzantine faults (nodes sending malicious or corrupted data) are out of scope. This is appropriate for a trusted-operator deployment (Irish road authority) where all nodes are under administrative control.

---

## 6. Data Consistency Requirements

### 6.1 Consistency Model per Service

| Data domain | Model | Justification |
|---|---|---|
| Road slot availability (conflict DB) | **Strong** — SERIALIZABLE isolation | Two drivers must never both receive CONFIRMED for the same slot. Serializability is the strongest SQL isolation level; conflicts are detected atomically. |
| Points ledger (journey DB) | **Strong** — `SELECT FOR UPDATE` + READ COMMITTED | Points are money-equivalent; double-spend is not acceptable. Pessimistic locking prevents concurrent deductions from racing. |
| User authentication (user DB) | **Strong** per write; **Read-your-writes** | Login must always reflect the latest password/vehicle changes. Reads route to the replica for scale, but write responses include the written value to avoid stale reads. |
| Enforcement cache | **Eventual** — seconds of lag acceptable | A roadside check that shows a journey confirmed 2 seconds ago is operationally acceptable. Cache TTL is 24h; cache invalidation is event-driven (`journey.cancelled`, `journey.completed`). |
| Analytics events | **Eventual** — minutes of lag acceptable | Dashboard statistics are used for reporting, not real-time decision-making. |
| Cross-service saga outcome | **Eventual** with bounded window | After a booking is confirmed in journey-service, the conflict-service, enforcement-service, and analytics-service are updated via RabbitMQ within seconds. |

### 6.2 Isolation Levels

| Service | Operation | Isolation Level | Why |
|---|---|---|---|
| conflict-service | Slot capacity check + reserve | `SERIALIZABLE` | Prevents phantom reads — two transactions cannot both see a free slot and both commit |
| conflict-service | Slot release (cancellation) | `READ COMMITTED` | Idempotent; only sets `is_active = false`, no read-modify-write |
| journey-service | Points deduction | `READ COMMITTED` + `SELECT FOR UPDATE` | Pessimistic row lock prevents concurrent deductions |
| journey-service | Journey status update | `READ COMMITTED` | Simple update; no concurrent conflict |
| user-service | User registration | `READ COMMITTED` | Unique constraint on email handles concurrent registration attempts |

### 6.3 Partition Behaviour (CAP Trade-off)

The system chooses **CP (Consistency + Partition Tolerance)** for the booking critical path:

- During a network partition, the conflict-service becomes unreachable from the journey-service
- The circuit breaker opens; bookings fail (REJECTED) rather than proceeding without a conflict check
- **Consistency is preserved at the cost of availability** — no booking is confirmed without an authoritative conflict check

For enforcement lookups, the system chooses **AP (Availability + Partition Tolerance)**:

- During a partition, the enforcement-service serves results from its Redis cache
- Results may be up to 24h stale (marked with `X-Cache-Stale: true` header)
- **Availability is preserved at the cost of strict consistency** — a stale enforcement response is better than no response for a roadside officer

---

## 7. Data Durability Requirements

| Data | Durability mechanism | Loss tolerance |
|---|---|---|
| Confirmed journeys | Postgres WAL (fsync) + streaming replica | Zero — a confirmed booking must survive any single-node failure |
| Booked road slots | Postgres WAL + streaming replica | Zero — losing a slot record could allow a double-booking |
| Points ledger entries | Postgres WAL + streaming replica | Zero |
| RabbitMQ events | Persistent messages (`delivery_mode=2`) + durable queues + mirrored cluster | Zero — every event must be delivered at least once |
| Redis enforcement cache | AOF persistence (`appendonly yes`) | Acceptable loss — cache is reconstructible from Postgres + journey-service |
| Analytics events | Postgres (primary) + read replica | Low — historical reporting data; some loss tolerable in extreme failure |
| Outbox events | Same Postgres transaction as journey record | Zero — outbox is written atomically with the journey |

### 7.1 Redis Persistence Configuration

```
appendonly yes          # AOF: every write appended to log
maxmemory 256mb         # Sized for ~200,000 active journey cache entries
maxmemory-policy allkeys-lru  # LRU eviction: oldest-accessed entries evicted first
```

The 256mb limit is derived from: 200,000 active journeys × ~1KB average cache value = ~200MB, leaving 56MB headroom. LRU eviction is chosen over `volatile-lru` because all keys are operationally equivalent in importance.

---

## 8. Summary: Requirements Traceability

| Rubric criterion | Addressed in section |
|---|---|
| Performance (qualitative + quantitative) | §2 |
| Scalability (qualitative + quantitative) | §3 |
| Availability (qualitative + quantitative) | §4 |
| Reliability | §5 |
| Data consistency | §6 |
| Data durability | §7 |
| Motivated by reference to historic data / load patterns | §1.1, §2.1, §3.1 |
| Qualitative and quantitative requirements | Throughout (all tables include numeric targets) |

---

*CS7NS6 Distributed Systems — Exercise 2 | Group J | TCD 2025–2026*
