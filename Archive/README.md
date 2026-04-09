# Globally Distributed Traffic Service (GDTS)

A production-quality distributed systems demonstration in Python.  
Each running instance is a **region node** that participates in a peer-to-peer cluster, manages a local road network, and coordinates journey bookings across nodes using **Two-Phase Commit (2PC)**.

---

## Quick Start

```bash
# Terminal 1 — first region (e.g. Ireland)
source env/bin/activate
python main.py

# Terminal 2 — second region (e.g. France)
source env/bin/activate
python main.py
# When prompted for "Seed node", enter:  127.0.0.1:6000  (port of node 1)
```

Both nodes will discover each other, auto-connect via inter-region roads, and present the simulation menu.

---

## Architecture

Each node runs **7 micro-services** as in-process threads, all exposed via a single Flask REST API.

```
┌───────────────────────────────────────────────────┐
│                 Region Node                        │
│                                                    │
│  [1] Discovery Service    ←─ UDP broadcast         │
│  [2] Region Service       ←─ Road network (NetworkX)│
│  [3] Booking Service      ←─ Journey CRUD + locking│
│  [4] Coordinator Service  ←─ 2PC coordinator      │
│  [5] Health Monitor       ←─ Heartbeats + recovery │
│  [6] Replication Service  ←─ Eventual consistency  │
│  [7] Gateway / Router     ←─ Cross-node routing    │
│                                                    │
│            Flask REST API  (:6000)                 │
└───────────────────────────────────────────────────┘
             ↕  UDP + HTTP
┌──────────────────┐      ┌──────────────────┐
│  Node: Ireland   │────▶ │  Node: France    │
└──────────────────┘      └──────────────────┘
```

---

## Distributed Problems Demonstrated

| # | Problem | Simulation | Solution |
|---|---------|-----------|---------|
| 1 | **Network Delay** | Inject 10–2000 ms latency | Timeout + exponential retry |
| 2 | **Node Failure** | Crash the API (503s returned) | Miss 3 heartbeats → SUSPECT, 6 → DEAD |
| 3 | **Data Consistency** | Two writers same route/time | Optimistic locking + conflict window check |
| 4 | **Concurrent Access** | 20 simultaneous booking threads | SQLite WAL + per-booking thread lock |
| 5 | **Data Partitioning** | Multi-region journey | 2PC PREPARE → COMMIT/ABORT across nodes |
| 6 | **Node Recovery** | Re-enable failed node | Pull-sync from peers; re-announce via broadcast |
| 7 | **Graceful Degradation** | < 50 % peers alive | LOCAL_ONLY mode; queue cross-region requests |

---

## Terminal Menu

```
═══ GDTS Terminal — Region: Ireland ═══
Host 192.168.1.5:6000 | Peers: 2 | Bookings: 14 | Delay: 0ms | 🟢 ALIVE | 🌐 GLOBAL

── Standard Operations ──
[1]  Book a journey
[2]  Cancel a booking
[3]  List all bookings
[4]  Show road network graph
[5]  Show connected peers

── Simulate Distributed Problems ──
[6]  🌐 Network Delay  — inject latency
[7]  💀 Node Failure   — simulate crash
[8]  🔀 Data Consistency — concurrent conflict
[9]  🌪️  Concurrent Storm — booking flood
[10] 🗺️  Cross-Region booking (2PC demo)
[11] 🔄 Node Recovery  — rejoin network
[12] 🔴 Graceful Degradation — local-only mode

[0]  Exit
```

---

## REST API Reference

