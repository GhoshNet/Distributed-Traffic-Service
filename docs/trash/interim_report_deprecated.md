# Journey Booking System — Interim Technical Architecture Report

**Module:** Distributed Systems — Exercise 2
**Date:** February 2026
**Deadline:** Friday, 6th March 2026

---

## Group Members

| Name | Service Responsibility |
|------|----------------------|
| Member 1 *(sign)* | User Service |
| Member 2 *(sign)* | Journey Service |
| Member 3 *(sign)* | Conflict Detection Service |
| Member 4 *(sign)* | Notification Service |
| Member 5 *(sign)* | Enforcement Service |
| Member 6 *(sign)* | Analytics & Monitoring Service |

---

## 1. Introduction

This report outlines the technical architecture of a globally-accessible journey pre-booking system for road-vehicle drivers. The system allows drivers to book, cancel, and manage journeys, receive notifications of booking status, and be verified by enforcement agents. It is designed to serve millions of users worldwide with appropriate levels of performance, scalability, availability, and reliability.

The system is structured as **six distributed microservices** operating in a loosely-coupled manner, communicating via both synchronous REST APIs and asynchronous message passing through a central message broker.

---

## 2. System Requirements

### 2.1 Functional Requirements

1. **User Management:** Drivers register an account, authenticate, and manage their profile (including driving licence and vehicle registration).
2. **Journey Booking:** Drivers request a journey by specifying origin, destination, departure time, estimated duration, and vehicle. The system confirms or rejects the booking.
3. **Conflict Detection:** The system checks for scheduling conflicts — a driver or vehicle cannot have overlapping journeys — and verifies that road capacity is not exceeded in any geographic area.
4. **Journey Cancellation:** Drivers may cancel a previously confirmed journey.
5. **Real-Time Notifications:** Drivers are notified of booking confirmations, rejections, and cancellations via WebSocket and stored notification history.
6. **Enforcement Verification:** Enforcement agents can verify, by vehicle registration or driving licence number, whether a driver currently holds a valid, active journey booking.
7. **Analytics & Monitoring:** System operators can view real-time statistics (bookings per day, rejection rates) and monitor the health of all services.

### 2.2 Non-Functional Requirements

| Property | Target | Rationale |
|----------|--------|-----------|
| **Availability** | 99.9% (~8.7 h downtime/year) | Drivers depend on the system to travel; higher targets are cost-prohibitive for this use case |
| **Booking Latency** | < 2 s (p95) | Pre-booking is not time-critical; 2 s is acceptable UX |
| **Verification Latency** | < 500 ms (p95) | Roadside enforcement checks must be near-instantaneous |
| **Peak Throughput** | ~10,000 bookings/s | Supports millions of users during morning/evening rush hours |
| **Consistency** | Strong per-user writes; eventually consistent global reads | A driver always sees their own confirmed bookings immediately; the global view may lag by seconds |
| **Durability** | No confirmed booking lost | Zero tolerance for data loss of confirmed bookings |

### 2.3 Failure Model

We adopt a **crash-recovery** failure model:

- **Process failures:** Services may crash at any time and restart. All persistent state is stored in durable databases, so no committed data is lost on crash.
- **Network failures:** Messages between services may be delayed, reordered, or lost. We handle this through retries, idempotency keys, and persistent message queues.
- **Byzantine faults:** We assume a trusted internal network between services. Byzantine fault tolerance is not required and would add unjustified complexity.
- **Correlated failures:** Mitigated by deploying service replicas independently (in a production setting, across availability zones).

---

## 3. Technical Architecture

### 3.1 High-Level Overview

The system follows a **microservices architecture** with an API Gateway fronting all client traffic. Services communicate synchronously (REST over HTTP) for request-response flows and asynchronously (RabbitMQ message broker) for event-driven flows.

