# Geographic Partitioning — Detailed Change Specification

> **Goal:** Evolve the current single-region microservices deployment into a
> geo-partitioned architecture where each **city/country cluster** owns its local
> road grid, and cross-region journeys are coordinated via inter-cluster sagas.

---

## 1. Architecture Overview

```
                        ┌─────────────────────┐
                        │   Global DNS / LB    │
                        │  (GeoDNS or Anycast) │
                        └────┬───────┬────┬────┘
                             │       │    │
              ┌──────────────┘       │    └──────────────┐
              ▼                      ▼                   ▼
    ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
    │  Cluster: IE    │   │  Cluster: UK    │   │  Cluster: DE    │
    │  (Ireland)      │   │  (United Kingdom│   │  (Germany)      │
    │                 │   │                 │   │                 │
    │ ┌─────────────┐ │   │ ┌─────────────┐ │   │ ┌─────────────┐ │
    │ │ API Gateway │ │   │ │ API Gateway │ │   │ │ API Gateway │ │
    │ └──────┬──────┘ │   │ └──────┬──────┘ │   │ └──────┬──────┘ │
    │        │        │   │        │        │   │        │        │
    │ ┌──────┴──────┐ │   │ ┌──────┴──────┐ │   │ ┌──────┴──────┐ │
    │ │Journey Svc  │ │   │ │Journey Svc  │ │   │ │Journey Svc  │ │
    │ │Conflict Svc │ │   │ │Conflict Svc │ │   │ │Conflict Svc │ │
    │ │Enforce Svc  │ │   │ │Enforce Svc  │ │   │ │Enforce Svc  │ │
    │ │Notify  Svc  │ │   │ │Notify  Svc  │ │   │ │Notify  Svc  │ │
    │ │Analytics Svc│ │   │ │Analytics Svc│ │   │ │Analytics Svc│ │
    │ └──────┬──────┘ │   │ └──────┬──────┘ │   │ └──────┬──────┘ │
    │        │        │   │        │        │   │        │        │
    │ ┌──────┴──────┐ │   │ ┌──────┴──────┐ │   │ ┌──────┴──────┐ │
    │ │Postgres(local│ │   │ │Postgres(local│ │   │ │Postgres(local│ │
    │ │Redis Sentinel│ │   │ │Redis Sentinel│ │   │ │Redis Sentinel│ │
    │ │RabbitMQ     │ │   │ │RabbitMQ     │ │   │ │RabbitMQ     │ │
    │ └─────────────┘ │   │ └─────────────┘ │   │ └─────────────┘ │
    └────────┬────────┘   └────────┬────────┘   └────────┬────────┘
             │                     │                     │
             └─────────┬───────────┴─────────────────────┘
                       │
              ┌────────┴────────┐
              │  Inter-Cluster  │
              │  RabbitMQ Shovel│
              │  / Federation   │
              └─────────────────┘
                       │
              ┌────────┴────────┐
              │ Global Services │
              │ • User Service  │
              │ • Global Router │
              └─────────────────┘
```

**Key principle:** A journey whose origin AND destination fall within a single
region is handled entirely within that cluster. A journey that crosses region
boundaries triggers a **cross-region saga** coordinated between the origin and
destination clusters.

---

## 2. Region Registry — NEW Component

### 2.1 What It Is

A lightweight lookup service (or static config) that maps geographic bounding
boxes to cluster identifiers. Every cluster loads this on startup.

### 2.2 Files to Create

| File | Purpose |
|------|---------|
| `region-registry/regions.json` | Static region definitions |
| `region-registry/registry.py` | Lookup library: `(lat, lng) → region_id` |
| `region-registry/registry.go` | Go equivalent for conflict/notification/analytics services |

### 2.3 Data Model

```json
{
  "regions": [
    {
      "id": "IE",
      "name": "Ireland",
      "bounding_box": {
        "min_lat": 51.42, "max_lat": 55.43,
        "min_lng": -10.48, "max_lng": -5.99
      },
      "cluster_endpoint": "https://ie.traffic.example.com",
      "rabbitmq_federation_uri": "amqp://ie-cluster-rabbitmq:5672"
    },
    {
      "id": "UK",
      "name": "United Kingdom",
      "bounding_box": {
        "min_lat": 49.96, "max_lat": 58.64,
        "min_lng": -7.57, "max_lng": 1.68
      },
      "cluster_endpoint": "https://uk.traffic.example.com",
      "rabbitmq_federation_uri": "amqp://uk-cluster-rabbitmq:5672"
    }
  ]
}
```

