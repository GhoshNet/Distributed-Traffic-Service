#!/bin/bash
# =============================================================================
# demo_cheatsheet.sh — Copy-paste commands for the live demo
#
# This is a reference script. Run individual commands as needed.
# =============================================================================

source .env 2>/dev/null || true

echo "
╔══════════════════════════════════════════════════════════════════╗
║           DEMO CHEAT SHEET — Laptop $MY_LABEL                          ║
╚══════════════════════════════════════════════════════════════════╝

URLs
  Frontend   : http://localhost:3000
  API Gateway: http://localhost:8080
  RabbitMQ UI: http://localhost:15672  (journey_admin / journey_pass)

Peers: A=$IP_A  B=$IP_B  C=$IP_C  D=$IP_D

══════════════════════════════════════════════════
 1. CHECK HEALTH
══════════════════════════════════════════════════
# Own node
curl http://localhost:8080/health

# All services aggregated (analytics hub)
curl http://localhost:8080/api/analytics/health/services | python3 -m json.tool

# Peer node health (replace IP as needed)
curl http://<PEER_IP>:8080/health

# Who's ALIVE / SUSPECT / DEAD
curl http://localhost:8080/health/nodes | python3 -m json.tool

# Partition status of dependencies
curl http://localhost:8080/health/partitions | python3 -m json.tool

══════════════════════════════════════════════════
 2. SOFT KILL — All routes return 503
    (journey + user service both go down,
     browser's resilientFetch reroutes to a peer)
══════════════════════════════════════════════════
# Kill this node
curl -X POST http://localhost:8080/admin/simulate/fail

# Recover
curl -X POST http://localhost:8080/admin/simulate/recover

══════════════════════════════════════════════════
 3. HARD KILL INDIVIDUAL SERVICES
    (more dramatic for the demo — actually stops containers)
══════════════════════════════════════════════════

# Kill journey-service (browser failover kicks in)
docker service scale traffic-service_journey-service=0
# Restore
docker service scale traffic-service_journey-service=1

# Kill conflict-service (booking tries peer's conflict service)
docker service scale traffic-service_conflict-service=0
# Restore
docker service scale traffic-service_conflict-service=1

# Kill user-service (login/register goes to peer)
docker service scale traffic-service_user-service=0
# Restore
docker service scale traffic-service_user-service=1

# Kill notification-service (WebSocket fails over to peer)
docker service scale traffic-service_notification-service=0
# Restore
docker service scale traffic-service_notification-service=1

# Kill enforcement-service
docker service scale traffic-service_enforcement-service=0
# Restore
docker service scale traffic-service_enforcement-service=1

# Kill analytics-service
docker service scale traffic-service_analytics-service=0
# Restore
docker service scale traffic-service_analytics-service=1

# Kill gateway (HAProxy fails over to second nginx instance)
docker service scale traffic-service_api-gateway-1=0
# Restore
docker service scale traffic-service_api-gateway-1=1

══════════════════════════════════════════════════
 4. KILL THE WHOLE NODE (most dramatic)
══════════════════════════════════════════════════
# Brings down all services on THIS laptop
docker stack rm traffic-service

# Bring it back (takes ~90s to be healthy again)
export \$(cat .env | xargs)
./deploy-swarm.sh

══════════════════════════════════════════════════
 5. WATCH SERVICES STATUS (live dashboard)
══════════════════════════════════════════════════
watch -n 2 docker service ls

══════════════════════════════════════════════════
 6. SERVICE LOGS
══════════════════════════════════════════════════
docker service logs traffic-service_journey-service    -f --tail 30
docker service logs traffic-service_conflict-service   -f --tail 30
docker service logs traffic-service_user-service       -f --tail 30
docker service logs traffic-service_notification-service -f --tail 30
docker service logs traffic-service_enforcement-service  -f --tail 30
docker service logs traffic-service_analytics-service    -f --tail 30
docker service logs traffic-service_haproxy              -f --tail 30
docker service logs traffic-service_api-gateway-1        -f --tail 30

# Watch replication happening across nodes
docker service logs traffic-service_conflict-service 2>&1 | grep -E 'replication|sync|PUSH|RECV' | tail -20

══════════════════════════════════════════════════
 7. FORCE CATCH-UP SYNC (if a node rejoins late)
══════════════════════════════════════════════════
# Force conflict-service to re-sync from a specific peer
curl -X POST http://localhost:8003/internal/peers/register \\
  -H 'Content-Type: application/json' \\
  -d '{\"peer_url\": \"http://<PEER_IP>:8003\"}'

# Rebuild enforcement cache after node recovery
curl -X POST http://localhost:8080/admin/recovery/rebuild-enforcement-cache

# Drain any unpublished outbox events
curl -X POST http://localhost:8080/admin/recovery/drain-outbox

══════════════════════════════════════════════════
 8. VERIFY CROSS-NODE REPLICATION AT DB LEVEL
══════════════════════════════════════════════════
# Run on BOTH nodes — counts should match within ~1s of a booking
CTR=\$(docker ps --filter 'name=traffic-service_postgres-conflicts.' --format '{{.Names}}' | head -1)
docker exec \$CTR psql -U conflicts_user -d conflicts_db \\
  -c 'SELECT COUNT(*) AS total, SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active FROM booked_slots;'

# See last 10 bookings replicated
docker exec \$CTR psql -U conflicts_user -d conflicts_db \\
  -c 'SELECT journey_id, vehicle_registration, departure_time, is_active, created_at FROM booked_slots ORDER BY created_at DESC LIMIT 10;'

══════════════════════════════════════════════════
 9. RE-REGISTER PEERS (if health monitor lost them)
══════════════════════════════════════════════════
./register_peers.sh

══════════════════════════════════════════════════
 10. CHECK POSTGRES REPLICATION LAG
══════════════════════════════════════════════════
curl http://localhost:8080/api/analytics/replica-lag | python3 -m json.tool

"
