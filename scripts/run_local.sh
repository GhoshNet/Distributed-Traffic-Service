#!/usr/bin/env bash
# Run all services locally using the DS conda environment
# Usage: bash scripts/run_local.sh [start|stop|status|logs <service>]

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONDA_ENV="DS"
LOG_DIR="$PROJECT_DIR/logs"
PID_DIR="$PROJECT_DIR/.pids"
CONDA_BASE="$(conda info --base 2>/dev/null)"

mkdir -p "$LOG_DIR" "$PID_DIR"

export PATH="/opt/homebrew/opt/postgresql@16/bin:/opt/homebrew/opt/rabbitmq/sbin:/opt/homebrew/opt/redis/bin:/opt/homebrew/bin:$PATH"

# Shared config
RABBITMQ_URL="amqp://journey_admin:journey_pass@localhost:5672/journey_vhost"
JWT_SECRET="super-secret-jwt-key-change-in-production"
PYTHON="$CONDA_BASE/envs/$CONDA_ENV/bin/python"
UVICORN="$CONDA_BASE/envs/$CONDA_ENV/bin/uvicorn"

start_service() {
    local name=$1   # e.g. "user-service"
    local port=$2
    local db_url=$3
    local redis_db=$4
    local extra_env=$5   # semicolon-separated KEY=VAL pairs

    PID_FILE="$PID_DIR/$name.pid"

    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        echo "  [$name] Already running (PID $(cat "$PID_FILE"))"
        return
    fi

    local service_dir="$PROJECT_DIR/$name"

    # Build environment string
    local env_str="SERVICE_NAME=$name"
    env_str="$env_str PYTHONPATH=$PROJECT_DIR"
    env_str="$env_str RABBITMQ_URL=$RABBITMQ_URL"
    env_str="$env_str REDIS_URL=redis://localhost:6379/$redis_db"
    env_str="$env_str JWT_SECRET=$JWT_SECRET"
    [ -n "$db_url" ] && env_str="$env_str DATABASE_URL=$db_url"
    if [ -n "$extra_env" ]; then
        IFS=';' read -ra extras <<< "$extra_env"
        for e in "${extras[@]}"; do
            env_str="$env_str $e"
        done
    fi

    # Start service in background from its own directory
    (cd "$service_dir" && env $env_str "$UVICORN" app.main:app \
        --host 0.0.0.0 --port "$port" --workers 1 \
        > "$LOG_DIR/$name.log" 2>&1) &

    local pid=$!
    echo "$pid" > "$PID_FILE"
    echo "  [$name] Starting on port $port (PID $pid)"
}

stop_service() {
    local name=$1
    local PID_FILE="$PID_DIR/$name.pid"
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            echo "  [$name] Stopped (PID $pid)"
        else
            echo "  [$name] Not running (stale PID)"
        fi
        rm -f "$PID_FILE"
    else
        echo "  [$name] No PID file"
    fi
}

status_service() {
    local name=$1
    local PID_FILE="$PID_DIR/$name.pid"
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            echo "  [$name] Running (PID $pid)"
        else
            echo "  [$name] Dead (stale)"
            rm -f "$PID_FILE"
        fi
    else
        echo "  [$name] Not running"
    fi
}

case "${1:-start}" in
start)
    echo ""
    echo "Starting Journey Booking System (local mode)..."
    echo "================================================"

    start_service "user-service"     8001 \
        "postgresql+asyncpg://users_user:users_pass@localhost:5432/users_db" 0

    start_service "conflict-service" 8003 \
        "postgresql+asyncpg://conflicts_user:conflicts_pass@localhost:5432/conflicts_db" 2

    start_service "analytics-service" 8006 \
        "postgresql+asyncpg://analytics_user:analytics_pass@localhost:5432/analytics_db" 5

    start_service "notification-service" 8004 "" 3

    start_service "enforcement-service" 8005 "" 1 \
        "JOURNEY_SERVICE_URL=http://localhost:8002"

    # Small delay so conflict-service is ready
    echo "  [info] Waiting 4s before starting journey-service..."
    /bin/sleep 4

    start_service "journey-service" 8002 \
        "postgresql+asyncpg://journeys_user:journeys_pass@localhost:5432/journeys_db" 1 \
        "CONFLICT_SERVICE_URL=http://localhost:8003"

    echo ""
    echo "All services starting. Logs in: $LOG_DIR/"
    echo ""
    echo "Service URLs (use these in tests):"
    echo "  User Service:         http://localhost:8001"
    echo "  Journey Service:      http://localhost:8002"
    echo "  Conflict Service:     http://localhost:8003"
    echo "  Notification Service: http://localhost:8004"
    echo "  Enforcement Service:  http://localhost:8005"
    echo "  Analytics Service:    http://localhost:8006"
    echo ""
    echo "Run demo: conda run -n DS python scripts/demo_local.py"
    echo ""
    ;;

stop)
    echo ""
    echo "Stopping all services..."
    for name in user-service journey-service conflict-service notification-service enforcement-service analytics-service; do
        stop_service "$name"
    done
    echo ""
    ;;

status)
    echo ""
    echo "Service Status:"
    echo "==============="
    for name in user-service journey-service conflict-service notification-service enforcement-service analytics-service; do
        status_service "$name"
    done
    echo ""
    ;;

logs)
    name="${2:-user-service}"
    tail -f "$LOG_DIR/$name.log"
    ;;

*)
    echo "Usage: $0 [start|stop|status|logs <service-name>]"
    exit 1
    ;;
esac