### 2.4 Lookup Function

```python
def resolve_region(lat: float, lng: float) -> str | None:
    """Returns region_id for the region containing (lat, lng), or None."""
    for region in REGIONS:
        bb = region["bounding_box"]
        if bb["min_lat"] <= lat <= bb["max_lat"] and bb["min_lng"] <= lng <= bb["max_lng"]:
            return region["id"]
    return None
```

---

## 3. Changes to User Service

**Current:** Single instance, single Postgres DB (`postgres-users`).

**New:** User Service becomes a **global service** — it is NOT partitioned by
region. Users are global entities (a driver from Ireland may book a journey in
Germany).

### 3.1 File Changes

| File | Change | Reason |
|------|--------|--------|
| `user-service/app/database.py` | Add `home_region` column (String[10]) to `User` model | Default routing preference; not a hard partition key |
| `user-service/app/service.py:30` | Set `home_region` from origin coordinates at registration | Users know their base region |
| `user-service/app/routes.py:35` | Accept optional `home_region` in registration payload | Frontend can auto-detect |
| `docker-compose.yml` | User service stays as-is (single global instance) | Global service, not partitioned |

### 3.2 Schema Change

```python
# user-service/app/database.py — Add to User model (after line 64)
home_region = Column(String(10), nullable=True, index=True)
```

### 3.3 What Does NOT Change

- JWT auth flow — unchanged
- Vehicle registration — unchanged
- Password hashing — unchanged
- Read/write replica routing — unchanged

---

## 4. Changes to Journey Service

**Current:** Single instance at `:8002`, single Postgres DB (`postgres-journeys`),
calls conflict-service synchronously.

**New:** One journey-service instance **per region**, each with its own Postgres DB.
The journey-service now routes requests based on where the journey is located.

### 4.1 File Changes

| File | Lines | Change | Detail |
|------|-------|--------|--------|
| `journey-service/app/database.py:50-81` | Journey model | Add `region` column | `Column(String(10), nullable=False, index=True)` — set from origin coords |
| `journey-service/app/database.py:50-81` | Journey model | Add `is_cross_region` column | `Column(Boolean, default=False)` — True when origin and destination are in different regions |
| `journey-service/app/database.py:50-81` | Journey model | Add `destination_region` column | `Column(String(10), nullable=True)` — only set for cross-region journeys |
| `journey-service/app/service.py:34-123` | `create_journey()` | Add region resolution | Call `resolve_region(origin_lat, origin_lng)` before creating journey |
| `journey-service/app/service.py:34-123` | `create_journey()` | Add cross-region detection | Compare `origin_region != destination_region`; if different, flag `is_cross_region=True` |
| `journey-service/app/service.py:34-123` | `create_journey()` | Add cross-region saga trigger | If cross-region, call `CrossRegionSaga.execute()` instead of `BookingSaga.execute()` |
| `journey-service/app/saga.py` | Entire file | Add `CrossRegionSaga` class | New saga that calls BOTH the local conflict-service AND the remote region's conflict-service |
| `journey-service/app/outbox_publisher.py` | Event payload | Add `region` field to all events | Consumers need to know which region an event belongs to |
| `journey-service/app/scheduler.py:24-82` | `transition_journeys()` | Filter by region | Only transition journeys owned by this region instance |
| `journey-service/app/main.py` | Startup | Load `REGION_ID` from env var | Each instance knows its own region |
| `journey-service/app/routes.py:31-42` | `create_journey()` | Add region routing guard | If origin is NOT in this instance's region, return `HTTP 307 Redirect` to correct cluster |

### 4.2 New File: `journey-service/app/cross_region_saga.py`

This is the most critical new component. It handles journeys that span two regions.

```python
class CrossRegionSaga:
    """
    Orchestrates a journey that crosses region boundaries.
    
    Steps:
    1. Reserve capacity in ORIGIN region's conflict-service (local call)
    2. Reserve capacity in DESTINATION region's conflict-service (remote HTTP call)
    3. If both succeed → CONFIRMED
    4. If step 2 fails → compensate step 1 (cancel origin reservation)
    5. If step 1 fails → REJECTED (no compensation needed)
    """
    
    async def execute(self, journey, origin_region, dest_region):
        # Step 1: Check origin segment (local)
        origin_result = await self._check_local_conflicts(journey, origin_region)
        if origin_result.is_conflict:
            return JourneyStatus.REJECTED, origin_result.conflict_details
        
        # Step 2: Check destination segment (remote)
        dest_cluster_url = get_cluster_endpoint(dest_region)
        try:
            dest_result = await self._check_remote_conflicts(
                journey, dest_cluster_url
            )
        except (TimeoutError, ConnectionError):
            # Compensate: cancel origin reservation
            await self._cancel_origin_reservation(journey.id)
            return JourneyStatus.REJECTED, "Destination region unavailable"
        
        if dest_result.is_conflict:
            await self._cancel_origin_reservation(journey.id)
            return JourneyStatus.REJECTED, dest_result.conflict_details
        
        return JourneyStatus.CONFIRMED, None
```

