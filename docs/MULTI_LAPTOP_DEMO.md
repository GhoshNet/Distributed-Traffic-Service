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
- macOS Firewall off, or ports 8003 and 8080 allowed

---

## LAPTOP A — Your Machine (stack already running)

### Step 1 — Stop anything on port 8080
```bash
docker stop excercise2-haproxy-1 2>/dev/null; true
```
> Stops HAProxy if it grabbed port 8080 (happens in full mode).

---

### Step 2 — Set the peer's conflict-service URL
```bash
export PEER_CONFLICT_URLS=http://<LAPTOP_B_IP>:8003
```
> Tells your conflict-service where to push replicated bookings.  
> Replace `<LAPTOP_B_IP>` with Laptop B's actual IP.

---

### Step 3 — Restart conflict-service with the new peer URL
```bash
docker compose \
  -f /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/docker-compose.yml \
  -f /Users/tanmay/Documents/TCD_Course_Material/DS/Excercise2/docker-compose.slim.yml \
  up -d --no-build conflict-service
```
> Recreates the conflict-service container with the `PEER_CONFLICT_URLS` env var injected.

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

---

### Step 5 — Confirm everything is healthy
```bash
curl http://localhost:8080/health
```
> Should return `{"status":"healthy",...}` — gateway is up and routing correctly.

```bash
curl http://localhost:8003/health
```
> Should return `{"status":"healthy",...}` — conflict-service is up directly.

```bash
docker logs excercise2-conflict-service-1 2>&1 | grep -E "peer|sync"
```
> Should show: `Cross-node replication enabled — peers: [http://<LAPTOP_B_IP>:8003]`

---

### Step 6 — Open the frontend
```
http://localhost:3000
```
> Open this in your browser. The app auto-connects to your own backend.

---
---

## LAPTOP B — Peer Machine (fresh setup)

### Step 1 — Clone the repository
```bash
git clone https://github.com/GhoshNet/Distributed-Traffic-Service.git Excercise2
```
> Downloads the full project from GitHub.

---

### Step 2 — Go into the project folder
```bash
cd Excercise2
```
> Moves into the project directory.

---

### Step 3 — Switch to the correct branch
```bash
git checkout approach3
```
> Switches to the branch with all the distributed systems features.

---

### Step 4 — Set the peer URL pointing back to Laptop A
```bash
export PEER_CONFLICT_URLS=http://<LAPTOP_A_IP>:8003
```
> Tells this conflict-service to replicate bookings to Laptop A.  
> Replace `<LAPTOP_A_IP>` with Laptop A's actual IP.

---

### Step 5 — Build the conflict-service image
```bash
docker compose -f docker-compose.yml -f docker-compose.slim.yml build conflict-service
```
> Compiles the Go conflict-service binary with the replication code baked in. Takes ~30s.

---

### Step 6 — Start the full stack
```bash
docker compose -f docker-compose.yml -f docker-compose.slim.yml up -d
```
> Starts all 6 microservices + databases + RabbitMQ + Redis + nginx in the background.

---

### Step 7 — Wait for services to become healthy (~30 seconds)
```bash
watch docker ps
```
> Shows live container status. Wait until all containers say `healthy` (not `starting`).  
> Press `Ctrl+C` to exit watch.

---

### Step 8 — Check all services are up
```bash
curl http://localhost:8080/health
```
> Gateway health check — should return `{"status":"healthy"}`.

```bash
curl http://localhost:8003/health
```
> Conflict-service direct health check.

---

### Step 9 — Confirm catch-up sync ran
```bash
docker logs $(docker ps -qf "name=conflict-service" | head -1) 2>&1 | grep -E "sync|peer"
```
> Should show the startup sync result, e.g.:  
> `[sync] catch-up from http://<LAPTOP_A_IP>:8003 complete: X/X slots applied`  
> If it says `unreachable` — Laptop A is not reachable yet. Check firewall (Step 10).

---

### Step 10 — Allow incoming connections through macOS Firewall (if needed)
```
System Settings → Network → Firewall → Options
→ Make sure "Block all incoming connections" is OFF
```
> macOS firewall can block Docker's exposed ports from other machines on the network.  
> If unsure, temporarily turn the firewall off for the demo.

---

### Step 11 — Verify Laptop A can reach you
Run this **on Laptop A**:
```bash
curl http://<LAPTOP_B_IP>:8080/health
curl http://<LAPTOP_B_IP>:8003/health
```
> Both should return healthy JSON. If they fail, check firewall on Laptop B.

---

### Step 12 — Open the frontend
```
http://localhost:3000
```
> Open in Laptop B's browser. It auto-connects to Laptop B's own backend.

---
---

## Testing on the Frontend — Step by Step

> Do these steps **simultaneously on both browsers** (side by side if possible).

---

### PART 1 — Account Setup (both laptops, ~2 min)

**On Laptop A browser:**

1. Go to `http://localhost:3000`
2. Click **Register** tab
3. Fill in:
   - Full Name: `Alice Driver`
   - Email: `alice@test.com`
   - License: `LIC-001`
   - Password: `password123`
4. Click **Create Account**
5. Switch to **Sign In** tab, log in with those credentials
6. Go to **Journeys** tab (🛣️ in sidebar)
7. Click **My Vehicles → + Add**
   - Registration: `ALICE-01`
   - Type: `CAR`
   - Click **Register Vehicle**

**On Laptop B browser:**

