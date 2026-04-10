# Multi-Laptop Demo Setup Guide
**CS7NS6 — Distributed Journey Booking System (Docker Swarm)**

---

## Before You Start

> Run this on **both laptops** to get your IPs, then fill them in below.

```bash
ipconfig getifaddr en0
```

Write them down:
- **Laptop A (yours):** `_______________` (e.g. `172.20.10.10`)
- **Laptop B (peer):**  `_______________` (e.g. `172.20.10.12`)

**Requirements on both machines:**
- Docker Desktop installed and running
- Both laptops on the **same Wi-Fi / hotspot**
- macOS Firewall off, or ports **8003**, **8080**, and **2377** allowed
- Docker Swarm initialized (`docker info | grep Swarm` → must say `active`)

---

## Understanding the UI Indicators

| Indicator | What it means | Does it affect bookings? |
|-----------|--------------|--------------------------|
| `● Live Data` (green dot) | WebSocket push notifications (toasts + live map). Goes grey while reconnecting. | **No.** Bookings work whether green or grey. |
| `🟢 Primary` | Your own backend node is handling requests. Each user sees their **own** node as Primary — this is correct. | Yes — switches to `⚡ Failover: <peer-ip>` when your node is down. |

**If the Quick Route dropdown is empty:** Routes failed to load at login (conflict-service wasn't ready). Fix: click away to another tab and back to **Journeys** — it retries automatically.

---

## LAPTOP A — Your Machine

### Step 1 — Clean wipe Docker (start fresh)

> **Warning:** This deletes ALL data — databases, queues, volumes. Skip if you just want to redeploy.

```bash
# Remove the existing stack
docker stack rm traffic-service
sleep 15

# Remove all volumes
docker volume rm \
  traffic-service_rabbitmq_data \
  traffic-service_rabbitmq_data_2 \
  traffic-service_rabbitmq_data_3 \
  traffic-service_redis_data \
  traffic-service_pg_users_data \
  traffic-service_pg_users_replica_data \
  traffic-service_pg_journeys_data \
  traffic-service_pg_journeys_replica_data \
  traffic-service_pg_conflicts_data \
  traffic-service_pg_conflicts_replica_data \
  traffic-service_pg_analytics_data \
  traffic-service_pg_analytics_replica_data

# Remove locally built images
docker rmi $(docker images '127.0.0.1:5000/*' -q) 2>/dev/null || true
docker system prune -f
```

---

### Step 2 — Verify Swarm is initialized

```bash
docker info | grep Swarm
# Must say: Swarm: active
```

If not active:
```bash
docker swarm init
```

---

### Step 3 — Write peer IP into .env

```bash
cat > .env <<EOF
PEER_CONFLICT_URLS=http://<LAPTOP_B_IP>:8003
PEER_USER_URLS=http://<LAPTOP_B_IP>:8080
EOF
```

> Replace `<LAPTOP_B_IP>` with Laptop B's actual IP.
> This file is loaded automatically at deploy time.

---

### Step 4 — Deploy the full stack

```bash
cd /path/to/Excercise2

# Export the peer env vars so docker stack deploy picks them up
export $(cat .env | xargs)

# Start the local registry if not already running
docker service ls | grep registry || \
  docker service create --name registry --publish published=5000,target=5000 registry:2
sleep 5

# Run the deploy script (builds all images, pushes to local registry, deploys stack)
./deploy-swarm.sh
```

> The script builds all 6 services, pushes them to the local registry on port 5000, then deploys the full Swarm stack. First run takes ~2–3 minutes to build. Subsequent runs use Docker layer cache and take ~30 seconds.

---

### Step 5 — Monitor services coming up

```bash
docker service ls
```

Wait until all services show `N/N` replicas (not `0/N`). Takes ~60–90 seconds for databases and RabbitMQ to be healthy. If any service is stuck:

```bash
# See what's wrong
docker service ps traffic-service_<service-name> --no-trunc

# See logs
docker service logs traffic-service_<service-name> --tail 50
```

---

### Step 6 — Confirm peer replication is configured

```bash
docker service logs traffic-service_conflict-service 2>&1 | grep -E "peer|sync|replication" | tail -10
docker service logs traffic-service_user-service 2>&1 | grep -E "peer|sync|replication" | tail -10
```

> Expected:
> - `Cross-node replication enabled — peers: [http://<LAPTOP_B_IP>:8003]`
> - `[user-replication] peers configured: ['http://<LAPTOP_B_IP>:8080']`

---

### Step 7 — Open the frontend

```
http://localhost:3000
```

Hard-refresh first (`Cmd+Shift+R`) to clear any cached JS.

---

### Updating peer IPs without a full redeploy

If you need to change the peer IP after the stack is already running:

```bash
# Edit .env with new IP
cat > .env <<EOF
PEER_CONFLICT_URLS=http://<NEW_PEER_IP>:8003
PEER_USER_URLS=http://<NEW_PEER_IP>:8080
EOF

# Re-export and redeploy (only affected services restart)
export $(cat .env | xargs)
docker stack deploy -c docker-compose.swarm.yml traffic-service
```

---
---

## LAPTOP B — Peer Machine (fresh setup)

### Step 1 — Clone the repository

```bash
git clone https://github.com/GhoshNet/Distributed-Traffic-Service.git Excercise2
cd Excercise2
```

### Step 2 — Switch to the correct branch

```bash
git checkout approach3
```

### Step 3 — Verify Swarm is initialized

```bash
docker info | grep Swarm
# Must say: Swarm: active
```

If not active:
```bash
docker swarm init
```

### Step 4 — Write peer IP into .env

```bash
cat > .env <<EOF
PEER_CONFLICT_URLS=http://<LAPTOP_A_IP>:8003
PEER_USER_URLS=http://<LAPTOP_A_IP>:8080
EOF
```

> Replace `<LAPTOP_A_IP>` with Laptop A's actual IP.

---

### Step 5 — Start the local registry

```bash
docker service ls | grep registry || \
  docker service create --name registry --publish published=5000,target=5000 registry:2
sleep 5
```

---

### Step 6 — Deploy the full stack

```bash
export $(cat .env | xargs)
./deploy-swarm.sh
```

> Builds all 6 services, pushes to local registry, deploys the Swarm stack. Takes ~2–3 minutes first run.

---

### Step 7 — Monitor until healthy

```bash
docker service ls
```

Wait until all services show `N/N` replicas. Takes ~60–90 seconds.

---

### Step 8 — Check health endpoints

```bash
curl http://localhost:8080/health
curl http://localhost:8003/health
```

### Step 9 — Confirm catch-up sync ran

```bash
docker service logs traffic-service_conflict-service 2>&1 | grep -E "sync|peer" | tail -10
```

> Expected: `[sync] CATCH-UP from peer=http://<A_IP>:8003 complete: applied=N total=N`
> If it says `unreachable` — Laptop A is not reachable yet. Check firewall (Step 10).

---

### Step 10 — Allow incoming connections (if needed)

```
System Settings → Network → Firewall → Options
→ Make sure "Block all incoming connections" is OFF
```

### Step 11 — Verify Laptop A can reach you

Run this **on Laptop A**:
```bash
curl http://<LAPTOP_B_IP>:8080/health
curl http://<LAPTOP_B_IP>:8003/health
```

### Step 12 — Open the frontend

```
http://localhost:3000
```

Hard-refresh (`Cmd+Shift+R`) after opening.

---
---

## API Gateway Reference

All browser API calls go through HAProxy → Nginx on port **8080**.

| Service | Endpoint |
|---------|----------|
| Users | `http://localhost:8080/api/users/` |
| Journeys | `http://localhost:8080/api/journeys/` |
| Conflicts | `http://localhost:8080/api/conflicts/` |
| Notifications | `http://localhost:8080/api/notifications/` |
| Enforcement | `http://localhost:8080/api/enforcement/` |
| Analytics | `http://localhost:8080/api/analytics/` |
| Health | `http://localhost:8080/health` |
| Frontend | `http://localhost:3000` |

**View API gateway logs:**
```bash
docker service logs traffic-service_api-gateway-1 -f
docker service logs traffic-service_haproxy -f
```

---
---

## Testing on the Frontend — Step by Step

> Do these steps **simultaneously on both browsers** (side by side if possible).

---

### PART 1 — Account Setup (both laptops, ~2 min)

**On Laptop A browser:**

1. Go to `http://localhost:3000`
2. Click **Register** tab → fill in:
   - Full Name: `Alice Driver` | Email: `alice@test.com` | License: `LIC-001` | Password: `password123`
3. Click **Create Account** → switch to **Sign In** → log in
4. Go to **Journeys** tab → **My Vehicles → + Add**
   - Registration: `ALICE-01` | Type: `CAR` → **Register Vehicle**

**On Laptop B browser:**

1. Go to `http://localhost:3000`
2. Click **Register** → fill in:
   - Full Name: `Bob Driver` | Email: `bob@test.com` | License: `LIC-002` | Password: `password123`
3. Log in → **Journeys** tab → **My Vehicles → + Add**
   - Registration: `BOB-01` ← **must be different from Alice's** | Type: `CAR`

---

### PART 2 — Cross-Node Conflict Test

> Proves that a booking on Node A blocks the same slot on Node B.

**On BOTH browsers — set up the same journey:**
- **Quick Route** → `Dublin → Galway (M6)` ← same on both
- **Departure** → same date/time on both (e.g. tomorrow at 10:00)
- **Duration** → leave as auto-filled (135 min)
- **Vehicle** → your own vehicle
- **Protocol** → `Saga (default)`

**Fire:**
1. **Laptop A** submits first → ✅ green toast: `Journey booked! (CONFIRMED)`
2. Wait **2–3 seconds** (replication is async, usually <200ms on LAN)
3. **Laptop B** submits → ❌ red toast: `Rejected: Road segment fully booked at 10:00 UTC`

> Node A's booking was pushed to Node B's conflict-service DB via REST replication before Node B
> submitted. Two independent nodes, one consistent conflict state.

**Verify in the Activity Feed** (Simulate tab → Distributed Activity Feed):
- You should see `[replication] PUSH slot=... vehicle=ALICE-01 ... → peer=http://...` on Node A
- And `[replication] RECV slot=... vehicle=ALICE-01 ... — applying locally` on Node B

---

### PART 3 — Node Failure & Recovery Demo

> Demonstrates ALIVE → SUSPECT → DEAD health model.

**Register each other as health peers first:**

On Laptop A → **Simulate tab** → **Register Remote Peer**:
- Name: `bob-node` | Health URL: `http://<LAPTOP_B_IP>:8080/health` → **+ Add Peer**

On Laptop B → same:
- Name: `alice-node` | Health URL: `http://<LAPTOP_A_IP>:8080/health` → **+ Add Peer**

Both frontends show the peer card as `ALIVE` (auto-refreshes every 10s).

**Simulate a crash:**

On **Laptop A's** Simulate tab → **💀 Kill Node**
- Laptop A shows: `💀 FAILED`
- This kills **both** journey-service AND user-service on Node A — login and all booking operations return 503.

Watch **Laptop B's** peer grid:
- ~30s → `alice-node` → `SUSPECT`
- ~60s → `alice-node` → `DEAD`

**Recover:** Click **💚 Recover Node** → Laptop B shows `ALIVE` on next heartbeat (~10s).

---

### PART 4 — Seamless Node Failover Demo

> A user on Node A can still log in and book when Node A is dead — requests transparently route to Node B.

**Prerequisites:** Both nodes have each other registered as health peers (Part 3).

**Run the demo:**

1. On **Laptop A** → click **💀 Kill Node**
   - Node A's health → 503, login → 503, all booking operations → 503

2. On **Laptop A's browser** — try **any** of these:
   - Log out and log back in → login routes to Node B → succeeds
   - Go to Journeys → book a journey → books on Node B → `CONFIRMED`
   - The topbar shows: `⚡ Failover: <LAPTOP_B_IP>:8080`
   - Toast: `Node failover — now routing to <LAPTOP_B_IP>:8080`

3. Click **💚 Recover Node** → topbar returns to `🟢 Primary` on next call

> **Why this works:** Every API call uses `resilientFetch` which tries the primary node first.
> On 5xx or network error it tries each ALIVE peer in order. JWT tokens are signed with the same
> secret on all nodes, so a session from Node A is valid on Node B.

**What about live notifications (WebSocket)?**
After 2 consecutive WS failures on the dead primary, the browser automatically reconnects to the
peer's notification service. The `Live Data` dot may flicker grey for ~10s then go green again.

---

### PART 5 — Concurrent Booking Storm

> Proves serializable transaction locking prevents double-booking under load.

On **either laptop's** Simulate tab → **🌪️ Concurrent Booking Storm**
- 10 concurrent bookings fire at once for the same slot
- Expected: exactly 1 `CONFIRMED`, rest `REJECTED` — no double-booking

---

### PART 6 — Two-Phase Commit Demo

> Stronger consistency — atomic PREPARE → COMMIT across journey-service and conflict-service.

On **either laptop's** Simulate tab → **🔄 Two-Phase Commit Demo**
- Watch the log: `✅ 2PC COMMITTED — capacity reserved + journey confirmed atomically`

---

### PART 7 — Distributed Activity Feed

> Shows live log output from **all nodes** merged into one view with UTC timestamps.

On **either laptop's** Simulate tab → scroll down to **Distributed Activity Feed**:
- Auto-refreshes every 5s
- Shows logs from **this node AND all registered peer nodes**
- Node hostname column identifies which machine produced each log line
- Colour coding:
  - `[replication]` purple — cross-node slot push/receive
  - `[sync]` blue — catch-up sync activity
  - `CONFIRMED` green / `REJECTED` red — booking outcomes
  - `PUSH` purple — this node sending a slot to a peer
  - `RECV` blue — this node receiving a slot from a peer
  - `SIMULATION` red — node failure/recovery events

**To see cross-node replication in real time:**
Make a booking on Laptop A while watching the feed on Laptop B. Within 1–2 seconds you'll see:
```
Node-A  [replication] PUSH slot=<id> vehicle=ALICE-01 → peer=http://<B>:8003 (HTTP 204)
Node-B  [replication] RECV slot=<id> vehicle=ALICE-01 — applying locally
```

---

## Verifying Replication at the Database Level

> In Swarm mode, container names are auto-generated. Use `docker ps --filter` to find them.

```bash
# Find the conflicts container name
docker ps --filter "name=traffic-service_postgres-conflicts" --format "{{.Names}}"
```

### conflicts_db — key database for replication verification

```bash
CONFLICTS_CTR=$(docker ps --filter "name=traffic-service_postgres-conflicts\." --format "{{.Names}}" | head -1)

# All active booking slots (run on BOTH nodes — should match after replication)
docker exec $CONFLICTS_CTR psql -U conflicts_user -d conflicts_db \
  -c "SELECT journey_id, vehicle_registration, departure_time, arrival_time, is_active, created_at
      FROM booked_slots ORDER BY created_at DESC LIMIT 20;"

# Count active slots (should be IDENTICAL on both nodes within ~1s of a booking)
docker exec $CONFLICTS_CTR psql -U conflicts_user -d conflicts_db \
  -c "SELECT COUNT(*) AS total, SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active FROM booked_slots;"

# Road segment capacity
docker exec $CONFLICTS_CTR psql -U conflicts_user -d conflicts_db \
  -c "SELECT round(grid_lat::numeric,3), round(grid_lng::numeric,3), time_slot_start, current_bookings, max_capacity
      FROM road_segment_capacity ORDER BY time_slot_start DESC LIMIT 20;"
```

### journeys_db — bookings made on THIS node

```bash
JOURNEYS_CTR=$(docker ps --filter "name=traffic-service_postgres-journeys\." --format "{{.Names}}" | head -1)

docker exec $JOURNEYS_CTR psql -U journeys_user -d journeys_db \
  -c "SELECT id, vehicle_registration, status, departure_time, route_id, created_at
      FROM journeys ORDER BY created_at DESC LIMIT 20;"
```

### users_db

```bash
USERS_CTR=$(docker ps --filter "name=traffic-service_postgres-users\." --format "{{.Names}}" | head -1)

# Registered users
docker exec $USERS_CTR psql -U users_user -d users_db \
  -c "SELECT id, email, full_name, role, created_at FROM users ORDER BY created_at DESC;"

# Vehicles with owner email
docker exec $USERS_CTR psql -U users_user -d users_db \
  -c "SELECT v.registration, v.vehicle_type, u.email FROM vehicles v JOIN users u ON v.user_id=u.id;"
```

### analytics_db

```bash
ANALYTICS_CTR=$(docker ps --filter "name=traffic-service_postgres-analytics\." --format "{{.Names}}" | head -1)

docker exec $ANALYTICS_CTR psql -U analytics_user -d analytics_db \
  -c "SELECT event_type, COUNT(*) FROM event_logs GROUP BY event_type ORDER BY count DESC;"
```

### How to confirm replication is working

1. Make a booking on **Laptop A**
2. Wait 2–3 seconds
3. Run the `booked_slots` query on **Laptop B** — Alice's booking should appear there
4. Run the count query on both — numbers should match

If Laptop B's count is lower, replication hasn't arrived yet (or `PEER_CONFLICT_URLS` wasn't set correctly in `.env`).

---

## Troubleshooting

| Problem | Command to diagnose | Fix |
|---------|---------------------|-----|
| Laptop B can't reach Laptop A | `curl http://<A_IP>:8080/health` from B's terminal | Disable macOS Firewall on Laptop A |
| Service stuck at `0/N` replicas | `docker service ps traffic-service_<name> --no-trunc` | Check logs: `docker service logs traffic-service_<name>` |
| No conflict after cross-node booking | Activity Feed → look for `[replication]` lines | Check `.env` was exported before `docker stack deploy` |
| Peer shows DEAD immediately | `curl --connect-timeout 5 http://<IP>:8080/health` | Firewall blocking — allow ports 8003 and 8080 |
| Catch-up sync shows unreachable | `docker service logs traffic-service_conflict-service \| grep sync` | Peer not started yet — register peer manually (see below) |
| Quick Route dropdown empty | Click away from Journeys tab and back | Conflict-service wasn't ready at login — auto-retries on tab switch |
| Live Data dot grey | Normal during reconnect (~5–30s) | Doesn't affect bookings — WS reconnects automatically to peer |
| Failover not working | Check Simulate tab — are peers `ALIVE`? | Register health peers first (Part 3), then try again |
| `depends_on must be a list` error | — | Already fixed in `docker-compose.swarm.yml` |
| Port 5000 conflict on registry | `docker service ls \| grep registry` | Registry already running — the `registry` block was removed from the swarm file |

**Force a catch-up sync manually (without restart):**
```bash
curl -X POST http://localhost:8003/internal/peers/register \
  -H "Content-Type: application/json" \
  -d '{"peer_url": "http://<PEER_IP>:8003"}'
```
> Registers the peer live and immediately pulls all their active slots. No restart needed.

**Check replication logs:**
```bash
docker service logs traffic-service_conflict-service 2>&1 | grep -E "replication|sync" | tail -20
```

**View raw logs from the API:**
```bash
curl http://localhost:8003/admin/logs | python3 -m json.tool | grep '"msg"' | tail -20
curl http://localhost:8080/admin/logs -H "Authorization: Bearer <token>" | python3 -m json.tool | grep '"msg"' | tail -20
```

---

## Port Reference

| Port | Service | Used for |
|------|---------|----------|
| `3000` | Frontend | Browser UI |
| `8080` | HAProxy → Nginx gateway | All API calls from the browser |
| `8003` | Conflict-service (direct) | Cross-node replication between laptops |
| `5000` | Local Docker registry | Swarm image distribution (internal) |
| `5672` | RabbitMQ | Internal only |
| `15672` | RabbitMQ UI | `http://localhost:15672` — user: `journey_admin` / `journey_pass` |
