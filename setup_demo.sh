#!/bin/bash
# =============================================================================
# setup_demo.sh — distributed demo setup (2–10 laptops)
#
# Usage:
#   ./setup_demo.sh <your-label> <ip-1> <ip-2> [ip-3] ... [ip-N]
#
# Labels are assigned in order: first IP = A, second = B, third = C, etc.
# Pass ALL laptops' IPs in the same fixed order every time.
#
# Examples:
#   2 laptops (you are A):  ./setup_demo.sh A 172.20.10.2 172.20.10.3
#   4 laptops (you are B):  ./setup_demo.sh B 172.20.10.2 172.20.10.3 172.20.10.4 172.20.10.5
#   6 laptops (you are C):  ./setup_demo.sh C 172.20.10.2 172.20.10.3 172.20.10.4 172.20.10.5 172.20.10.6 172.20.10.7
#
# Get your hotspot IP: ipconfig getifaddr en0
# =============================================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Parse arguments ────────────────────────────────────────────────────────────
if [ $# -lt 3 ]; then
    echo ""
    echo "Usage: ./setup_demo.sh <your-label> <ip-1> <ip-2> [ip-3] ... [ip-N]"
    echo ""
    echo "  <your-label>  Which laptop you are: A, B, C, ... (matches position of your IP)"
    echo "  <ip-1..N>     IPs of ALL laptops in a fixed order (same order on every laptop)"
    echo ""
    echo "  Minimum: 2 laptops (2 IPs). Maximum: 10 (labels A–J)."
    echo ""
    echo "Examples:"
    echo "  2 laptops, you are A:  ./setup_demo.sh A 192.168.1.10 192.168.1.11"
    echo "  4 laptops, you are B:  ./setup_demo.sh B 172.20.10.2 172.20.10.3 172.20.10.4 172.20.10.5"
    echo ""
    echo "Get your hotspot IP with:  ipconfig getifaddr en0"
    echo ""
    exit 1
fi

MY_LABEL=$(echo "$1" | tr '[:lower:]' '[:upper:]')
shift  # remaining args are IPs

# Build IP array from remaining args
IPS=("$@")
N=${#IPS[@]}

# Validate label
LABELS=(A B C D E F G H I J)
MY_INDEX=-1
for i in "${!LABELS[@]}"; do
    if [ "${LABELS[$i]}" = "$MY_LABEL" ]; then
        MY_INDEX=$i
        break
    fi
done

if [ "$MY_INDEX" -eq -1 ]; then
    error "Label must be A–J, got '$MY_LABEL'"
fi
if [ "$MY_INDEX" -ge "$N" ]; then
    error "Label '$MY_LABEL' is position $((MY_INDEX+1)) but only $N IPs were provided"
fi

MY_IP="${IPS[$MY_INDEX]}"

# Build peer lists (all IPs except mine)
PEER_CONFLICT_URLS=""
PEER_USER_URLS=""
PEER_JOURNEY_URLS=""
for i in "${!IPS[@]}"; do
    if [ "$i" -ne "$MY_INDEX" ]; then
        ip="${IPS[$i]}"
        PEER_CONFLICT_URLS="${PEER_CONFLICT_URLS:+$PEER_CONFLICT_URLS,}http://$ip:8003"
        PEER_USER_URLS="${PEER_USER_URLS:+$PEER_USER_URLS,}http://$ip:8080"
        PEER_JOURNEY_URLS="${PEER_JOURNEY_URLS:+$PEER_JOURNEY_URLS,}http://$ip:8080"
    fi
done

echo ""
echo "============================================="
echo " Distributed Traffic System — Demo Setup"
echo " Laptop $MY_LABEL  |  My IP: $MY_IP  |  Nodes: $N"
echo "============================================="
echo ""
info "Peer conflict URLs : $PEER_CONFLICT_URLS"
info "Peer user URLs     : $PEER_USER_URLS"
echo ""

# ── Step 1: Verify we're on the right IP ──────────────────────────────────────
ACTUAL_IP=$(ipconfig getifaddr en0 2>/dev/null || echo "unknown")
if [ "$ACTUAL_IP" != "$MY_IP" ]; then
    warn "Your current hotspot IP is $ACTUAL_IP but you said you are $MY_IP"
    warn "Make sure you connected to the hotspot BEFORE running this script."
    read -p "Continue anyway? [y/N] " yn
    [[ "$yn" =~ ^[Yy]$ ]] || exit 1
else
    success "IP confirmed: $ACTUAL_IP"
fi

# ── Step 2: Check Docker Swarm ────────────────────────────────────────────────
info "Checking Docker Swarm..."
SWARM_STATE=$(docker info 2>/dev/null | grep "Swarm:" | awk '{print $2}')
if [ "$SWARM_STATE" != "active" ]; then
    info "Swarm not active — initialising..."
    docker swarm init --advertise-addr "$MY_IP"
    success "Swarm initialised"
else
    success "Swarm already active"
fi

# ── Step 3: Write .env ────────────────────────────────────────────────────────
info "Writing .env..."
{
    echo "PEER_CONFLICT_URLS=$PEER_CONFLICT_URLS"
    echo "PEER_USER_URLS=$PEER_USER_URLS"
    echo "PEER_JOURNEY_URLS=$PEER_JOURNEY_URLS"
    echo "MY_LABEL=$MY_LABEL"
    echo "NODE_COUNT=$N"
    for i in "${!IPS[@]}"; do
        echo "IP_${LABELS[$i]}=${IPS[$i]}"
    done
} > .env
success ".env written"

# ── Step 4: Start local registry ──────────────────────────────────────────────
info "Ensuring local Docker registry is running..."
if ! docker service ls 2>/dev/null | grep -q "registry"; then
    docker service create --name registry --publish published=5000,target=5000 registry:2
    sleep 6
    success "Registry started"
else
    success "Registry already running"
fi

# ── Step 5: Export env and deploy ─────────────────────────────────────────────
export PEER_CONFLICT_URLS
export PEER_USER_URLS
export PEER_JOURNEY_URLS

info "Building images and deploying stack..."
./deploy-swarm.sh

# ── Step 6: Wait for services ─────────────────────────────────────────────────
echo ""
info "Waiting for services to become healthy (up to 3 minutes)..."
for i in $(seq 1 36); do
    sleep 5
    TOTAL=$(docker service ls 2>/dev/null | grep "traffic-service" | wc -l)
    READY=$(docker service ls 2>/dev/null | grep "traffic-service" | awk '{print $4}' | grep -v "replicas" | awk -F'/' '{if($1>0 && $1==$2) print}' | wc -l)
    echo -ne "\r  Services ready: $READY / $TOTAL   (${i}×5s elapsed)"
    if [ "$READY" -eq "$TOTAL" ] && [ "$TOTAL" -gt 0 ]; then
        echo ""
        break
    fi
done
echo ""

# ── Step 7: Self health check ─────────────────────────────────────────────────
info "Checking own health endpoint..."
sleep 3
HEALTH=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/health 2>/dev/null || echo "000")
if [ "$HEALTH" = "200" ]; then
    success "Gateway healthy (HTTP 200)"
else
    warn "Gateway returned HTTP $HEALTH — services may still be starting. Try: curl http://localhost:8080/health"
fi

# ── Step 8: Register peers via live API ───────────────────────────────────────
echo ""
info "Registering peer health monitors (POST /admin/peers/register)..."
sleep 5
for i in "${!IPS[@]}"; do
    if [ "$i" -ne "$MY_INDEX" ]; then
        label="${LABELS[$i]}"
        ip="${IPS[$i]}"
        HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST http://localhost:8080/admin/peers/register \
            -H "Content-Type: application/json" \
            -d "{\"name\": \"laptop-${label}\", \"health_url\": \"http://${ip}:8080/health\"}" \
            2>/dev/null || echo "000")
        if [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ]; then
            success "Registered peer laptop-$label ($ip)"
        else
            warn "Could not register laptop-$label (HTTP $HTTP) — retry with: ./register_peers.sh"
        fi
    fi