| Method | Endpoint | Description |
|--------|---------|-------------|
| `GET` | `/api/health/ping` | Liveness probe (503 if failure simulated) |
| `GET` | `/api/health/status` | Full node status |
| `GET` | `/api/region/info` | Region metadata & road stats |
| `GET` | `/api/region/graph` | Full NetworkX graph as JSON |
| `POST` | `/api/booking/create` | Book a journey |
| `POST` | `/api/booking/cancel/<id>` | Cancel a booking |
| `GET` | `/api/booking/list` | List all bookings |
| `GET` | `/api/booking/<id>` | Get single booking |
| `GET` | `/api/peer/list` | List known peers |
| `POST` | `/api/peer/announce` | Manually register a peer |
| `POST` | `/api/coordinator/prepare` | 2PC PREPARE (participant) |
| `POST` | `/api/coordinator/commit` | 2PC COMMIT (participant) |
| `POST` | `/api/coordinator/abort` | 2PC ABORT (participant) |
| `POST` | `/api/replication/sync` | Receive booking replication push |
| `GET` | `/api/replication/bookings-since` | Pull bookings since timestamp |

---

## Cross-Region Booking Flow (2PC)

```
Driver           Ireland Node          France Node
  │                    │                    │
  │──book Dublin→Paris▶│                    │
  │                    │──PREPARE──────────▶│
  │                    │◀──YES──────────────│  (HELD booking created)
  │                    │──COMMIT───────────▶│
  │                    │                    │  HELD → CONFIRMED
  │◀──confirmed────────│                    │
```

If France votes **NO** (conflict / capacity / failure), Ireland sends **ABORT** to all participants and HELD bookings are released.

---

## File Structure

```
GloballyDistributedTrafficService/
├── main.py                      # Entry point & setup wizard
├── config.py                    # All tunable constants
├── node_state.py                # Shared mutable node state
├── requirements.txt
│
├── services/
│   ├── discovery.py             # SERVICE 1: UDP peer discovery
│   ├── region_service.py        # SERVICE 2: Road graph management
│   ├── booking_service.py       # SERVICE 3: Journey CRUD + locking
│   ├── coordinator.py           # SERVICE 4: 2PC coordinator/participant
│   ├── health_monitor.py        # SERVICE 5: Heartbeat + failure detection
│   ├── replication.py           # SERVICE 6: Eventual consistency sync
│   └── gateway.py               # SERVICE 7: Request routing
│
├── models/
│   ├── road_network.py          # NetworkX road graph
│   └── booking.py               # Booking dataclass
│
├── database/
│   └── db.py                    # Thread-safe SQLite (WAL mode)
│
├── api/
│   └── routes.py                # All Flask REST endpoints
│
├── simulation/
│   └── problems.py              # Simulation menu handlers
│
├── utils/
│   └── logger.py                # Rich coloured terminal logger
│
├── data/                        # SQLite databases (one per region)
└── test_integration.py          # 2-node automated integration test
```

---

## Running the Integration Test

```bash
source env/bin/activate
python test_integration.py
```

Tests: health pings, peer discovery, local booking, conflict detection,  
cross-region 2PC, cancellation, replication, node failure, recovery, concurrent storm.

---

## Configuration (`config.py`)

| Constant | Default | Effect |
|---------|---------|--------|
| `DISCOVERY_PORT` | 5001 | UDP broadcast port |
| `DISCOVERY_INTERVAL` | 5 s | Broadcast frequency |
| `HEARTBEAT_INTERVAL` | 3 s | Health check frequency |
| `SUSPECT_THRESHOLD` | 3 | Missed heartbeats → SUSPECT |
| `DEAD_THRESHOLD` | 6 | Missed heartbeats → DEAD |
| `TWO_PC_TIMEOUT` | 5 s | 2PC PREPARE timeout |
| `MAX_ROAD_CAPACITY` | 5 | Max bookings per road segment |
| `REPLICATION_INTERVAL` | 15 s | Periodic replication push |
| `REQUEST_TIMEOUT` | 4 s | HTTP call timeout |

---

## Multi-Machine Deployment

1. Start a seed node on machine A: `python main.py` → note the IP:port
2. On machine B: `python main.py` → enter `<IP_A>:<port>` as seed node
3. Nodes will exchange peer lists and form the mesh automatically
4. UDP broadcast also works automatically on a shared LAN subnet

---

## Dependencies

```
flask, flask-cors, networkx, requests, rich, colorama, tabulate
```

Install: `pip install -r requirements.txt`
