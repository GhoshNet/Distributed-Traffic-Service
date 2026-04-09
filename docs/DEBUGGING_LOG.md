# Journey Booking System — Debugging & Fix Log

## Context

CS7NS6 Exercise 2 — Distributed Systems.  
6 FastAPI/Go microservices running on Docker (slim mode, M1 Mac, 8 GB RAM).  
Stack: Python FastAPI, Go, PostgreSQL 16, Redis 7, RabbitMQ 3.13, Nginx, Docker Compose.

Start system:
```bash
docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d
```

---

## Bug 1 — `journeys.route_id` column does not exist

**Symptom**  
Journey scheduler crashes every 30 seconds with:
```
sqlalchemy.exc.ProgrammingError: UndefinedColumnError: column journeys.route_id does not exist
```
Affects: journey lifecycle transitions (CONFIRMED → IN_PROGRESS → COMPLETED).

**Root cause**  
`journey-service/app/database.py:68` added `route_id: Mapped[str]` to the SQLAlchemy ORM model,  
but the PostgreSQL table was created before that column was added. SQLAlchemy does not auto-migrate.

**Fix (no rebuild needed)**
```bash
docker exec excercise2-postgres-journeys-1 psql -U journeys_user -d journeys_db \
  -c "ALTER TABLE journeys ADD COLUMN route_id VARCHAR(50);"

docker restart excercise2-journey-service-1
```

**Note:** Only needed on databases created before the column was added to the model.  
Fresh `docker compose down -v && up` creates the table correctly from the ORM model.

**Verification**  
After restart, scheduler logs:
```
INFO app.scheduler: Transitioning journey <id> to COMPLETED
```

---

## Bug 2 — `GET /api/journeys/` returns 500 (DNS: Name or service not known)

**Symptom**  
`GET /api/journeys/` through the gateway returns HTTP 500.  
Journey service logs: `socket.gaierror: [Errno -2] Name or service not known`

**Root cause**  
`docker-compose.slim.yml` sets `DATABASE_READ_URL: ""` intending to fall back to the primary DB.  
Docker Compose does **not** override an existing non-empty env var with an empty string from an override file.  
Result: the running container still uses:
```
DATABASE_READ_URL=postgresql+asyncpg://journeys_user:journeys_pass@postgres-journeys-replica:5432/journeys_db
```
`postgres-journeys-replica` only exists in `full` profile — causing a DNS failure on every read query.

**Fix**  
Update `docker-compose.slim.yml` to set explicit primary DB URLs instead of empty strings:

```yaml
# docker-compose.slim.yml
journey-service:
  environment:
    DATABASE_READ_URL: "postgresql+asyncpg://journeys_user:journeys_pass@postgres-journeys:5432/journeys_db"

user-service:
  environment:
    DATABASE_READ_URL: "postgresql+asyncpg://users_user:users_pass@postgres-users:5432/users_db"

analytics-service:
  environment:
    DATABASE_READ_URL: "postgresql://analytics_user:analytics_pass@postgres-analytics:5432/analytics_db"
```

Then recreate the affected services:
```bash
docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d --no-build \
  journey-service user-service analytics-service
```

---

## Bug 3 — Enforcement agent always gets `DRIVER` role in JWT

**Symptom**  
`POST /api/users/register/agent` returns HTTP 201 with `role: DRIVER`.  
Logging in as the agent produces a JWT with `"role": "DRIVER"`.  
Calling `GET /api/enforcement/verify/vehicle/...` with agent token returns HTTP 403:
```json
{"detail": "Operation requires ENFORCEMENT_AGENT role"}
```

**Root cause**  
The running user-service Docker image was built from an **older version** of `user-service/app/service.py`  
that did not include the `role=...` parameter in the `User()` constructor:

```python
# OLD (in running container image):
user = User(
    id=str(uuid.uuid4()),
    email=request.email,
    password_hash=pwd_context.hash(request.password),
    full_name=request.full_name,
    license_number=request.license_number,
    # ← role missing here!
)
```