### 4.3 Journey Splitting for Cross-Region

When a journey crosses regions, it must be split into **segments** at the region
boundary. Each segment is checked independently against its region's conflict-service.

```
Dublin (IE) ───────────── Border ───────────── Belfast (UK)
        ↓                    ↓                      ↓
  IE conflict-service   boundary point    UK conflict-service
  checks IE grid cells                   checks UK grid cells
```

**New utility** in `journey-service/app/region_utils.py`:

```python
def split_journey_at_border(
    origin_lat, origin_lng,
    dest_lat, dest_lng,
    departure_time, duration_minutes,
    origin_region, dest_region
) -> tuple[JourneySegment, JourneySegment]:
    """
    Returns two JourneySegment objects:
    - origin_segment: origin → border crossing point
    - dest_segment:   border crossing point → destination
    
    The border crossing point is the intersection of the straight-line
    path with the region bounding box edge.
    """
```

### 4.4 Environment Variable Changes

```yaml
# Per-region journey-service instance
journey-service:
  environment:
    REGION_ID: "IE"                                    # NEW
    REGION_REGISTRY_PATH: "/app/regions.json"          # NEW
    CONFLICT_SERVICE_URL: http://conflict-service:8000 # unchanged (local)
    PEER_CLUSTERS: "UK=https://uk.traffic.example.com,DE=https://de.traffic.example.com"  # NEW
```

---

## 5. Changes to Conflict Service

**Current:** Single Go service at `:8003`, single Postgres DB (`postgres-conflicts`),
grid resolution 0.01 degrees, max capacity 1 per cell per 30-min slot.

**New:** One conflict-service instance **per region**, each owning ONLY its region's
road grid cells. The service rejects requests for coordinates outside its region.

### 5.1 File Changes

| File | Lines | Change | Detail |
|------|-------|--------|--------|
| `conflict-service/config.go:5-11` | Config struct | Add `RegionID` and `BoundingBox` fields | Loaded from env/config |
| `conflict-service/service.go:57-137` | `checkConflicts()` | Add region boundary validation | Reject requests where origin/destination are outside this region's bounding box |
| `conflict-service/service.go:208-237` | `pathGridCells()` | Clip path to region boundary | Only generate grid cells that fall within this region's bounding box |
| `conflict-service/service.go:263-290` | `checkRoadCapacity()` | No change needed | Already iterates cells from `pathGridCells()` — clipping upstream handles it |
| `conflict-service/service.go:321-337` | `incrementCapacity()` | No change needed | Same reason |
| `conflict-service/handlers.go:22-43` | `checkConflictsHandler()` | Add region validation middleware | Return `400` if coordinates are outside this region |
| `conflict-service/database.go:61-72` | `road_segment_capacity` table | Add `region` column | `VARCHAR(10) NOT NULL` — for clarity and future cross-region queries |
| `conflict-service/database.go:37-59` | `booked_slots` table | Add `region` column | Same reason |
| `conflict-service/consumer.go:122-150` | `handleEvent()` | Filter events by region | Only process `journey.cancelled` events where region matches this instance |

### 5.2 New: Region Boundary Clipping in `pathGridCells()`

```go
// conflict-service/service.go — modified pathGridCells()
func (s *Service) pathGridCells(
    originLat, originLng, destLat, destLng float64,
) []gridCell {
    cells := computeFullPath(originLat, originLng, destLat, destLng)
    
    // Clip to this region's bounding box
    var clipped []gridCell
    for _, c := range cells {
        if s.regionBBox.Contains(c.lat, c.lng) {
            clipped = append(clipped, c)
        }
    }
    return clipped
}
```

### 5.3 New Endpoint: `POST /api/conflicts/check-segment`

For cross-region sagas, the remote journey-service sends only the segment that
falls within this region. This is a thin wrapper around `checkConflicts()` but
accepts a pre-clipped coordinate pair.

