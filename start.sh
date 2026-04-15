#!/bin/bash
# =============================================================================
# start.sh — Clean start for the slim docker-compose stack
#
# Kills anything holding ports 3000, 6379, 8080, 8003 and removes stale
# containers/volumes before bringing the stack up fresh.
#
# Usage:
#   ./start.sh                        # standalone (no peers)
#   ./start.sh <PEER_IP>              # one peer
#   ./start.sh <PEER_IP1> <PEER_IP2>  # multiple peers
#
# Example:
#   ./start.sh 172.20.10.12
# =============================================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.slim.yml"

# ── 1. Build peer lists from args ─────────────────────────────────────────────
PEER_CONFLICT_URLS=""
PEER_USER_URLS=""
for ip in "$@"; do
    PEER_CONFLICT_URLS="${PEER_CONFLICT_URLS:+$PEER_CONFLICT_URLS,}http://$ip:8003"
    PEER_USER_URLS="${PEER_USER_URLS:+$PEER_USER_URLS,}http://$ip:8080"
done

if [ -n "$PEER_CONFLICT_URLS" ]; then
    info "Peers: $PEER_CONFLICT_URLS"
    cat > .env <<EOF
PEER_CONFLICT_URLS=$PEER_CONFLICT_URLS
PEER_USER_URLS=$PEER_USER_URLS
EOF
    success ".env written with peer URLs"
else
    info "No peers specified — starting standalone"
    rm -f .env
fi

# ── 2. Tear down existing stack ───────────────────────────────────────────────
info "Stopping existing stack..."
$COMPOSE down --remove-orphans 2>/dev/null || true

# ── 3. Free ports that are commonly stuck ─────────────────────────────────────
free_port() {
    local port=$1
    local pids
    pids=$(lsof -ti ":$port" 2>/dev/null || true)
    if [ -n "$pids" ]; then
        warn "Port $port in use — killing PID(s): $pids"
        echo "$pids" | xargs kill -9 2>/dev/null || true
    fi
}

free_port 3000
free_port 6379
free_port 8080
free_port 8003

# Short wait for OS to release ports
sleep 1

# ── 4. Remove the RabbitMQ volume (cookie permission issue on re-runs) ─────────
info "Removing stale RabbitMQ volume..."
docker volume rm excercise2_rabbitmq_data 2>/dev/null && \
    success "Removed excercise2_rabbitmq_data" || \
    info "Volume didn't exist — nothing to remove"

# ── 5. Start the stack ────────────────────────────────────────────────────────
info "Starting stack..."
$COMPOSE up -d

# ── 6. Wait and health-check ──────────────────────────────────────────────────
echo ""
info "Waiting for gateway to become healthy..."
for i in $(seq 1 24); do
    sleep 5
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/health 2>/dev/null || echo "000")
    if [ "$STATUS" = "200" ]; then
        success "Gateway healthy (HTTP 200) after $((i*5))s"
        break
    fi
    echo -ne "\r  Waiting... ${i}×5s (last status: $STATUS)"
done
echo ""

# ── 7. Summary ────────────────────────────────────────────────────────────────
echo ""
echo "============================================="
echo -e " ${GREEN}Stack is up${NC}"
echo "============================================="
echo "  Frontend   : http://localhost:3000"
echo "  API Gateway: http://localhost:8080"
echo "  Conflict   : http://localhost:8003"
echo "  RabbitMQ UI: http://localhost:15672  (journey_admin / journey_pass)"
if [ -n "$PEER_CONFLICT_URLS" ]; then
    echo ""
    echo "  Peers configured: $*"
    echo ""
    echo "  To verify replication:"
    echo "    docker logs \$(docker ps -qf 'name=conflict-service') 2>&1 | grep -E 'sync|peer' | tail -5"
fi
echo ""