```python
# NEW (in local file):
user = User(
    ...
    role=request.role.value if hasattr(request, "role") and request.role else "DRIVER",
)
```

Without the `role` parameter, SQLAlchemy uses the Python-level `default="DRIVER"` from the model,  
storing `DRIVER` regardless of what the route set on `request.role`.

**Fix**  
Rebuild and restart user-service:
```bash
docker compose -f docker-compose.yml -f docker-compose.slim.yml build user-service
docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d user-service

# Also recreate nginx to pick up new service IP:
docker stop excercise2-api-gateway-1-1 && docker rm excercise2-api-gateway-1-1
docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d --no-build api-gateway-1
```

**Verification**
```bash
# Register agent, login, decode JWT:
# JWT payload should contain: "role": "ENFORCEMENT_AGENT"
```

---

## Nginx Gateway Stale IP Issue (operational note)

**Symptom**  
After recreating a service container (new IP assigned), nginx upstream returns 404 for routes  
that were working before, because nginx's static `upstream {}` block cached the old IP at startup.

**Root cause**  
Nginx `upstream {}` blocks resolve DNS **once at startup**, not periodically.  
The `resolver 127.0.0.11 valid=10s;` directive only applies to `proxy_pass` with variables, not static upstreams.

**Fix**  
After recreating any backend service, force-recreate the nginx gateway container:
```bash
docker stop excercise2-api-gateway-1-1 && docker rm excercise2-api-gateway-1-1
docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d --no-build api-gateway-1
```

**Long-term fix** (optional)  
Rewrite nginx `location` blocks to use variables:
```nginx
resolver 127.0.0.11 valid=5s ipvs=off;
set $user_svc "user-service:8000";
location /api/users/ {
    proxy_pass http://$user_svc/api/users/;
}
```

---

## Nginx Config Stale Mount Issue (Mac + Docker Desktop)

**Symptom**  
`docker exec excercise2-api-gateway-1-1 wc -c /etc/nginx/nginx.conf` shows a smaller byte count  
than the local file. `nginx -s reload` fails with "unexpected end of file".

**Root cause**  
Docker Desktop on Mac sometimes does not sync bind-mounted file changes to the container immediately  
when the container was started before the file was modified on the host.

**Fix**  
Force-recreate the container (stop + rm + up) rather than just restarting it:
```bash
docker stop excercise2-api-gateway-1-1 && docker rm excercise2-api-gateway-1-1
docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d --no-build api-gateway-1
```

---

## Bug 5 — Multi-Laptop Peer Registration Broken (`/admin/peers/register` → 404)

**Symptom**  
`POST /api/peers/register` through gateway returns 404. Frontend shows "Registered peer 'undefined' → undefined" plus a `toastCounter` JS error.

**Root causes**

1. **nginx variable proxy_pass drops URI suffix** — `proxy_pass http://$var/path/` replaces the matched location with the static path but does NOT append the remaining URI. Request `/admin/peers/register` → nginx sent `/admin/peers/` to journey-service → FastAPI 404.

2. **nginx container had no networks** — when stopping/recreating the gateway using relative docker-compose file paths, Docker Compose cannot reliably attach the container to `journey-net`. The container starts but fails DNS resolution for upstream services (emerg: host not found in upstream).

3. **`toastCounter` TDZ error** — `let toastCounter = 0` was declared at line ~884 in app.js. Any call to `showToast()` from a WebSocket event or early async error before the script fully executed hit the Temporal Dead Zone.

4. **Frontend label wrong** — peer URL hint said `/health/nodes` but the health monitor pings `/health` (which returns 503 on failure simulation).

**Fixes applied**

