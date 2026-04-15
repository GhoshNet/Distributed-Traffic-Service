# Globally Distributed Traffic Service — Implementation Plan

## Overview

A Python-based distributed system where each running instance represents a **geographical region** (city/country). Instances form a loosely-coupled peer-to-peer network, share a road graph topology, and collectively manage pre-booked driver journeys. The system is designed to demonstrate — and solve — the seven classic distributed systems challenges.

---

## Architecture

Each node runs **7 micro-services** (satisfying N ≥ 6) as in-process threads, exposed via a single Flask REST API. Nodes discover each other via **UDP broadcast**, form inter-region road edges automatically, and coordinate cross-region bookings using **Two-Phase Commit (2PC)**.

```
┌─────────────────────────────────────────────────────┐
│                  Region Node (main.py)               │
│                                                      │
│  ┌──────────┐  ┌──────────┐  ┌───────────────────┐  │
│  │Discovery │  │  Health  │  │  Simulation Menu  │  │
│  │ Service  │  │ Monitor  │  │  (terminal UI)    │  │
│  └────┬─────┘  └────┬─────┘  └───────────────────┘  │
│       │             │                                 │
│  ┌────▼─────────────▼──────────────────────────────┐ │
│  │              Flask REST Gateway                  │ │
│  │  /api/booking  /api/region  /api/health  etc.   │ │
│  └────┬────────────────────────────────────────────┘ │
│       │                                               │
│  ┌────▼──────┐  ┌──────────┐  ┌────────────────────┐ │
│  │  Booking  │  │ Coord.   │  │   Replication      │ │
│  │  Service  │  │ Service  │  │   Service          │ │
│  └────┬──────┘  └────┬─────┘  └────────────────────┘ │
│       │              │                                 │
│  ┌────▼──────────────▼───────────────────────────┐   │
│  │          SQLite Local Database                 │   │
│  └───────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
           ↕  UDP Discovery + HTTP REST
┌──────────────────┐       ┌──────────────────┐
│  Node: Paris     │ ────▶ │  Node: Berlin    │
└──────────────────┘       └──────────────────┘
```

---

## Services (N = 7)

| # | Service | Role |
|---|---------|------|
| 1 | **Discovery Service** | UDP broadcast peer discovery; maintains peer registry |
| 2 | **Region Service** | Manages the road network graph (NetworkX), region metadata |
| 3 | **Booking Service** | Create/cancel local and cross-region journeys |
| 4 | **Coordinator Service** | 2PC for cross-region bookings; distributed locking |
| 5 | **Health Monitor Service** | Heartbeat-based failure detection; triggers recovery |
| 6 | **Replication Service** | Propagates confirmed bookings to peer replicas for fault tolerance |
| 7 | **Gateway / Router Service** | Routes incoming booking requests to the correct region node |

---

## Distributed Problems & Solutions

| Problem | Simulation | Solution Implemented |
|---------|-----------|----------------------|
| **Network Delay** | Inject artificial sleep (10–500 ms) on outgoing HTTP calls | Timeout handling + retry with exponential backoff |
| **Node Failure** | Menu option kills the HTTP listener thread, stops heartbeats | Peers detect via missed heartbeats; mark node as "suspect → dead"; re-route traffic |
| **Data Consistency** | Concurrent writes shown on screen | Optimistic locking with version vectors; eventual consistency via replication |
| **Concurrent Access** | Spawn N threads hitting the same route simultaneously | Thread-safe SQLite writes with serializable transactions + row-level locks |
| **Data Partitioning** | Multi-region journey (e.g., Dublin→London) | Consistent hashing to assign "home region"; 2PC for cross-region transactions |
| **Node Recovery** | Re-start a "failed" node | Node announces itself, pulls missed bookings via replication sync |
| **Graceful Degradation** | Kill majority of peers | Operate in "local-only" read mode; queue cross-region requests; warn user |

---

## File Structure

```
GloballyDistributedTrafficService/
├── env/                          # Virtual environment
├── requirements.txt
├── main.py                       # Entry point — setup wizard + menu
│
├── config.py                     # Ports, timeouts, constants
│
├── services/
│   ├── __init__.py
│   ├── discovery.py              # SERVICE 1: UDP peer discovery
│   ├── region_service.py         # SERVICE 2: Road graph + region management
│   ├── booking_service.py        # SERVICE 3: Journey CRUD + conflict detection
│   ├── coordinator.py            # SERVICE 4: 2PC coordinator/participant
│   ├── health_monitor.py         # SERVICE 5: Heartbeat + failure detection
│   ├── replication.py            # SERVICE 6: Booking replication to peers
│   └── gateway.py                # SERVICE 7: Request routing to home region
│
├── models/
│   ├── __init__.py
│   ├── road_network.py           # Random graph generation (NetworkX)
│   └── booking.py                # Booking dataclass / schema
│
├── database/
│   ├── __init__.py
│   └── db.py                     # SQLite manager (bookings + peers + locks)
│
├── api/
│   ├── __init__.py
│   └── routes.py                 # Flask routes (all REST endpoints)
│
├── simulation/
│   ├── __init__.py
│   └── problems.py               # Menu-driven problem simulators
│
└── utils/
    ├── __init__.py
    └── logger.py                 # Coloured terminal logger
```

