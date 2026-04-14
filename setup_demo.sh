#!/bin/bash
# =============================================================================
# setup_demo.sh — 4-laptop distributed demo setup
#
# Usage:
#   ./setup_demo.sh <your-label> <ip-A> <ip-B> <ip-C> <ip-D>
#
# Example (you are laptop B):
#   ./setup_demo.sh B 172.20.10.2 172.20.10.3 172.20.10.4 172.20.10.5
#
# Labels: A, B, C, D  — just tells the script which IP is yours
# IPs: pass all 4 in order A B C D every time, same order on every laptop
# =============================================================================
set -e

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Parse arguments ────────────────────────────────────────────────────────────
if [ $# -ne 5 ]; then
    echo ""
    echo "Usage: ./setup_demo.sh <your-label> <ip-A> <ip-B> <ip-C> <ip-D>"
    echo ""
    echo "  <your-label>  Which laptop you are: A, B, C, or D"
    echo "  <ip-A..D>     Hotspot IPs of all 4 laptops in fixed order"
    echo ""
    echo "Example (you are laptop C):"
    echo "  ./setup_demo.sh C 172.20.10.2 172.20.10.3 172.20.10.4 172.20.10.5"
    echo ""
    echo "Get your hotspot IP with:"
    echo "  ipconfig getifaddr en0"
    echo ""
    exit 1
fi

MY_LABEL=$(echo "$1" | tr '[:lower:]' '[:upper:]')
IP_A="$2"
IP_B="$3"
IP_C="$4"
IP_D="$5"

case "$MY_LABEL" in
    A) MY_IP="$IP_A" ;;
    B) MY_IP="$IP_B" ;;
    C) MY_IP="$IP_C" ;;
    D) MY_IP="$IP_D" ;;
    *) error "Label must be A, B, C, or D — got '$MY_LABEL'" ;;
esac

# Build peer lists (all IPs except mine)
PEER_CONFLICT_URLS=""
PEER_USER_URLS=""
PEER_JOURNEY_URLS=""
for label in A B C D; do
    case "$label" in
        A) ip="$IP_A" ;;
        B) ip="$IP_B" ;;
        C) ip="$IP_C" ;;
        D) ip="$IP_D" ;;
    esac
    if [ "$label" != "$MY_LABEL" ]; then
        PEER_CONFLICT_URLS="${PEER_CONFLICT_URLS:+$PEER_CONFLICT_URLS,}http://$ip:8003"
        PEER_USER_URLS="${PEER_USER_URLS:+$PEER_USER_URLS,}http://$ip:8080"
        PEER_JOURNEY_URLS="${PEER_JOURNEY_URLS:+$PEER_JOURNEY_URLS,}http://$ip:8080"
    fi
done

echo ""
echo "============================================="
echo " Distributed Traffic System — Demo Setup"
echo " Laptop $MY_LABEL  |  My IP: $MY_IP"
echo "============================================="
echo ""
info "Peer conflict URLs : $PEER_CONFLICT_URLS"
info "Peer user URLs     : $PEER_USER_URLS"
info "Peer journey URLs  : $PEER_JOURNEY_URLS"
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
cat > .env <<EOF
PEER_CONFLICT_URLS=$PEER_CONFLICT_URLS
PEER_USER_URLS=$PEER_USER_URLS
PEER_JOURNEY_URLS=$PEER_JOURNEY_URLS
MY_LABEL=$MY_LABEL
IP_A=$IP_A
IP_B=$IP_B
IP_C=$IP_C
IP_D=$IP_D
EOF
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

# ── Step 8: Register peers via live API (no restart needed) ──────────────────
echo ""
info "Registering peer health monitors (POST /admin/peers/register)..."
sleep 5
for label in A B C D; do
    case "$label" in
        A) ip="$IP_A" ;;
        B) ip="$IP_B" ;;
        C) ip="$IP_C" ;;
        D) ip="$IP_D" ;;
    esac
    if [ "$label" != "$MY_LABEL" ]; then
        HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST http://localhost:8080/admin/peers/register \
            -H "Content-Type: application/json" \
            -d "{\"name\": \"laptop-${label}\", \"health_url\": \"http://${ip}:8080/health\"}" \
            2>/dev/null || echo "000")
        if [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ]; then
            success "Registered peer laptop-$label ($ip)"
        else
            warn "Could not register laptop-$label yet (HTTP $HTTP) — peer may still be starting. Retry with: ./register_peers.sh"
        fi
    fi
done

# ── Step 9: Trigger catch-up sync from all peers ──────────────────────────────
echo ""
info "Triggering conflict-service catch-up sync from peers..."
sleep 2
for label in A B C D; do
    case "$label" in
        A) ip="$IP_A" ;;
        B) ip="$IP_B" ;;
        C) ip="$IP_C" ;;
        D) ip="$IP_D" ;;
    esac
    if [ "$label" != "$MY_LABEL" ]; then
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

        # Trigger journey catch-up sync
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
echo -e " ${GREEN}Setup complete — Laptop $MY_LABEL${NC}"
echo "============================================="
echo ""
echo "  Frontend:    http://localhost:3000"
echo "  API Gateway: http://localhost:8080"
echo "  Health:      http://localhost:8080/health"
echo "  RabbitMQ UI: http://localhost:15672  (journey_admin / journey_pass)"
echo ""
echo "  Peer IPs:"
for label in A B C D; do
    case "$label" in
        A) ip="$IP_A" ;;
        B) ip="$IP_B" ;;
        C) ip="$IP_C" ;;
        D) ip="$IP_D" ;;
    esac
    if [ "$label" = "$MY_LABEL" ]; then
        echo "    Laptop $label: $ip  ← YOU"
    else
        echo "    Laptop $label: $ip"
    fi
done
echo ""
echo "  Demo kill commands (see demo_cheatsheet.sh for full list):"
echo "    Soft kill (all routes 503, browser fails over):"
echo "      curl -X POST http://localhost:8080/admin/simulate/fail"
echo "    Recover:"
echo "      curl -X POST http://localhost:8080/admin/simulate/recover"
echo ""
echo "  If peer registration failed, run:"
echo "    ./register_peers.sh"
echo ""