Fix nginx variable proxy_pass for health/admin locations (use `proxy_pass http://$journey_svc` with no path — full URI is forwarded unchanged):
```nginx
location = /health {
    set $journey_svc "journey-service:8000";
    proxy_pass http://$journey_svc;   # ← no path suffix
    ...
}
location /admin/ {
    set $journey_svc "journey-service:8000";
    proxy_pass http://$journey_svc;   # ← no path suffix
    ...
}
```

Fix nginx recreate — always use absolute paths AND check for port conflicts (haproxy grabs 8080 in some states):
```bash
# Stop anything on port 8080 first
docker stop excercise2-haproxy-1 2>/dev/null || true
docker rm -f excercise2-api-gateway-1-1 2>/dev/null || true
docker compose -f /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/docker-compose.yml \
               -f /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/docker-compose.slim.yml \
               up -d --no-build api-gateway-1
```

Fix JS TDZ — moved `let toastCounter = 0` to the top of app.js with the other global declarations.

Fixed frontend label to say `/health` not `/health/nodes`.

---

## Bug 6 — Cross-Node Conflict Detection Not Working (Multi-Laptop)

**Symptom**  
Two laptops peered on the same hotspot. When both users book the same route/time through  
**different** IP addresses (`172.20.10.10:8080` vs `172.20.10.12:8080`), no conflict is detected.  
When both use the **same** IP, conflicts are detected correctly.

**Root cause**  
Each laptop runs a fully independent Docker stack with its own `postgres-conflicts` database.  
The conflict-service only checks its **local** `booked_slots` and `road_segment_capacity` tables.  
Bookings on Node A are invisible to Node B — no shared or replicated state existed between nodes.

This is the distributed shared-state problem: without cross-node replication, the system behaves  
as two isolated silos rather than a distributed system.

**Fix — REST-based slot replication with startup catch-up sync**

New file `conflict-service/replication.go` implements three mechanisms:

**1. Forward replication (push, async)**  
After every successful booking commit in `checkConflicts()`, the originating node pushes the  
slot to all registered peers via `POST /internal/slots/replicate`. Each push runs in a  
goroutine — non-blocking, eventual consistency. Cancellations are likewise pushed via  
`POST /internal/slots/cancel` from both the REST cancel handler and the RabbitMQ consumer  
(peers have their own RabbitMQ and won't receive the cancellation event independently).

**2. Startup catch-up sync (pull, handles late-join and rejoin-after-downtime)**  
On startup, each node calls `GET /internal/slots/active` on every configured peer and applies  
any missing slots locally. This covers two cases:
- **Late-joining peer**: Peer X starts with an empty DB; it pulls everything from Peer A.
- **Rejoin after downtime**: Peer B was offline for N minutes; it backfills all missed bookings.

The `applyReplicatedSlot` function is fully idempotent (skips slots already present by `journey_id`),  
so startup sync and forward replication can overlap without double-counting capacity.

**3. Periodic re-sync every 5 minutes**  
A background goroutine re-syncs from all peers every 5 minutes. This is a safety net for  
transient network failures where a push was missed but the node never restarted.

**4. Runtime peer registration**  
`POST /internal/peers/register {"peer_url": "..."}` adds a peer to the live list without  
restarting the container, and immediately triggers a catch-up sync from the new peer.

**Files changed:**
- `conflict-service/replication.go` — new file, all replication and sync logic
- `conflict-service/service.go` — added `go replicateSlotToPeers(req, arrivalTime)` after commit
- `conflict-service/handlers.go` — added `activeSlotsHandler`, `addPeerHandler`, `replicateSlotHandler`, `replicateCancelHandler`; updated cancel handler to call `go replicateCancelToPeers(journeyID)`
- `conflict-service/consumer.go` — added `go replicateCancelToPeers(journeyID)` after RabbitMQ-triggered cancel
- `conflict-service/config.go` — added `PeerConflictURLs []string` parsed from `PEER_CONFLICT_URLS` env var (comma-separated)
- `conflict-service/main.go` — sets peer list, starts startup sync goroutines, starts periodic sync, registers new internal routes
- `docker-compose.slim.yml` — added `PEER_CONFLICT_URLS: "${PEER_CONFLICT_URLS:-}"` under conflict-service

**DS concepts demonstrated:**
- Eventual consistency (async push replication)
- State transfer / catch-up sync on node join
- Idempotent replication (safe to apply same slot multiple times)
- Gossip-style peer-to-peer data propagation
- Known limitation: concurrent booking from two nodes in the same ~millisecond window can both pass (no distributed lock). This is the inherent cost of active-active eventual consistency without consensus (Raft/Paxos).

---

## API Reference (discovered during testing)

### User Service (port 8001 / gateway `/api/users/`)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| POST | `/api/users/register` | none | Registers a DRIVER. `role` field is ignored (forced to DRIVER). |
| POST | `/api/users/register/agent` | none | Registers an ENFORCEMENT_AGENT. |
| POST | `/api/users/login` | none | Returns JWT. |
| POST | `/api/users/vehicles` | bearer (DRIVER) | Register a vehicle to the current user. |
| GET | `/api/users/vehicles` | bearer (DRIVER) | List user's vehicles. |

### Journey Service (port 8002 / gateway `/api/journeys/`)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| POST | `/api/journeys/` | bearer (DRIVER) | Book a journey. Add `?mode=2pc` for Two-Phase Commit. |
| GET | `/api/journeys/` | bearer (DRIVER) | List current user's journeys. |
| GET | `/api/journeys/{id}` | bearer (DRIVER) | Get a single journey. |
| DELETE | `/api/journeys/{id}` | bearer (DRIVER) | Cancel a journey. |
| GET | `/api/journeys/vehicle/{reg}/active` | internal | Used by enforcement service. |

**Vehicle must be pre-registered** via `/api/users/vehicles` before booking.

### Enforcement Service (port 8005 / gateway `/api/enforcement/`)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/api/enforcement/verify/vehicle/{reg}` | bearer (ENFORCEMENT_AGENT) | Verify active journey by vehicle. |
| GET | `/api/enforcement/verify/license/{num}` | bearer (ENFORCEMENT_AGENT) | Verify active journey by license number. |

**Important**: Returns `is_valid=false` if journey departs more than 30 minutes in the future  
(correct enforcement behavior — you can only check active/in-progress journeys).

### Analytics Service (port 8006 / gateway `/api/analytics/`)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/api/analytics/stats` | none | Today's stats: confirmed, rejected, cancelled counts. |
| GET | `/api/analytics/events` | none | Event log. |
| GET | `/api/analytics/hourly` | none | Hourly breakdown. |

Note: endpoint is `/stats` not `/summary`.

### Conflict Service (port 8003 / gateway `/api/conflicts/`)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| POST | `/api/conflicts/check` | internal | Called by journey-service; checks and reserves road capacity. |
| POST | `/api/conflicts/cancel/{journey_id}` | internal | Deactivates booking slot on cancellation. |
| GET | `/api/conflicts/routes` | none | Returns all predefined routes with waypoints. |

**Internal replication endpoints** (direct port 8003 only, not proxied through nginx):

| Method | Path | Notes |
|--------|------|-------|
| GET | `/internal/slots/active` | Returns all active slots — used for catch-up sync by peers. |
| POST | `/internal/slots/replicate` | Receives a new slot pushed by a peer. Idempotent. |
| POST | `/internal/slots/cancel` | Receives a cancellation pushed by a peer. Idempotent. |
| POST | `/internal/peers/register` | Registers a peer URL at runtime and triggers immediate catch-up sync. Body: `{"peer_url": "http://..."}` |

---

## System Health Check

All services should be healthy after startup:
```bash
curl http://localhost:8080/health           # journey service (via gateway)
curl http://localhost:8001/health           # user service (direct)
curl http://localhost:8002/health           # journey service (direct)
curl http://localhost:8003/health           # conflict service (direct)
curl http://localhost:8004/health           # notification service (direct)
curl http://localhost:8005/health           # enforcement service (direct)
curl http://localhost:8006/health           # analytics service (direct)
```

---

## End-to-End Test Results (post-fixes)

All core distributed systems features verified working:

| Feature | Status | Notes |
|---------|--------|-------|
| User registration | ✓ PASS | |
| JWT authentication | ✓ PASS | |
| Vehicle registration | ✓ PASS | Required before booking |
| Enforcement agent role | ✓ PASS | Fixed by rebuild |
| Journey booking (Saga) | ✓ PASS | Returns CONFIRMED |
| Idempotency | ✓ PASS | Same key returns same journey |
| Conflict detection (single node) | ✓ PASS | REJECTED with reason |
| Conflict detection (cross-node) | ✓ PASS | Slot replicated to peer before peer's booking check |
| Two-Phase Commit (2PC) | ✓ PASS | CONFIRMS when road capacity free |
| Journey listing (read replica) | ✓ PASS | Fixed by DATABASE_READ_URL fix |
| Enforcement verification | ✓ PASS | Returns `is_valid` field |
| Journey cancellation | ✓ PASS | Returns CANCELLED; cancellation replicated to peers |
| Analytics stats | ✓ PASS | Returns daily counters |
| Cross-node catch-up sync | ✓ PASS | Late-joining peer pulls all active slots on startup |
| Rejoin-after-downtime sync | ✓ PASS | Periodic 5-min re-sync fills missed bookings |
| Runtime peer registration | ✓ PASS | POST /internal/peers/register + immediate sync |
| Node failure detection | ✓ PASS | ALIVE → SUSPECT (3 misses) → DEAD (6 misses) |

**Note on enforcement timing**: Enforcement returns `is_valid=false` for journeys departing >30 min  
in the future — this is correct behavior, not a bug.

**Note on 2PC road capacity**: If many test runs have booked the Galway→Dublin route, road capacity  
(max 5 bookings per segment) fills up and 2PC correctly aborts. Use a different route or wait for  
scheduled journeys to complete.

---

## Multi-Laptop Setup Guide

**Network (hotspot):** Your machine: `172.20.10.10` | Peer: `172.20.10.12`

**Architecture note**: Each laptop runs an independent Docker stack with its own databases.  
Booking data is replicated between nodes via the conflict-service replication layer.  
Health monitoring (ALIVE/SUSPECT/DEAD) is separate and tracks liveness only.

**Peer registration is in-memory for health monitoring** — lost on every container restart.  
Re-register health peers after each `docker compose up`. Conflict-service peers are persistent  
via `PEER_CONFLICT_URLS` env var and do NOT need re-registration after restart.

### Your laptop (172.20.10.10) — already running

```bash
# 1. Set peer URL and recreate conflict-service
export PEER_CONFLICT_URLS=http://172.20.10.12:8003
docker compose -f /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/docker-compose.yml \
  -f /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/docker-compose.slim.yml \
  up -d --no-build conflict-service

# 2. Confirm peer registered
docker logs excercise2-conflict-service-1 2>&1 | grep -E "peer|sync"
# Expected: Cross-node replication enabled — peers: [http://172.20.10.12:8003]

# 3. Open frontend
open http://localhost:3000
```

### Peer laptop (172.20.10.12) — starting from scratch

```bash
# 1. Get the code
git clone <repo-url> Excercise2 && cd Excercise2 && git checkout approach3

# 2. Set peer URL pointing back to your laptop
export PEER_CONFLICT_URLS=http://172.20.10.10:8003

# 3. Build conflict-service (has the replication code)
docker compose -f docker-compose.yml -f docker-compose.slim.yml build conflict-service

# 4. Start the full stack
docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d

# 5. Verify catch-up sync ran
sleep 10
docker logs <project>-conflict-service-1 2>&1 | grep sync
# Expected: [sync] catch-up from http://172.20.10.10:8003 complete: X/X slots applied

# 6. Open frontend
open http://172.20.10.12:3000
```

### Adding a peer at runtime (no restart needed)

If the peer laptop comes online after your stack is already running:
```bash
curl -X POST http://localhost:8003/internal/peers/register \
  -H "Content-Type: application/json" \
  -d '{"peer_url": "http://172.20.10.12:8003"}'
# Returns: {"registered":"http://172.20.10.12:8003","peers":[...],"note":"Catch-up sync started in background"}
```

### Register health peers (for ALIVE/SUSPECT/DEAD demo)

On your machine:
```bash
curl -X POST http://localhost:8080/admin/peers/register \
  -H "Content-Type: application/json" \
  -d '{"name": "peer-laptop", "health_url": "http://172.20.10.12:8080/health"}'
```

On the peer machine:
```bash
curl -X POST http://localhost:8080/admin/peers/register \
  -H "Content-Type: application/json" \
  -d '{"name": "your-laptop", "health_url": "http://172.20.10.10:8080/health"}'
```

Or use the **Simulate tab → Register Remote Peer** section in the frontend.

### Cross-node conflict demo (frontend)

1. Both devices: register account (different emails), register a vehicle (different plates)
2. Both devices: Journeys tab → Quick Route → `Dublin → Galway (M6)`, same departure time
3. **Device A** submits first → green toast: `Journey booked! (CONFIRMED)`
4. Wait ~1 second (replication is async, usually <100ms on LAN)
5. **Device B** submits → red toast: `Rejected: Road segment fully booked at HH:MM UTC`

### Node failure demo (frontend — Simulate tab)

1. Click **Kill Node** on Device A
2. Watch Device B's peer grid: `ALIVE` → `SUSPECT` (~30s) → `DEAD` (~60s)
3. Click **Recover Node** on Device A
4. Device B shows `ALIVE` on the next heartbeat

### Troubleshooting

| Problem | Fix |
|---------|-----|
| Port 8080 already allocated | `docker stop excercise2-haproxy-1` then recreate nginx gateway |
| Peer shows DEAD immediately | `curl --connect-timeout 5 http://172.20.10.12:8080/health` — check macOS firewall |
| No conflict after cross-node booking | `docker logs conflict-service-1 \| grep replication` — should show `slot X → http://...` |
| Peer joined late, no catch-up | Call `POST /internal/peers/register` or restart conflict-service with `PEER_CONFLICT_URLS` set |
| Port 8003 unreachable from peer | Allow port 8003 in macOS Firewall (System Settings → Network → Firewall) |

---

## Key Things to Remember for Next Session

1. Always rebuild after code changes: `docker compose -f docker-compose.yml -f docker-compose.slim.yml build <service>`
2. After recreating any backend service, recreate the nginx gateway to flush stale IPs
3. `docker-compose.slim.yml` must use explicit primary DB URLs (not empty strings) for `DATABASE_READ_URL`
4. The `route_id` column was manually added to the live DB — any fresh `docker compose down -v && up` will need it again (it's in the ORM model but not in an init SQL). Not needed on fresh starts with clean volumes.
5. Analytics endpoint is `/api/analytics/stats`, not `/summary`
6. Enforcement endpoint requires `ENFORCEMENT_AGENT` role — register via `/api/users/register/agent`
7. Multi-device: always recreate nginx with **absolute paths** — using relative paths leaves the container with no network attachments
8. `PEER_CONFLICT_URLS` env var must be exported in the shell **before** running `docker compose up` — or set as a shell variable inline: `PEER_CONFLICT_URLS=http://172.20.10.12:8003 docker compose ... up -d conflict-service`
9. Conflict-service internal replication endpoints are on **port 8003 direct**, not through the nginx gateway (port 8080). Firewall must allow 8003 on both machines for cross-node replication to work.
10. Health-monitor peers (ALIVE/SUSPECT/DEAD) are in-memory in journey-service — re-register after every restart via the frontend Simulate tab or curl.