1. Go to `http://<LAPTOP_B_IP>:3000`
2. Click **Register** tab
3. Fill in:
   - Full Name: `Bob Driver`
   - Email: `bob@test.com`
   - License: `LIC-002`
   - Password: `password123`
4. Click **Create Account**
5. Switch to **Sign In** tab, log in
6. Go to **Journeys** tab
7. Click **My Vehicles → + Add**
   - Registration: `BOB-01`  ← must be different from Alice's
   - Type: `CAR`
   - Click **Register Vehicle**

---

### PART 2 — Cross-Node Conflict Test

> This proves that a booking on Laptop A is visible to Laptop B's conflict-service.

**On BOTH browsers — set up the same journey:**

In the **Book New Journey** form:
- **Quick Route** → select `Dublin → Galway (M6)`  ← same on both!
- **Departure** → pick the same date/time on both, e.g. `tomorrow at 10:00`
- **Duration** → leave as `135` (auto-filled)
- **Vehicle** → select your own vehicle (`ALICE-01` / `BOB-01`)
- **Protocol** → `Saga (default)`

**Fire the bookings:**

1. **Laptop A** clicks **Submit Request** first
   - ✅ Expected: green toast — `Journey booked successfully!` with status `CONFIRMED`

2. Wait **2–3 seconds** (replication is async, usually <200ms on LAN)

3. **Laptop B** clicks **Submit Request**
   - ❌ Expected: red toast — `Rejected: Road segment (XX.XX, XX.XX) is fully booked at 10:00 UTC`

> **What just happened:** Laptop A's booking was replicated to Laptop B's conflict-service database  
> before Laptop B submitted. When Laptop B's conflict-service checked, it found Laptop A's slot  
> and rejected the duplicate booking. Two independent nodes, one consistent conflict state.

---

### PART 3 — Node Failure & Recovery Demo

> This demonstrates the ALIVE → SUSPECT → DEAD health model.

**Register each other as health peers first:**

On Laptop A's frontend → **Simulate tab** (⚡ in sidebar):
- Scroll to **Register Remote Peer (Multi-Device)**
- Name: `bob-node`
- Health URL: `http://<LAPTOP_B_IP>:8080/health`
- Click **+ Add Peer**

On Laptop B's frontend → same section:
- Name: `alice-node`
- Health URL: `http://<LAPTOP_A_IP>:8080/health`
- Click **+ Add Peer**

**Verify both see each other:**
- Both frontends should show the peer card with status `ALIVE` in the Simulate tab (auto-refreshes every 10s)

**Simulate a crash:**

On **Laptop A's** Simulate tab:
- Click **💀 Kill Node**
- ✅ Laptop A shows: `💀 FAILED`

Watch **Laptop B's** Simulate tab (auto-refreshes every 10s):
- After ~30s: `alice-node` changes to `SUSPECT`
- After ~60s: `alice-node` changes to `DEAD`

**Recover:**

On **Laptop A's** Simulate tab:
- Click **💚 Recover Node**
- Watch Laptop B: `alice-node` returns to `ALIVE` on the next heartbeat (~10s)

---

### PART 4 — Concurrent Booking Storm (single node demo)

> Shows the serializable transaction locking working under concurrent load.

On **either laptop's** Simulate tab:
- Click **🌪️ Concurrent Booking Storm**
- Watch the simulation log — 10 concurrent bookings fire at once
- Expected: some `CONFIRMED`, rest `REJECTED` — no double-booking allowed

---

### PART 5 — Two-Phase Commit Demo

> Shows the stronger consistency protocol.

On **either laptop's** Simulate tab:
- Click **🔄 Two-Phase Commit Demo**
- Watch the log: should show `✅ 2PC COMMITTED — capacity reserved + journey confirmed atomically`

---

## Troubleshooting

| Problem | Command to diagnose | Fix |
|---------|---------------------|-----|
| Laptop B can't reach Laptop A | `curl http://<A_IP>:8080/health` from B's terminal | Disable macOS Firewall on Laptop A |
| Port 8080 already allocated | `docker ps \| grep 8080` | `docker stop excercise2-haproxy-1` |
| No conflict after cross-node booking | `docker logs <conflict-container> \| grep replication` | Check `PEER_CONFLICT_URLS` was exported before `up -d` |
| Peer shows DEAD immediately | `curl --connect-timeout 5 http://<IP>:8080/health` | Firewall blocking — allow ports 8003 and 8080 |
| Catch-up sync shows unreachable | `docker logs <conflict-container> \| grep sync` | Peer not started yet — run sync manually (see below) |
| Services not healthy after `up -d` | `docker ps` | Wait 30s more; check `docker logs <service>` for errors |

**Force a catch-up sync manually (without restart):**
```bash
curl -X POST http://localhost:8003/internal/peers/register \
  -H "Content-Type: application/json" \
  -d '{"peer_url": "http://<PEER_IP>:8003"}'
```
> Registers the peer live and immediately pulls all their active slots. No restart needed.

**Check replication is flowing:**
```bash
docker logs $(docker ps -qf "name=conflict-service" | head -1) 2>&1 | tail -20
```
> Look for lines like `[replication] slot <id> → http://... (HTTP 204)` after a booking.

---

## Port Reference

| Port | Service | Used for |
|------|---------|----------|
| `3000` | Frontend | Browser UI |
| `8080` | Nginx gateway | All API calls from the browser |
| `8003` | Conflict-service (direct) | Cross-node replication between laptops |
| `5672` | RabbitMQ | Internal only |
| `15672` | RabbitMQ UI | `http://localhost:15672` — user: `journey_admin` / `journey_pass` |
