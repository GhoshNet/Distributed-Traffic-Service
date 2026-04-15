# Multi-Laptop Demo Setup Guide
**CS7NS6 — Distributed Journey Booking System**

---

## Before You Start

Run this on **every laptop** to get your IP:

```bash
ipconfig getifaddr en0
```

Share all IPs with the group and write them down:
- **Laptop A:** `_______________`
- **Laptop B:** `_______________`
- **Laptop C:** `_______________` *(if applicable)*

**Requirements on every machine:**
- Docker Desktop installed and running
- All laptops on the **same Wi-Fi / hotspot**
- macOS Firewall off, or ports **3000**, **8003**, and **8080** allowed
- Repository cloned and on branch `approach3`

```bash
git checkout approach3 && git pull
```

---

## Understanding the UI Indicators

| Indicator | What it means | Does it affect bookings? |
|-----------|--------------|--------------------------|
| `● Live Data` (green dot) | WebSocket connection for push notifications. Goes grey while reconnecting. | **No.** Bookings work whether green or grey. |
| `🟢 Primary` | Your own backend is handling requests. Each user sees their **own** node as Primary — this is correct. | Yes — switches to `⚡ Failover: <peer-ip>` when your node is down. |

**If the Quick Route dropdown is empty:** Conflict-service wasn't ready at login. Fix: click away to another tab and back to **Journeys** — it retries automatically.

---

## Setup — Every Laptop (same steps, different IPs)

### Step 1 — Start the stack with peers

Run `start.sh` with the IPs of **all other laptops** as arguments:

```bash
# 2 laptops — Laptop A runs this (replace with Laptop B's actual IP):
./start.sh <LAPTOP_B_IP>

# 3 laptops — Laptop A runs this:
./start.sh <LAPTOP_B_IP> <LAPTOP_C_IP>

# Standalone (no peers, single-node testing):
./start.sh
```

> `start.sh` handles everything: tears down stale containers, frees conflicting ports,
> clears the RabbitMQ volume, writes `.env` with peer URLs, builds if needed, and starts
> the stack. It waits until the gateway is healthy before returning.

The script prints when it's ready:
```
[OK]    Gateway healthy (HTTP 200) after 30s
```

---

### Step 2 — Register peers (once all laptops are up)

Wait until **every laptop** has finished `start.sh`, then run on each machine:

```bash
./register_peers.sh
```

> This script reads the `.env` written by `start.sh` and registers all peers with
> the health monitor, conflict-service (triggers catch-up sync), journey-service,
> and user-service — all in one shot. Safe to run multiple times.

Expected output per peer:
```
[OK]    Health peer laptop-B (172.20.10.12) registered
[OK]    Conflict peer laptop-B (172.20.10.12:8003) registered + catch-up triggered
[OK]    Journey peer laptop-B (172.20.10.12:8080) registered + catch-up triggered
[OK]    User peer laptop-B (172.20.10.12:8080) registered + catch-up triggered
```

---

### Step 3 — Verify everything is connected

```bash
# Gateway healthy
curl http://localhost:8080/health

# Conflict-service healthy + shows peer
curl http://localhost:8003/health

# Replication sync happened
docker logs $(docker ps -qf "name=conflict-service") 2>&1 | grep -E "sync|peer" | tail -5
# Expected: [sync] CATCH-UP from peer=http://<PEER_IP>:8003 complete: applied=N total=N

# Peer health status
curl -s http://localhost:8080/health/nodes | python3 -m json.tool
```

---

### Step 4 — Open the frontend

```
http://localhost:3000
```

Hard-refresh first (`Cmd+Shift+R`) to clear any cached JS.

---

## Restarting / Resetting

If anything goes wrong or you want a clean slate, just run `start.sh` again:

```bash
./start.sh <PEER_IP1> <PEER_IP2>
```

It always does a full teardown before starting — no manual cleanup needed.

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

> **Note:** If you ran `./register_peers.sh` in Step 2, peers are already registered — skip to "Simulate a crash".

