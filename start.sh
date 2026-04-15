#!/bin/bash
# =============================================================================
# start.sh — Start (or update) the slim docker-compose stack
#
# Usage:
#   ./start.sh                        # standalone, no peers — fresh start
#   ./start.sh <PEER_IP> [IP2 ...]    # write .env for peers, full fresh start
#   ./start.sh --update [IP ...]      # update .env + force-recreate (no teardown)
#   ./start.sh --verify               # check replication status only
#
# Examples:
#   ./start.sh 172.20.10.12
#   ./start.sh 172.20.10.12 172.20.10.13
#   ./start.sh --update 172.20.10.12
#   ./start.sh --verify
# =============================================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }

COMPOSE="docker compose -f docker-compose.yml -f docker-compose.slim.yml"

# ── Mode detection ────────────────────────────────────────────────────────────
MODE="fresh"   # fresh | update | verify
if [ "${1:-}" = "--update" ]; then
    MODE="update"
    shift
elif [ "${1:-}" = "--verify" ]; then
    MODE="verify"
    shift
fi

# ── Step 0: Git — ensure we're on approach3 with latest code ─────────────────
if [ "$MODE" != "verify" ]; then
    echo ""
    info "── Git setup ──────────────────────────────────────────────────"
    CURRENT_BRANCH=$(git branch --show-current 2>/dev/null || echo "unknown")
    if [ "$CURRENT_BRANCH" != "approach3" ]; then
        info "Switching from '$CURRENT_BRANCH' to approach3..."
        git checkout approach3
    else
        success "Already on approach3"
    fi
    git pull
    success "Branch: $(git branch --show-current)  Commit: $(git rev-parse --short HEAD)"
fi

# ── Step 1: Build peer URL lists from args ────────────────────────────────────
PEER_CONFLICT_URLS=""
PEER_USER_URLS=""
for ip in "$@"; do
    PEER_CONFLICT_URLS="${PEER_CONFLICT_URLS:+$PEER_CONFLICT_URLS,}http://$ip:8003"
    PEER_USER_URLS="${PEER_USER_URLS:+$PEER_USER_URLS,}http://$ip:8080"
done

# ── VERIFY mode: show replication status and exit ─────────────────────────────
if [ "$MODE" = "verify" ]; then
    echo ""
    info "── Conflict-service peer/sync log (last 15 lines) ──"
    CONFLICT_CTR=$(docker ps --format '{{.Names}}' | grep -E 'conflict.service' | head -1 || true)
    if [ -n "$CONFLICT_CTR" ]; then
        docker logs "$CONFLICT_CTR" 2>&1 | grep -E "peer|sync|replication|PUSH|RECV" | tail -15 \
            || warn "No peer/sync lines found yet"
    else
        warn "conflict-service container not found"
    fi

    echo ""
    info "── User-service peer/sync log (last 15 lines) ──"
    USER_CTR=$(docker ps --format '{{.Names}}' | grep -E 'user.service' | head -1 || true)
    if [ -n "$USER_CTR" ]; then
        docker logs "$USER_CTR" 2>&1 | grep -E "peer|sync|replication|dist-lock" | tail -15 \
            || warn "No peer/sync lines found yet"
    else
        warn "user-service container not found"
    fi

    echo ""
    info "── Active booked_slots (conflicts DB) ──"
    PG_CTR=$(docker ps --format '{{.Names}}' | grep -E 'postgres.conflicts' | head -1 || true)
    if [ -n "$PG_CTR" ]; then
        docker exec "$PG_CTR" psql -U conflicts_user -d conflicts_db \
            -c "SELECT COUNT(*) AS active_slots FROM booked_slots WHERE is_active=true;" 2>/dev/null \
            || warn "Could not query conflicts DB"
    else
        warn "postgres-conflicts container not found"
    fi

    echo ""
    info "── Running containers ──"
    docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
    exit 0
fi

# ── UPDATE mode: write .env + force-recreate peer-aware services only ─────────
if [ "$MODE" = "update" ]; then
    echo ""
    info "── Update mode — force-recreate (no teardown) ────────────────"
    if [ -n "$PEER_CONFLICT_URLS" ]; then
        cat > .env <<EOF
PEER_CONFLICT_URLS=$PEER_CONFLICT_URLS
PEER_USER_URLS=$PEER_USER_URLS
EOF
        success ".env updated:"
        echo "  PEER_CONFLICT_URLS=$PEER_CONFLICT_URLS"
        echo "  PEER_USER_URLS=$PEER_USER_URLS"
    else
        info "No IPs given — keeping existing .env:"
        [ -f .env ] && cat .env || echo "  (empty)"
    fi

    info "Force-recreating conflict-service, user-service, journey-service..."
    $COMPOSE up -d --no-build --force-recreate conflict-service user-service journey-service
    success "Services restarted with new peer config"

    echo ""
    info "Waiting 10s for services to settle..."
    sleep 10
    ./start.sh --verify
    exit 0
fi

# ── FRESH mode: write .env, tear down, free ports, start ──────────────────────

# Step 1 cont.: write / clear .env
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

# Step 2: Tear down existing stack
echo ""
info "── Stopping existing stack ───────────────────────────────────────"
$COMPOSE down --remove-orphans 2>/dev/null || true

# Step 3: Free ports that are commonly stuck
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

sleep 1

# Step 4: Remove the RabbitMQ volume (cookie permission issue on re-runs)
info "Removing stale RabbitMQ volume..."
docker volume rm excercise2_rabbitmq_data 2>/dev/null && \
    success "Removed excercise2_rabbitmq_data" || \
    info "Volume didn't exist — nothing to remove"

# Step 5: Build peer-aware services and start the stack
echo ""
info "── Building conflict-service and journey-service ────────────────"
$COMPOSE build conflict-service journey-service

echo ""
info "── Starting stack ────────────────────────────────────────────────"
$COMPOSE up -d

# Step 6: Wait and health-check
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

# Step 7: Summary
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
    echo "  Next: register peers live (triggers catch-up sync):"
    echo "    ./register_peers.sh $*"
fi
echo ""
echo "  Verify replication anytime:"
echo "    ./start.sh --verify"
echo ""