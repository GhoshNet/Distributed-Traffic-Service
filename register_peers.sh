#!/bin/bash
# register_peers.sh — Live-register peers without redeploying
#
# Works in two modes:
#
#   Mode 1 — bare IPs as args (works with start.sh):
#     ./register_peers.sh 192.168.1.10 192.168.1.11
#
#   Mode 2 — no args, reads .env written by setup_demo.sh (label format):
#     ./register_peers.sh
#
# Safe to run multiple times. All curl failures are non-fatal (shows WARN).

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }

is_peer_reachable() {
    local ip="$1"
    local http
    http=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 \
        "http://${ip}:8080/health" 2>/dev/null || echo "000")
    [ "$http" = "200" ]
}

register_peer_ip() {
    local ip="$1"
    local label="${2:-$ip}"   # display name — label letter or raw IP

    # Reachability check — skip entirely if the peer's gateway isn't responding
    if ! is_peer_reachable "$ip"; then
        warn "Skipping $label ($ip) — not reachable on port 8080 (offline or firewall)"
        return
    fi

    # 1. Conflict-service peer (triggers catch-up sync immediately)
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST http://localhost:8003/internal/peers/register \
        -H "Content-Type: application/json" \
        -d "{\"peer_url\": \"http://${ip}:8003\"}" \
        2>/dev/null || echo "000")
    [ "$HTTP" = "200" ] || [ "$HTTP" = "204" ] \
        && success "Conflict peer $label ($ip:8003) registered + catch-up triggered" \
        || warn    "Conflict peer $label: HTTP $HTTP"

    # 2. Journey-service peer
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST http://localhost:8080/internal/journeys/peers/register \
        -H "Content-Type: application/json" \
        -d "{\"peer_url\": \"http://${ip}:8080\"}" \
        2>/dev/null || echo "000")
    [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ] \
        && success "Journey peer $label ($ip:8080) registered + catch-up triggered" \
        || warn    "Journey peer $label: HTTP $HTTP"

    # 3. User-service peer
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST http://localhost:8080/internal/peers/register \
        -H "Content-Type: application/json" \
        -d "{\"peer_url\": \"http://${ip}:8080\"}" \
        2>/dev/null || echo "000")
    [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ] \
        && success "User peer $label ($ip:8080) registered + catch-up triggered" \
        || warn    "User peer $label: HTTP $HTTP"

    # 4. Health monitor
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
        -X POST http://localhost:8080/admin/peers/register \
        -H "Content-Type: application/json" \
        -d "{\"name\": \"${label}\", \"health_url\": \"http://${ip}:8080/health\"}" \
        2>/dev/null || echo "000")
    [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ] \
        && success "Health monitor $label registered" \
        || warn    "Health monitor $label: HTTP $HTTP"
}

# ── Mode 1: IPs passed as arguments ──────────────────────────────────────────
if [ $# -gt 0 ]; then
    echo ""
    info "Registering ${#}  peer(s) from args: $*"
    echo ""
    for ip in "$@"; do
        register_peer_ip "$ip" "$ip"
        echo ""
    done

# ── Mode 2: Read .env written by setup_demo.sh (label-based format) ───────────
else
    [ -f .env ] || { echo "No .env found. Either run setup_demo.sh first, or pass IPs directly: ./register_peers.sh <IP> [IP2]"; exit 1; }
    source .env

    LABELS=(A B C D E F G H I J)

    # Rebuild IPs array from .env (IP_A, IP_B, ...)
    IPS=()
    for lbl in "${LABELS[@]}"; do
        var="IP_${lbl}"
        val="${!var:-}"
        [ -n "$val" ] && IPS+=("$val") || break
    done

    if [ ${#IPS[@]} -eq 0 ]; then
        echo "No IP_A / IP_B entries in .env. Pass IPs directly: ./register_peers.sh <IP> [IP2]"
        exit 1
    fi

    N=${#IPS[@]}

    # Find my index
    MY_INDEX=-1
    for i in "${!LABELS[@]}"; do
        [ "${LABELS[$i]}" = "${MY_LABEL:-}" ] && MY_INDEX=$i && break
    done

    echo ""
    info "Registering peers for Laptop ${MY_LABEL:-?} ($N nodes total)..."
    echo ""

    for i in "${!IPS[@]}"; do
        if [ "$i" -ne "$MY_INDEX" ]; then
            label="${LABELS[$i]}"
            ip="${IPS[$i]}"
            register_peer_ip "$ip" "laptop-$label"
            echo ""
        fi
    done
fi

# ── Final: show node health ───────────────────────────────────────────────────
info "Node health status:"
curl -s http://localhost:8080/health/nodes 2>/dev/null | python3 -m json.tool 2>/dev/null || \
    curl -s http://localhost:8080/health/nodes 2>/dev/null || \
    warn "Could not reach /health/nodes"

echo ""
success "Done. Peer cards should show ALIVE within 10s at http://localhost:3000"
echo ""