**If not already registered** — on each laptop's Simulate tab → **Register Remote Peer**:
- On Laptop A: Name `bob-node` | Health URL `http://<LAPTOP_B_IP>:8080/health` → **+ Add Peer**
- On Laptop B: Name `alice-node` | Health URL `http://<LAPTOP_A_IP>:8080/health` → **+ Add Peer**

Both frontends show the peer card as `ALIVE` (auto-refreshes every 10s).

**Simulate a crash:**

On **Laptop A's** Simulate tab → **💀 Kill Node**
- Laptop A shows: `💀 FAILED`
- This kills **both** journey-service AND user-service on Node A — all operations return 503.

Watch **Laptop B's** peer grid:
- ~30s → `alice-node` → `SUSPECT`
- ~60s → `alice-node` → `DEAD`

**Recover:** Click **💚 Recover Node** → Laptop B shows `ALIVE` on next heartbeat (~10s).

---

### PART 4 — Seamless Node Failover Demo

> A user on Node A can still log in and book when Node A is dead — requests transparently route to Node B.

**Prerequisites:** Both nodes have each other registered as health peers (Part 3 or `register_peers.sh`).

**Run the demo:**

1. On **Laptop A** → click **💀 Kill Node**
   - Node A's health → 503, login → 503, all booking operations → 503

2. On **Laptop A's browser** — try **any** of these:
   - Log out and log back in → login routes to Node B → succeeds
   - Go to Journeys → book a journey → books on Node B → `CONFIRMED`
   - The topbar shows: `⚡ Failover: <LAPTOP_B_IP>:8080`
   - Toast: `Node failover — now routing to <LAPTOP_B_IP>:8080`

3. Click **💚 Recover Node** → topbar returns to `🟢 Primary` on next call

> **Why this works:** Every API call uses `resilientFetch` — tries primary first, then each ALIVE peer
> on 5xx or network error. JWT tokens are signed with the same secret on all nodes, so a session from
> Node A is valid on Node B. The peer list is persisted in `localStorage` so failover works even at
> the login screen.

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
Make a booking on Laptop A while watching the feed on Laptop B. Within 1–2 seconds:
```
Node-A  [replication] PUSH slot=<id> vehicle=ALICE-01 → peer=http://<B>:8003 (HTTP 204)
Node-B  [replication] RECV slot=<id> vehicle=ALICE-01 — applying locally
```

---

## Verifying Replication at the Database Level

### conflicts_db — key database for replication verification

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

### journeys_db

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

---

## Troubleshooting

| Problem | Diagnosis | Fix |
|---------|-----------|-----|
| Port conflict on startup | `start.sh` normally handles this automatically | Re-run `./start.sh <peer-ips>` |
| Peer shows DEAD immediately | `curl http://<IP>:8080/health` from your terminal | macOS Firewall blocking — turn it off |
| No replication after cross-node booking | Activity Feed → look for `[replication]` lines | Re-run `./register_peers.sh` |
| Catch-up sync shows unreachable | `docker logs $(docker ps -qf 'name=conflict-service') \| grep sync` | Peer not up yet — wait and re-run `./register_peers.sh` |
| Quick Route dropdown empty | Click away from Journeys tab and back | Conflict-service wasn't ready at login — auto-retries |
| Services not healthy after start | `docker ps` | Wait 30s; check `docker logs <service>` |
| Live Data dot grey | Normal during reconnect (~5–30s) | WS reconnects automatically — doesn't affect bookings |
| Failover not working | Simulate tab — are peers `ALIVE`? | Run `./register_peers.sh`, then retry |
| Need a completely clean restart | — | `./start.sh <peer-ips>` — always does full teardown |

**Force catch-up sync manually:**
```bash
./register_peers.sh
```

**Check replication logs:**
```bash
docker logs $(docker ps -qf "name=conflict-service" | head -1) 2>&1 | grep -E "replication|sync" | tail -20
```

**View raw logs from the API:**
```bash
curl http://localhost:8003/admin/logs | python3 -m json.tool | grep '"msg"' | tail -20
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