```go
// conflict-service/handlers.go — new handler
func checkSegmentHandler(w http.ResponseWriter, r *http.Request) {
    // Same as checkConflictsHandler but:
    // 1. Accepts "segment_origin_lat/lng" and "segment_destination_lat/lng"
    //    (already clipped to this region by the caller)
    // 2. Validates coordinates are within this region's bounding box
    // 3. Delegates to checkConflicts() as normal
}
```

### 5.4 Environment Variable Changes

```yaml
conflict-service:
  environment:
    REGION_ID: "IE"
    REGION_BBOX_MIN_LAT: "51.42"
    REGION_BBOX_MAX_LAT: "55.43"
    REGION_BBOX_MIN_LNG: "-10.48"
    REGION_BBOX_MAX_LNG: "-5.99"
```

---

## 6. Changes to Notification Service

**Current:** Single Go service at `:8004`, in-memory WebSocket registry,
Redis-backed notification history.

**New:** One instance per region. WebSocket connections are region-local.
Cross-region notifications are forwarded via RabbitMQ federation.

### 6.1 File Changes

| File | Lines | Change | Detail |
|------|-------|--------|--------|
| `notification-service/consumer.go:122-150` | `handleEvent()` | Add region filtering | Only process events tagged with this region's ID |
| `notification-service/consumer.go:62-129` | WebSocket registry | No structural change | Users connect to their nearest region's notification service |
| `notification-service/redis.go:46-84` | Notification storage | Prefix keys with region | `notifications:{region}:{user_id}` instead of `notifications:{user_id}` |
| `notification-service/config.go` | Config struct | Add `RegionID` field | From env var |
| `notification-service/handlers.go:53-89` | `wsHandler()` | No change | Users connect to whichever cluster they're routed to |

### 6.2 Cross-Region Notification Forwarding

When a cross-region journey is confirmed, both the origin and destination
region need to know. The journey-service publishes the event to the local
RabbitMQ, and **RabbitMQ federation** forwards it to the other region.

No code change is needed for this — it is handled by RabbitMQ federation
configuration (see Section 10).

---

## 7. Changes to Enforcement Service

**Current:** Single Python service at `:8005`, Redis-first lookup with
journey-service API fallback.

**New:** One instance per region. Enforcement checks are local — a police officer
in Ireland queries the IE cluster.

### 7.1 File Changes

| File | Lines | Change | Detail |
|------|-------|--------|--------|
| `enforcement-service/app/service.py:51-216` | `verify_by_vehicle()` | Add region-aware Redis keys | Keys become `enforcement:{region}:{vehicle_reg}` |
| `enforcement-service/app/service.py:107-188` | `verify_by_license()` | Add cross-region fallback | If vehicle not found locally, query peer clusters (HTTP) |
| `enforcement-service/app/consumer.py:41-105` | `handle_journey_event()` | Filter by region | Only cache journeys belonging to this region |
| `enforcement-service/app/main.py` | Startup | Load `REGION_ID` env var | |

### 7.2 Cross-Region Enforcement

A vehicle registered in Ireland driving in the UK needs to be verifiable by UK
enforcement. Two approaches:

**Option A (recommended):** The cross-region saga publishes the journey event to
BOTH regions' RabbitMQ. The UK enforcement-service receives the event and caches
it locally. This is already handled by RabbitMQ federation.

**Option B (fallback):** If the UK enforcement-service doesn't find the vehicle
in its local cache, it queries peer clusters:

```python
# enforcement-service/app/service.py — new method
async def verify_cross_region(self, vehicle_reg: str) -> VerificationResponse:
    """Query all peer clusters for this vehicle."""
    for peer_url in PEER_CLUSTER_URLS:
        try:
            resp = await httpx.get(
                f"{peer_url}/api/enforcement/verify/vehicle/{vehicle_reg}",
                timeout=5.0
            )
            if resp.status_code == 200 and resp.json().get("has_active_journey"):
                return VerificationResponse(**resp.json())
        except (httpx.TimeoutException, httpx.ConnectError):
            continue
    return VerificationResponse(has_active_journey=False)
```

### 7.3 Environment Variable Changes

```yaml
enforcement-service:
  environment:
    REGION_ID: "IE"
    PEER_CLUSTERS: "UK=https://uk.traffic.example.com,DE=https://de.traffic.example.com"
```

---

## 8. Changes to Analytics Service

**Current:** Single Go service at `:8006`, dual-write Postgres + Redis,
hourly rollup.