done

# ── Step 9: Trigger catch-up sync from all peers ──────────────────────────────
echo ""
info "Triggering conflict-service catch-up sync from peers..."
sleep 2
for i in "${!IPS[@]}"; do
    if [ "$i" -ne "$MY_INDEX" ]; then
        label="${LABELS[$i]}"
        ip="${IPS[$i]}"

        HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST http://localhost:8003/internal/peers/register \
            -H "Content-Type: application/json" \
            -d "{\"peer_url\": \"http://${ip}:8003\"}" \
            2>/dev/null || echo "000")
        if [ "$HTTP" = "200" ] || [ "$HTTP" = "204" ]; then
            success "Conflict sync registered with laptop-$label ($ip:8003)"
        else
            warn "Conflict sync to laptop-$label returned HTTP $HTTP — may still be starting"
        fi

        HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST http://localhost:8080/internal/journeys/peers/register \
            -H "Content-Type: application/json" \
            -d "{\"peer_url\": \"http://${ip}:8080\"}" \
            2>/dev/null || echo "000")
        if [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ]; then
            success "Journey sync registered with laptop-$label ($ip:8080)"
        else
            warn "Journey sync to laptop-$label returned HTTP $HTTP — may still be starting"
        fi
    fi
done

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "============================================="
echo -e " ${GREEN}Setup complete — Laptop $MY_LABEL ($N nodes)${NC}"
echo "============================================="
echo ""
echo "  Frontend:    http://localhost:3000"
echo "  API Gateway: http://localhost:8080"
echo "  Health:      http://localhost:8080/health"
echo "  RabbitMQ UI: http://localhost:15672  (journey_admin / journey_pass)"
echo ""
echo "  All nodes:"
for i in "${!IPS[@]}"; do
    label="${LABELS[$i]}"
    ip="${IPS[$i]}"
    if [ "$i" -eq "$MY_INDEX" ]; then
        echo "    Laptop $label: $ip  ← YOU"
    else
        echo "    Laptop $label: $ip"
    fi
done
echo ""
echo "  Demo kill commands:"
echo "    Soft kill (all routes 503, browser fails over):"
echo "      curl -X POST http://localhost:8080/admin/simulate/fail"
echo "    Recover:"
echo "      curl -X POST http://localhost:8080/admin/simulate/recover"
echo ""
echo "  If peer registration failed, run:"
echo "    ./register_peers.sh"
echo ""