---

## Key Design Decisions

### Peer Discovery
- **UDP broadcast** on LAN port 5001 (configurable) every 5 s
- Each node broadcasts `{region_name, api_port, graph_summary}`
- On receipt, update local peer registry; auto-create inter-region road edge

### Road Network
- Within a region: `NetworkX` random connected graph (`nodes = num_cities`, edges weighted by distance)
- Between regions: automatically added when a new peer is discovered (edge weight = latency estimate)

### Booking Model
- `booking_id`, `driver_id`, `origin`, `destination`, `departure_time`, `status` (`PENDING/CONFIRMED/CANCELLED/HELD`)
- Routes that cross regions → promoted to 2PC flow

### Two-Phase Commit (2PC)
- **Coordinator** (the region receiving the request) sends `PREPARE` to all participant regions
- Participants respond `YES` / `NO` (check capacity/conflicts, set booking to `HELD`)
- Coordinator sends `COMMIT` or `ABORT`
- Timeout guard: if no `YES` within 5 s → `ABORT`

### Conflict Detection
- At booking time, check if any `CONFIRMED/HELD` booking shares the same `(origin, destination, departure_time ± 5 min)` window
- If conflict → reject request

### Heartbeat & Failure Detection
- Each node sends `GET /api/health/ping` to all peers every 3 s
- If 3 consecutive misses → peer marked `SUSPECT`
- After 5 more misses → peer marked `DEAD`; removed from routing table
- On peer recovery: node re-announces, replication service pulls delta

### Graceful Degradation
- If < 50 % of known peers are reachable → switch to `LOCAL_ONLY` mode
- Bookings that require cross-region approval are **queued** (not rejected)
- User sees a clear banner on the terminal

---

## Terminal Menu (Simulation)

```
=== GDTS Simulation Menu ===
[1] Book a journey
[2] Cancel a journey
[3] View all bookings
[4] Show region road network
[5] Show connected peers
--- Simulate Distributed Problems ---
[6]  Simulate: Network Delay (inject latency)
[7]  Simulate: Node Failure (self-shutdown)
[8]  Simulate: Data Consistency conflict
[9]  Simulate: Concurrent booking storm
[10] Simulate: Cross-region booking (partitioned data)
[11] Simulate: Node Recovery (re-join network)
[12] Simulate: Graceful Degradation (peer unavailable)
[0]  Exit
```

---

## Technology Stack

| Concern | Library |
|---------|---------|
| REST API | `Flask` + `flask-cors` |
| Road Graph | `networkx` |
| Database | `sqlite3` (stdlib) |
| Peer Discovery | `socket` UDP broadcast (stdlib) |
| Concurrency | `threading` (stdlib) |
| Logging | `colorama` + custom logger |
| HTTP Client | `requests` |
| Pretty output | `tabulate` + `rich` |

---

## Verification Plan

1. Run 3 instances in 3 separate terminals (representing Paris, Berlin, Dublin)
2. Watch them auto-discover and form inter-region edges
3. Book a cross-region journey (Paris→Dublin) and observe 2PC logs on all terminals
4. Simulate node failure on Berlin; confirm Paris and Dublin operate normally
5. Recover Berlin; observe state sync logs
6. Simulate concurrent booking storm and observe lock/retry logs
7. Inject network delay and observe timeout/retry behaviour

---

## Open Questions

> [!IMPORTANT]
> **1. Network scope**: Should peer discovery use **LAN UDP broadcast** (easy for local demo) or a **seed-node bootstrap file** (better for cross-machine/internet setups)? I plan to support both.

> [!IMPORTANT]
> **2. Region granularity**: Should "region" mean a **city** or a **country** that contains cities? Current plan: one process = one country; cities are nodes inside the graph.

> [!NOTE]
> **3. Conflict policy**: Two drivers wanting the same route at the same time — should this be rejected outright or are multiple bookings on the same road allowed up to a configurable capacity limit?