**New:** One instance per region with a **global aggregation layer**.

### 8.1 File Changes

| File | Lines | Change | Detail |
|------|-------|--------|--------|
| `analytics-service/database.go:40-50` | `event_logs` table | Add `region VARCHAR(10)` column | Tag every event with its region |
| `analytics-service/database.go:39-80` | `hourly_stats` table | Add `region VARCHAR(10)` column, update UNIQUE constraint to `(hour, region)` | Per-region hourly stats |
| `analytics-service/consumer.go:198-243` | `handleEvent()` | Extract `region` from event payload | Already receives full event JSON; just read the new field |
| `analytics-service/handlers.go:20-22` | `statsHandler()` | Accept `?region=IE` query param | Filter stats by region; omit param for global aggregate |
| `analytics-service/handlers.go:71-120` | `hourlyStatsHandler()` | Accept `?region=IE` query param | Same |
| `analytics-service/handlers.go:181-244` | `serviceHealthHandler()` | Probe local cluster services only | Each regional analytics probes its own cluster |
| `analytics-service/database.go:118-169` | `runHourlyRollup()` | Group by region in aggregation query | `GROUP BY date_trunc('hour', created_at), region` |

### 8.2 Global Dashboard

For a cross-region dashboard, the frontend queries each regional analytics
endpoint and merges client-side, OR a thin global-analytics proxy fans out
to all clusters:

```
GET /api/analytics/global/stats
  → fans out to IE /api/analytics/stats, UK /api/analytics/stats, DE /api/analytics/stats
  → merges and returns combined response
```

This is a new lightweight handler in a global analytics instance (or in the
global API gateway).

---

## 9. Changes to API Gateway / Load Balancer

**Current:** HAProxy → 2 nginx instances → 6 services on one Docker network.

**New:** Two-tier routing.

### 9.1 Tier 1: Global Router (GeoDNS or Global LB)

Routes the user to the nearest regional cluster based on client IP geolocation.

**For demo purposes:** A single nginx instance that reads the `X-Region` header
(or inspects the journey coordinates in the request body) and proxies to the
correct regional cluster.

**New file:** `api-gateway/global-router.conf`

```nginx
# Global router — routes to regional clusters
upstream cluster_ie {
    server ie-api-gateway:8080;
}
upstream cluster_uk {
    server uk-api-gateway:8080;
}

# User service is global (not region-partitioned)
location /api/users/ {
    proxy_pass http://global-user-service;
}

# Journey/conflict/enforcement/analytics are region-partitioned
# The frontend includes X-Region header based on selected origin
location /api/journeys/ {
    # Route based on X-Region header
    set $cluster "";
    if ($http_x_region = "IE") { set $cluster cluster_ie; }
    if ($http_x_region = "UK") { set $cluster cluster_uk; }
    proxy_pass http://$cluster;
}
```

### 9.2 Tier 2: Regional API Gateway (existing nginx, per-cluster)

Each regional cluster keeps the existing `api-gateway/nginx.conf` structure,
but only routes to its local services.

### 9.3 File Changes

| File | Change |
|------|--------|
| `api-gateway/nginx.conf` | No structural change — one copy per regional cluster |
| `api-gateway/haproxy.cfg` | No structural change — one copy per regional cluster |
| `api-gateway/global-router.conf` | **NEW** — global routing layer |

---

## 10. Changes to Infrastructure / Docker Compose

**Current:** Single `docker-compose.yml` with everything on `journey-net`.

**New:** One docker-compose file per region + a global services compose file.

### 10.1 New File Structure

```
docker-compose.global.yml          # User service, global router, global DB
docker-compose.region-ie.yml       # IE cluster: all 5 partitioned services + infra
docker-compose.region-uk.yml       # UK cluster: all 5 partitioned services + infra
docker-compose.region-de.yml       # DE cluster (optional 3rd region)
```

### 10.2 `docker-compose.global.yml`

```yaml
services:
  global-router:
    image: nginx:1.25-alpine
    ports: ["80:80"]
    volumes:
      - ./api-gateway/global-router.conf:/etc/nginx/nginx.conf:ro

  postgres-users:
    # ... (same as current)

  user-service:
    # ... (same as current, single global instance)
    environment:
      DATABASE_URL: postgresql+asyncpg://users_user:users_pass@postgres-users:5432/users_db

networks:
  global-net:
    driver: bridge
```

### 10.3 `docker-compose.region-ie.yml` (template for each region)

