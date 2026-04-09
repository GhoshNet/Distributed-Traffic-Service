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
resolver 127.0.0.11 valid=5s ipv6=off;
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

Internal only — called by Journey Service. Not exposed to users.

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
| Conflict detection | ✓ PASS | REJECTED with reason |
| Two-Phase Commit (2PC) | ✓ PASS | CONFIRMS when road capacity free |
| Journey listing (read replica) | ✓ PASS | Fixed by DATABASE_READ_URL fix |
| Enforcement verification | ✓ PASS | Returns `is_valid` field |
| Journey cancellation | ✓ PASS | Returns CANCELLED |
| Analytics stats | ✓ PASS | Returns daily counters |

**Note on enforcement timing**: Enforcement returns `is_valid=false` for journeys departing >30 min  
in the future — this is correct behavior, not a bug.

**Note on 2PC road capacity**: If many test runs have booked the Galway→Dublin route, road capacity  
(max 5 bookings per segment) fills up and 2PC correctly aborts. Use a different route or wait for  
scheduled journeys to complete.

---

## Key Things to Remember for Next Session

1. Always rebuild after code changes: `docker compose -f docker-compose.yml -f docker-compose.slim.yml build <service>`
2. After recreating any backend service, recreate the nginx gateway to flush stale IPs
3. `docker-compose.slim.yml` must use explicit primary DB URLs (not empty strings) for `DATABASE_READ_URL`
4. The `route_id` column was manually added to the live DB — any fresh `docker compose down -v && up` will need it again (it's in the ORM model but not in an init SQL)
5. Analytics endpoint is `/api/analytics/stats`, not `/summary`
6. Enforcement endpoint requires `ENFORCEMENT_AGENT` role — register via `/api/users/register/agent`
