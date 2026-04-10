# Multi-Laptop Demo Setup Guide
**CS7NS6 — Distributed Journey Booking System**

---

## Before You Start

> Run this on **both laptops** to get your IPs, then fill them in below.

```bash
ipconfig getifaddr en0
```

Write them down:
- **Your laptop (A):** `_______________` (e.g. `172.20.10.10`)
- **Peer laptop (B):**  `_______________` (e.g. `172.20.10.12`)

**Requirements on both machines:**
- Docker Desktop installed and running
- Both laptops on the **same Wi-Fi / hotspot**
- macOS Firewall off, or ports **8003** and **8080** allowed

---

## Understanding the UI Indicators

Before you start testing, know what the two topbar indicators mean:

| Indicator | What it means | Does it affect bookings? |
|-----------|--------------|--------------------------|
| `● Live Data` (green dot) | WebSocket connection for push notifications (toasts + live map). Goes grey while reconnecting. | **No.** Bookings work whether this is green or grey. |
| `🟢 Primary` | Your own backend node is handling your requests. Each user sees their **own** node as Primary — this is correct, not shared. | Yes — switches to `⚡ Failover: <peer-ip>` when your node is down and requests are routed to the peer. |

**If the Quick Route dropdown is empty:** The routes failed to load at login (conflict-service wasn't ready yet). Fix: click away to another tab and back to **Journeys** — it retries automatically

---

## LAPTOP A — Your Machine (stack already running)

### Step 1 — Stop anything on port 8080
```bash
docker stop excercise2-haproxy-1 2>/dev/null; true
```
> Stops HAProxy if it grabbed port 8080 (happens in full mode).

---

### Step 2 — Write the peer URLs into the .env file
```bash
cat > /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/.env <<EOF
PEER_CONFLICT_URLS=http://<LAPTOP_B_IP>:8003
PEER_USER_URLS=http://<LAPTOP_B_IP>:8080
EOF
```
> Replace `<LAPTOP_B_IP>` with Laptop B's actual IP.  
> Docker Compose loads `.env` automatically on every `docker compose` command — so you only set this once.

---

### Step 3 — Restart conflict-service and user-service with the new peer URLs
```bash
docker compose \
  -f /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/docker-compose.yml \
  -f /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/docker-compose.slim.yml \
  up -d --no-build --force-recreate conflict-service user-service
```
> `--force-recreate` is required so Docker actually injects the new `.env` values
> (without it, compose sees no config diff and leaves the container unchanged).

---

### Step 4 — Recreate the nginx gateway (clears stale IPs)
```bash
docker rm -f excercise2-api-gateway-1-1 2>/dev/null; true

docker compose \
  -f /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/docker-compose.yml \
  -f /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/docker-compose.slim.yml \
  up -d --no-build api-gateway-1
```
> Nginx caches service IPs at startup — force-recreate so it picks up the new conflict-service IP.

### ⚠️ IMPORTANT — If port 8080 fails after gateway recreate

Recreating `api-gateway-1` can silently stop `frontend` and `notification-service`. Nginx crash-loops if `notification-service` is missing. **Always run this after any gateway recreate:**

```bash
docker compose \
  -f /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/docker-compose.yml \
  -f /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/docker-compose.slim.yml \
  up -d --no-build
```
> Starts any stopped containers without touching running ones. Safe to run anytime.

To confirm everything is back:
```bash
docker ps --format "table {{.Names}}\t{{.Status}}" | grep -E "frontend|notification|gateway"
```
> All three should show `Up`.

---

### Step 5 — Confirm everything is healthy
```bash
curl http://localhost:8080/health
curl http://localhost:8003/health
docker logs excercise2-conflict-service-1 2>&1 | grep -E "peer|sync|replication"
docker logs excercise2-user-service-1 2>&1 | grep -E "peer|sync|replication"
```
> Should show:
> - `Cross-node replication enabled — peers: [http://<LAPTOP_B_IP>:8003]`
> - `[user-replication] peers configured: ['http://<LAPTOP_B_IP>:8080']`
> - `[sync] CATCH-UP from peer=... complete: applied=N total=N`

---

### Step 6 — Open the frontend
```
http://localhost:3000
```
> Open this in your browser. Hard-refresh first (`Cmd+Shift+R`) to clear any cached JS.

---
---

## LAPTOP B — Peer Machine (fresh setup)

### Step 1 — Clone the repository
```bash
git clone https://github.com/GhoshNet/Distributed-Traffic-Service.git Excercise2
```

### Step 2 — Go into the project folder
```bash
cd Excercise2
```

### Step 3 — Switch to the correct branch
```bash
git checkout approach3
```

### Step 4 — Write the peer URLs into the .env file
```bash
cat > .env <<EOF
PEER_CONFLICT_URLS=http://<LAPTOP_A_IP>:8003
PEER_USER_URLS=http://<LAPTOP_A_IP>:8080
EOF
```
> Replace `<LAPTOP_A_IP>` with Laptop A's actual IP.

---

### Step 5 — Build the services
```bash
docker compose -f docker-compose.yml -f docker-compose.slim.yml build conflict-service journey-service
```
> Builds the two services that have distributed-systems logic. Takes ~60s.  
> `conflict-service` = Go binary with slot replication + log buffer.  
> `journey-service` = Python with failover simulation + log buffer.

---

### Step 6 — Start the full stack
```bash
docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d
```
> Starts all 6 microservices + databases + RabbitMQ + Redis + nginx.

---

### Step 7 — Wait for services to become healthy (~30 seconds)
```bash
watch docker ps
```
> Wait until all containers show `healthy`. Press `Ctrl+C` to exit.

---

### Step 8 — Check all services are up
```bash
curl http://localhost:8080/health
curl http://localhost:8003/health
```

### Step 9 — Confirm catch-up sync ran
```bash
docker logs $(docker ps -qf "name=conflict-service" | head -1) 2>&1 | grep -E "sync|peer"
```
> Expected: `[sync] CATCH-UP from peer=http://<A_IP>:8003 complete: applied=N total=N`  
> If it says `unreachable` — Laptop A is not reachable yet. Check firewall (Step 10).

---

### Step 10 — Allow incoming connections through macOS Firewall (if needed)
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
> Hard-refresh (`Cmd+Shift+R`) after opening.

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

1. Go to `http://<LAPTOP_B_IP>:3000`
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
   - The topbar shows: `⚡ Failover: 172.20.10.12:8080`
   - Toast: `Node failover — now routing to 172.20.10.12:8080`

3. Click **💚 Recover Node** → topbar returns to `🟢 Primary` on next call

> **Why this works:** Every API call (including login/register) uses `resilientFetch` which tries the
> primary node first. On 5xx or network error it tries each ALIVE peer in order. JWT tokens are
> signed with the same secret on all nodes, so a session from Node A is valid on Node B.
> The peer list is persisted in `localStorage` so failover works even at the login screen.

**What about live notifications (WebSocket)?**  
After 2 consecutive WS failures on the dead primary, the browser automatically reconnects to the
peer's notification service. The `Live Data` dot may flicker grey for ~10s then go green again
on the peer node.

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

> These commands connect directly to the PostgreSQL containers on each node.

### conflicts_db — the key database for replication verification

```bash
# All active booking slots (run on BOTH nodes — should match after replication)
docker exec excercise2-postgres-conflicts-1 psql -U conflicts_user -d conflicts_db \
  -c "SELECT journey_id, vehicle_registration, departure_time, arrival_time, is_active, created_at
      FROM booked_slots ORDER BY created_at DESC LIMIT 20;"

# Count active slots (should be IDENTICAL on both nodes within ~1s of a booking)
docker exec excercise2-postgres-conflicts-1 psql -U conflicts_user -d conflicts_db \
  -c "SELECT COUNT(*) AS total, SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active FROM booked_slots;"

# Road segment capacity (how full each grid cell is per time slot)
docker exec excercise2-postgres-conflicts-1 psql -U conflicts_user -d conflicts_db \
  -c "SELECT round(grid_lat::numeric,3), round(grid_lng::numeric,3), time_slot_start, current_bookings, max_capacity
      FROM road_segment_capacity ORDER BY time_slot_start DESC LIMIT 20;"
```

### journeys_db — bookings made on THIS node

```bash
docker exec excercise2-postgres-journeys-1 psql -U journeys_user -d journeys_db \
  -c "SELECT id, vehicle_registration, status, departure_time, route_id, created_at
      FROM journeys ORDER BY created_at DESC LIMIT 20;"
```

### users_db

```bash
# Registered users
docker exec excercise2-postgres-users-1 psql -U users_user -d users_db \
  -c "SELECT id, email, full_name, role, created_at FROM users ORDER BY created_at DESC;"

# Vehicles with owner email
docker exec excercise2-postgres-users-1 psql -U users_user -d users_db \
  -c "SELECT v.registration, v.vehicle_type, u.email FROM vehicles v JOIN users u ON v.user_id=u.id;"
```

### analytics_db

```bash
docker exec excercise2-postgres-analytics-1 psql -U analytics_user -d analytics_db \
  -c "SELECT event_type, COUNT(*) FROM event_logs GROUP BY event_type ORDER BY count DESC;"
```

### How to confirm replication is working

1. Make a booking on **Laptop A**
2. Wait 2–3 seconds
3. Run the `booked_slots` query on **Laptop B** — Alice's booking should appear there
4. Run the count query on both — numbers should match

If Laptop B's count is lower, replication hasn't arrived yet (or `PEER_CONFLICT_URLS` wasn't set).

---

## Troubleshooting

| Problem | Command to diagnose | Fix |
|---------|---------------------|-----|
| Laptop B can't reach Laptop A | `curl http://<A_IP>:8080/health` from B's terminal | Disable macOS Firewall on Laptop A |
| Port 8080 already allocated | `docker ps \| grep 8080` | `docker stop excercise2-haproxy-1` |
| No conflict after cross-node booking | Activity Feed → look for `[replication]` lines | Check `PEER_CONFLICT_URLS` was exported before `up -d` |
| Peer shows DEAD immediately | `curl --connect-timeout 5 http://<IP>:8080/health` | Firewall blocking — allow ports 8003 and 8080 |
| Catch-up sync shows unreachable | Activity Feed or `docker logs <conflict>` \| grep sync | Peer not started yet — register peer manually (see below) |
| Quick Route dropdown empty | Click away from Journeys tab and back | Conflict-service wasn't ready at login — auto-retries on tab switch |
| Services not healthy after `up -d` | `docker ps` | Wait 30s more; `docker logs <service>` for errors |
| Live Data dot grey | Normal during reconnect (~5–30s) | Doesn't affect bookings — WS reconnects automatically to peer if primary is down |
| Failover not working (still on primary node) | Check Simulate tab — are peers `ALIVE`? | Register health peers first (Part 3), then try again |

**Force a catch-up sync manually (without restart):**
```bash
curl -X POST http://localhost:8003/internal/peers/register \
  -H "Content-Type: application/json" \
  -d '{"peer_url": "http://<PEER_IP>:8003"}'
```
> Registers the peer live and immediately pulls all their active slots. No restart needed.

**Check replication logs:**
```bash
docker logs $(docker ps -qf "name=conflict-service" | head -1) 2>&1 | grep -E "replication|sync" | tail -20
```
> Look for lines like `[replication] PUSH slot=<id> vehicle=... → peer=http://... (HTTP 204)`

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
| `8080` | Nginx gateway | All API calls from the browser |
| `8003` | Conflict-service (direct) | Cross-node replication between laptops |
| `5672` | RabbitMQ | Internal only |
| `15672` | RabbitMQ UI | `http://localhost:15672` — user: `journey_admin` / `journey_pass` |