```
                         ┌─────────────────────────────────┐
                         │        Client (Web/Mobile)       │
                         └───────────────┬─────────────────┘
                                         │ HTTPS
                                         ▼
                         ┌───────────────────────────────────┐
                         │     API Gateway (Nginx)            │
                         │  • Rate limiting  • Load balancing │
                         │  • Routing        • Auth proxy     │
                         └──┬────┬────┬────┬────┬────┬───────┘
                            │    │    │    │    │    │
              ┌─────────────┘    │    │    │    │    └──────────────┐
              ▼                  ▼    │    │    ▼                   ▼
        ┌──────────┐     ┌──────────┐ │    │ ┌──────────┐   ┌──────────┐
        │  User    │     │ Journey  │ │    │ │Enforce-  │   │Analytics │
        │ Service  │     │ Service  │ │    │ │ment Svc  │   │ Service  │
        └────┬─────┘     └──┬──┬───┘ │    │ └────┬─────┘   └────┬─────┘
             │              │  │     │    │      │              │
             │         REST │  │     │    │      │              │
             │              ▼  │     │    │      │              │
             │       ┌──────────┐    │    │      │              │
             │       │ Conflict │    │    │      │              │
             │       │Detection │    │    │      │              │
             │       │ Service  │    │    │      │              │
             │       └────┬─────┘    │    │      │              │
             │            │          │    │      │              │
             ▼            ▼          ▼    ▼      ▼              ▼
        ┌──────┐     ┌──────┐   ┌────────────┐  ┌──────┐  ┌──────┐
        │PG    │     │PG    │   │  RabbitMQ   │  │Redis │  │PG    │
        │Users │     │Conflt│   │  (Broker)   │  │Cache │  │Anlyt │
        └──────┘     └──────┘   └──────┬──────┘  └──────┘  └──────┘
                                       │
                          ┌────────────┼────────────┐
                          ▼            ▼            ▼
                   ┌──────────┐ ┌──────────┐ ┌──────────┐
                   │Notificat-│ │Enforce-  │ │Analytics │
                   │ion Svc   │ │ment Svc  │ │ Service  │
                   │(consumer)│ │(consumer)│ │(consumer)│
                   └──────────┘ └──────────┘ └──────────┘
```

### 3.2 Service Decomposition

Each of the six services maps to one group member and encapsulates a distinct bounded context:

| # | Service | Responsibility | Database | Stateful? |
|---|---------|---------------|----------|-----------|
| 1 | **User Service** | Registration, authentication (JWT), driver profiles | PostgreSQL (users_db) | Yes |
| 2 | **Journey Service** | Journey CRUD, booking saga orchestration, idempotency | PostgreSQL (journeys_db) | Yes |
| 3 | **Conflict Detection Service** | Time-overlap detection, vehicle-overlap detection, road-capacity checking | PostgreSQL (conflicts_db) | Yes |
| 4 | **Notification Service** | Consume events and deliver notifications (WebSocket, stored history) | Redis (notification store) | Minimal |
| 5 | **Enforcement Service** | Verify active bookings by vehicle or licence number | Redis (cache) + fallback to Journey Service | Minimal |
| 6 | **Analytics & Monitoring Service** | Event logging, real-time counters, aggregated statistics, service health dashboard | PostgreSQL (analytics_db) | Yes |

Each stateful service owns its own database instance (**database-per-service** pattern), ensuring loose coupling and independent deployability.

---

## 4. Key Design Decisions & Distributed Systems Techniques

### 4.1 Saga Pattern for Distributed Transactions

The booking flow spans two services (Journey Service and Conflict Detection Service). Since we avoid distributed locking (which would reduce availability), we use a **choreography-free saga** orchestrated by the Journey Service:

1. Journey Service creates the booking with status **PENDING**.
2. Journey Service calls Conflict Detection Service synchronously via REST.
3. If no conflict → status updated to **CONFIRMED**; event published.
4. If conflict → status updated to **REJECTED**; event published.
5. **Compensating action:** If the Conflict Detection Service is unreachable or times out (30 s), the booking is automatically rejected. The driver can safely retry.

This is a deliberate trade-off: we prioritise **availability** over attempting the booking at all costs. A rejected booking due to a transient failure is preferable to an inconsistent confirmed booking.

### 4.2 Asynchronous Event-Driven Communication

After a booking is confirmed, rejected, or cancelled, the Journey Service publishes an event to **RabbitMQ** using topic-based routing. Downstream consumers include:

- **Notification Service** — delivers user-facing notifications.
- **Enforcement Service** — updates its cache of active bookings.
- **Analytics Service** — logs the event for statistics.
- **Conflict Detection Service** — deactivates booking slots on cancellation.

Messages are **persistent** (written to disk before acknowledgement), and failed messages are routed to a **dead-letter queue** for manual inspection. Each consumer is **idempotent** (safe to reprocess the same message).

### 4.3 Caching Strategy

The Enforcement Service requires sub-500 ms responses. We achieve this with a **two-layer lookup**:

1. **Redis cache (primary):** When a journey is confirmed, the Journey Service writes it to Redis with a TTL equal to the journey duration plus a buffer. Cache keys are indexed by both vehicle registration and user ID.
2. **REST API fallback:** On a cache miss (e.g., after a Redis flush), the Enforcement Service queries the Journey Service's database.

Cache invalidation is event-driven: cancellation events trigger cache removal via RabbitMQ.

### 4.4 Idempotency

Every booking request may include a client-generated **idempotency key**. The Journey Service stores a mapping of processed keys to journey IDs. If a retry arrives with the same key, the existing journey is returned without creating a duplicate. This makes retries safe across network failures and client timeouts.