```yaml
services:
  # --- Infrastructure (per-region) ---
  rabbitmq-ie:
    image: rabbitmq:3.13-management-alpine
    environment:
      RABBITMQ_ERLANG_COOKIE: "ie_cluster_cookie"
      # Federation plugin enabled
      RABBITMQ_ENABLED_PLUGINS: "rabbitmq_management,rabbitmq_federation,rabbitmq_federation_management"

  redis-ie:
    image: redis:7-alpine

  postgres-journeys-ie:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: journeys_ie_db

  postgres-conflicts-ie:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: conflicts_ie_db

  postgres-analytics-ie:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: analytics_ie_db

  # --- Application services (per-region) ---
  journey-service-ie:
    build:
      context: .
      dockerfile: journey-service/Dockerfile
    environment:
      REGION_ID: "IE"
      DATABASE_URL: postgresql+asyncpg://...@postgres-journeys-ie:5432/journeys_ie_db
      CONFLICT_SERVICE_URL: http://conflict-service-ie:8000
      PEER_CLUSTERS: "UK=http://journey-service-uk:8000"
      RABBITMQ_URL: amqp://...@rabbitmq-ie:5672/journey_vhost

  conflict-service-ie:
    build:
      context: .
      dockerfile: conflict-service/Dockerfile
    environment:
      REGION_ID: "IE"
      REGION_BBOX_MIN_LAT: "51.42"
      REGION_BBOX_MAX_LAT: "55.43"
      REGION_BBOX_MIN_LNG: "-10.48"
      REGION_BBOX_MAX_LNG: "-5.99"
      DATABASE_URL: postgresql://...@postgres-conflicts-ie:5432/conflicts_ie_db

  enforcement-service-ie:
    build:
      context: .
      dockerfile: enforcement-service/Dockerfile
    environment:
      REGION_ID: "IE"
      PEER_CLUSTERS: "UK=http://enforcement-service-uk:8000"

  notification-service-ie:
    build:
      context: .
      dockerfile: notification-service/Dockerfile
    environment:
      REGION_ID: "IE"

  analytics-service-ie:
    build:
      context: .
      dockerfile: analytics-service/Dockerfile
    environment:
      REGION_ID: "IE"

networks:
  ie-net:
    driver: bridge
  # Connected to global-net for user-service access
```

### 10.4 RabbitMQ Federation Between Clusters

Cross-region events (e.g., a journey confirmed in IE that affects UK) are
forwarded between RabbitMQ instances using the **Shovel** or **Federation** plugin.

```bash
# On rabbitmq-ie: federate the journey_events exchange from UK
rabbitmqctl set_parameter federation-upstream uk-upstream \
  '{"uri":"amqp://rabbitmq-uk:5672","exchange":"journey_events"}'

rabbitmqctl set_policy federate-journey \
  "^journey_events$" \
  '{"federation-upstream-set":"all"}' \
  --apply-to exchanges
```

This means events published to `journey_events` in the UK cluster automatically
appear in the IE cluster's `journey_events` exchange (and vice versa), with
region-tagged routing keys like `journey.confirmed.UK`.

---

## 11. Changes to Shared Libraries

### 11.1 `shared/schemas.py`

| Line | Change |
|------|--------|
| After line 100 (JourneyCreateRequest) | Add `region: Optional[str] = None` field |
| After line 130 (JourneyResponse) | Add `region: str`, `is_cross_region: bool`, `destination_region: Optional[str]` fields |
| Line 231 (AnalyticsEvent) | `region` field already exists — ensure it's always populated |
| After line 167 (ConflictCheckRequest) | Add `region: str` field |

### 11.2 `shared/messaging.py`

| Line | Change |
|------|--------|
| `publish()` (line 86-105) | Add `region` to message headers automatically from `REGION_ID` env var |
| `subscribe()` (line 107-153) | Add optional `region_filter` param — consumers can filter by region in routing key |

### 11.3 `shared/partition.py`

| Line | Change |
|------|--------|
| Lines 59-202 (PartitionManager) | Add peer cluster probes — monitor connectivity to other regional clusters |
| New method | `register_peer_cluster(region_id, endpoint)` — probes peer cluster health |

### 11.4 New File: `shared/region.py`

