#!/bin/bash
# register_peers.sh — Re-register all peers without redeploying
# Run this if setup_demo.sh peer registration failed (peers not up yet)
# Also safe to run multiple times.
#
# Usage: ./register_peers.sh
# (reads .env written by setup_demo.sh)

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }

[ -f .env ] || { echo "Run setup_demo.sh first — .env not found"; exit 1; }
source .env

echo ""
echo "Registering peers for Laptop $MY_LABEL..."
echo ""

for label in A B C D; do
    case "$label" in
        A) ip="$IP_A" ;;
        B) ip="$IP_B" ;;
        C) ip="$IP_C" ;;
        D) ip="$IP_D" ;;
    esac
    if [ "$label" != "$MY_LABEL" ]; then
        # Register health peer (journey-service peer monitor)
        HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST http://localhost:8080/admin/peers/register \
            -H "Content-Type: application/json" \
            -d "{\"name\": \"laptop-${label}\", \"health_url\": \"http://${ip}:8080/health\"}" \
            2>/dev/null || echo "000")
        [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ] \
            && success "Health peer laptop-$label ($ip) registered" \
            || warn    "Health peer laptop-$label: HTTP $HTTP (is it up?)"

        # Register conflict-service replication peer + trigger catch-up sync
        HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST http://localhost:8003/internal/peers/register \
            -H "Content-Type: application/json" \
            -d "{\"peer_url\": \"http://${ip}:8003\"}" \
            2>/dev/null || echo "000")
        [ "$HTTP" = "200" ] || [ "$HTTP" = "204" ] \
            && success "Conflict peer laptop-$label ($ip:8003) registered + catch-up triggered" \
            || warn    "Conflict peer laptop-$label: HTTP $HTTP (is it up?)"

        # Register journey-service replication peer + trigger catch-up sync
        HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST http://localhost:8080/internal/journeys/peers/register \
            -H "Content-Type: application/json" \
            -d "{\"peer_url\": \"http://${ip}:8080\"}" \
            2>/dev/null || echo "000")
        [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ] \
            && success "Journey peer laptop-$label ($ip:8080) registered + catch-up triggered" \
            || warn    "Journey peer laptop-$label: HTTP $HTTP (is it up?)"

        # Register user-service replication peer + trigger catch-up sync
        HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
            -X POST http://localhost:8080/internal/peers/register \
            -H "Content-Type: application/json" \
            -d "{\"peer_url\": \"http://${ip}:8080\"}" \
            2>/dev/null || echo "000")
        [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ] \
            && success "User peer laptop-$label ($ip:8080) registered + catch-up triggered" \
            || warn    "User peer laptop-$label: HTTP $HTTP (is it up?)"
    fi
done

echo ""
info "Verifying peer health status..."
curl -s http://localhost:8080/health/nodes 2>/dev/null | python3 -m json.tool 2>/dev/null || \
    curl -s http://localhost:8080/health/nodes

echo ""
success "Done. Open http://localhost:3000 → Simulate tab → peer cards should show ALIVE within 10s."
echo ""