### 4.5 Rate Limiting & Load Balancing

The **Nginx API Gateway** provides:

- **Per-IP rate limiting:** Auth endpoints (5 req/s), booking endpoints (10 req/s), general endpoints (30 req/s). This protects against abuse and provides backpressure during traffic spikes.
- **Round-robin load balancing** across service replicas, with automatic removal of unhealthy instances based on `/health` endpoint checks.

### 4.6 Partitioning

- **Journey data** is logically partitioned by **user ID** (hash-based). This ensures a user's journeys are co-located, enabling efficient queries.
- **Road capacity data** in the Conflict Detection Service is partitioned by **geographic region** using a grid system (~1 km cells), allowing regional conflict detection to scale independently.

### 4.7 Replication

- Each PostgreSQL database is designed for **primary + read replica** streaming replication. Read-heavy services (Enforcement) route queries to replicas.
- Redis operates with **append-only persistence** for durability.
- All application services are **stateless** (session state is in JWT tokens and Redis), enabling horizontal scaling by running multiple replicas behind the load balancer.

---

## 5. Technology Stack

| Component | Technology | Justification |
|-----------|-----------|---------------|
| **Language** | Python 3.12 | Fast development cycle, rich async ecosystem, group familiarity |
| **Web Framework** | FastAPI | Native async/await, high throughput, automatic OpenAPI documentation |
| **Database** | PostgreSQL 16 | ACID guarantees, mature streaming replication, table partitioning, JSON support |
| **Cache** | Redis 7 | Sub-millisecond lookups, TTL-based expiry, append-only persistence |
| **Message Broker** | RabbitMQ 3.13 | Durable topic-based routing, dead-letter queues, built-in management UI |
| **API Gateway** | Nginx | Industry-standard reverse proxy, built-in rate limiting and health checks |
| **ORM** | SQLAlchemy 2.0 (async) | Mature ORM with full async support and migration tooling (Alembic) |
| **Authentication** | JWT (PyJWT + bcrypt) | Stateless, scalable, no session store required |
| **Containerisation** | Docker + Docker Compose | Reproducible multi-service deployment, straightforward failure injection for testing |

---

## 6. Usage & Failure Patterns Considered

| Pattern | Design Response |
|---------|----------------|
| **Peak hours** (morning/evening rush) | Horizontal scaling via stateless replicas; Redis caching reduces database load |
| **Read-heavy workload** (enforcement checks far exceed bookings) | Read replicas for databases; Redis cache-first strategy for enforcement |
| **Geographically distributed users** | Stateless services enable multi-region deployment; geographic partitioning of road capacity data |
| **Data locality** | User's journeys co-located via hash-based partitioning on user ID |
| **Failure frequency** | Crash-recovery model; failures expected to be rare but handled gracefully via saga compensations, message persistence, and idempotency |

---

## 7. Testing & Demonstration Strategy

We have built a **deployment framework** using Docker Compose that enables both normal operation and fault-injection testing:

| Test Type | Tool | Description |
|-----------|------|-------------|
| **End-to-end demo** | `scripts/demo.py` | Exercises the full lifecycle: registration → login → booking → conflict rejection → enforcement check → cancellation → analytics |
| **Failure scenarios** | `scripts/failure_tests.py` | Simulates service crashes, cache failures, broker restarts, and database outages using `docker compose stop/pause/restart` |
| **Load testing** | `scripts/load_test.py` | Concurrent user simulation with latency percentile reporting (p50, p95, p99) and throughput measurement |

### Key Failure Scenarios

1. **Conflict Service crash during booking:** Saga times out → booking rejected → driver retries successfully after service recovery.
2. **Redis cache flushed:** Enforcement Service falls back to REST API to the Journey Service; cache repopulates on subsequent lookups.
3. **RabbitMQ restart:** Persistent messages survive restart; consumers auto-reconnect and resume processing.
4. **Database outage:** Service returns HTTP 503; after database recovery, normal operation resumes with no data loss.

---

## 8. Summary

Our system decomposes the journey booking problem into six independently deployable microservices, each owned by one group member. The architecture applies several distributed systems techniques — the saga pattern, asynchronous messaging, caching with fallback, idempotency, rate limiting, and geographic partitioning — while deliberately avoiding unnecessary complexity such as Byzantine fault tolerance or distributed locking. The result is a system that balances performance, scalability, and reliability without over-engineering.

---

**Signatures:**

| Name | Signature | Date |
|------|-----------|------|
| Member 1 | | |
| Member 2 | | |
| Member 3 | | |
| Member 4 | | |
| Member 5 | | |
| Member 6 | | |