```python
"""Region resolution utilities shared by all Python services."""

import json, os

_REGIONS = None

def _load_regions():
    global _REGIONS
    path = os.getenv("REGION_REGISTRY_PATH", "/app/regions.json")
    with open(path) as f:
        _REGIONS = json.load(f)["regions"]

def resolve_region(lat: float, lng: float) -> str | None:
    if _REGIONS is None:
        _load_regions()
    for r in _REGIONS:
        bb = r["bounding_box"]
        if bb["min_lat"] <= lat <= bb["max_lat"] and bb["min_lng"] <= lng <= bb["max_lng"]:
            return r["id"]
    return None

def get_cluster_endpoint(region_id: str) -> str:
    if _REGIONS is None:
        _load_regions()
    for r in _REGIONS:
        if r["id"] == region_id:
            return r["cluster_endpoint"]
    raise ValueError(f"Unknown region: {region_id}")

MY_REGION = os.getenv("REGION_ID", "IE")
```

---

## 12. Changes to Frontend

**Current:** Static nginx-served frontend at `:3000`.

### 12.1 Changes

| File | Change |
|------|--------|
| `frontend/index.html` (or JS) | Add region selector or auto-detect from map coordinates |
| `frontend/` (API calls) | Include `X-Region` header in all API requests based on selected journey origin |
| `frontend/` (WebSocket) | Connect to the regional notification-service endpoint for the user's current region |

---

## 13. Database Migration Summary

### 13.1 Journey Service DB

```sql
-- Per-region database (e.g., journeys_ie_db)
ALTER TABLE journeys ADD COLUMN region VARCHAR(10) NOT NULL DEFAULT 'IE';
ALTER TABLE journeys ADD COLUMN is_cross_region BOOLEAN DEFAULT FALSE;
ALTER TABLE journeys ADD COLUMN destination_region VARCHAR(10);
CREATE INDEX idx_journeys_region ON journeys(region);
```

### 13.2 Conflict Service DB

```sql
-- Per-region database (e.g., conflicts_ie_db)
ALTER TABLE booked_slots ADD COLUMN region VARCHAR(10) NOT NULL DEFAULT 'IE';
ALTER TABLE road_segment_capacity ADD COLUMN region VARCHAR(10) NOT NULL DEFAULT 'IE';
```

### 13.3 Analytics Service DB

```sql
-- Per-region database (e.g., analytics_ie_db)
ALTER TABLE event_logs ADD COLUMN region VARCHAR(10) NOT NULL DEFAULT 'IE';
ALTER TABLE hourly_stats ADD COLUMN region VARCHAR(10) NOT NULL DEFAULT 'IE';
-- Update unique constraint
ALTER TABLE hourly_stats DROP CONSTRAINT IF EXISTS hourly_stats_hour_key;
ALTER TABLE hourly_stats ADD CONSTRAINT hourly_stats_hour_region_key UNIQUE(hour, region);
```

### 13.4 User Service DB (global, not partitioned)

```sql
ALTER TABLE users ADD COLUMN home_region VARCHAR(10);
```

---

## 14. Event Schema Changes

All events published to RabbitMQ gain a `region` field and region-qualified
routing keys.

### 14.1 Routing Key Changes

| Current | New |
|---------|-----|
| `journey.confirmed` | `journey.confirmed.IE` (region-suffixed) |
| `journey.rejected` | `journey.rejected.IE` |
| `journey.cancelled` | `journey.cancelled.IE` |
| `journey.started` | `journey.started.IE` |
| `journey.completed` | `journey.completed.IE` |
| `user.registered` | `user.registered` (unchanged — global) |

### 14.2 Event Payload Addition

```json
{
  "journey_id": "abc-123",
  "region": "IE",
  "is_cross_region": false,
  "destination_region": null,
  ...existing fields...
}
```

### 14.3 Queue Binding Changes

Each regional consumer binds to `journey.*.{REGION_ID}`:

```
# IE notification queue binds to:
journey.*.IE

# IE conflict cancellation queue binds to:
journey.cancelled.IE
```

---

## 15. Cross-Region Saga — Full Sequence Diagram

```
User (Dublin→Belfast)           IE Journey Svc          IE Conflict Svc       UK Conflict Svc
        │                            │                       │                      │
        │  POST /api/journeys/       │                       │                      │
        │───────────────────────────>│                       │                      │
        │                            │                       │                      │
        │                   resolve_region(origin) = IE      │                      │
        │                   resolve_region(dest)   = UK      │                      │
        │                   is_cross_region = true            │                      │
        │                            │                       │                      │
        │                   split journey at border           │                      │
        │                   IE segment: Dublin→Border         │                      │
        │                   UK segment: Border→Belfast        │                      │
        │                            │                       │                      │
        │                   Step 1: Check IE segment         │                      │
        │                            │──POST /check────────>│                      │
        │                            │<──── OK, no conflict──│                      │
        │                            │                       │                      │
        │                   Step 2: Check UK segment         │                      │
        │                            │──POST /check-segment──────────────────────>│
        │                            │<──── OK, no conflict──────────────────────│
        │                            │                       │                      │
        │                   Both OK → CONFIRMED              │                      │
        │                   Publish journey.confirmed.IE     │                      │
        │                   Publish journey.confirmed.UK (via federation)           │
        │                            │                       │                      │
        │  201 {status: CONFIRMED}   │                       │                      │
        │<───────────────────────────│                       │                      │
```

**Compensation (Step 2 fails):**

```
        │                   Step 2: Check UK segment         │                      │
        │                            │──POST /check-segment──────────────────────>│
        │                            │<──── CONFLICT: road full ─────────────────│
        │                            │                       │                      │
        │                   COMPENSATE Step 1:               │                      │
        │                            │──POST /cancel/{id}──>│                      │
        │                            │<──── OK ──────────────│                      │
        │                            │                       │                      │
        │                   REJECTED                         │                      │
        │  201 {status: REJECTED}    │                       │                      │
        │<───────────────────────────│                       │                      │
```

---

## 16. Summary: Files Changed Per Service

### New Files
| File | Purpose |
|------|---------|
| `region-registry/regions.json` | Static region bounding box definitions |
| `shared/region.py` | Python region lookup utilities |
| `conflict-service/region.go` | Go region lookup utilities |
| `journey-service/app/cross_region_saga.py` | Cross-region booking saga |
| `journey-service/app/region_utils.py` | Journey splitting at borders |
| `api-gateway/global-router.conf` | Global geo-routing nginx config |
| `docker-compose.global.yml` | Global services (user, router) |
| `docker-compose.region-ie.yml` | Ireland cluster |
| `docker-compose.region-uk.yml` | UK cluster |

### Modified Files (by service)

| Service | Files Modified | Key Changes |
|---------|---------------|-------------|
| **user-service** | `database.py`, `service.py`, `routes.py` | Add `home_region` column |
| **journey-service** | `database.py`, `service.py`, `saga.py`, `scheduler.py`, `outbox_publisher.py`, `routes.py`, `main.py` | Region resolution, cross-region detection, cross-region saga, region-filtered scheduling |
| **conflict-service** | `config.go`, `service.go`, `handlers.go`, `database.go`, `consumer.go` | Bounding box config, path clipping, region column, new `/check-segment` endpoint |
| **notification-service** | `consumer.go`, `redis.go`, `config.go` | Region-prefixed keys, region event filtering |
| **enforcement-service** | `service.py`, `consumer.py`, `main.py` | Region-prefixed cache keys, cross-region fallback query |
| **analytics-service** | `database.go`, `consumer.go`, `handlers.go` | Region column, region-filtered stats, per-region rollup |
| **shared** | `schemas.py`, `messaging.py`, `partition.py` | Region fields in schemas, region-aware publishing, peer cluster probes |
| **api-gateway** | `global-router.conf` (new), `nginx.conf` (minor) | Global routing layer |
| **infrastructure** | `docker-compose.*.yml` | Split into global + per-region compose files |

---

## 17. Trade-offs and Justification

| Decision | Trade-off | Justification |
|----------|-----------|---------------|
| **Geographic partitioning** | Adds complexity for cross-region journeys | Road data is naturally local; ~95% of journeys are intra-region. The rare cross-region case pays the coordination cost, but the common case is fast and partition-tolerant. |
| **User service stays global** | Single point of failure for auth | Users are global entities (a driver can travel anywhere). Partitioning users by region would require cross-region auth lookups on every request. A replicated global user DB is simpler. |
| **RabbitMQ federation** (not shared cluster) | Eventual consistency for cross-region events | Each region can operate independently during inter-cluster network partitions. Federation reconnects and replays when connectivity is restored. |
| **Region-suffixed routing keys** | More queue bindings to manage | Allows each regional consumer to subscribe only to its own events, preventing duplicate processing. |
| **Static region config** (not a service) | Must redeploy to add regions | Regions (countries) change rarely. A static JSON file is simpler and has no runtime dependency. |
| **Straight-line border crossing** | Inaccurate for real roads | Consistent with existing grid model (conflict-service already uses straight-line paths). Real road routing is explicitly out of scope per the exercise. |
