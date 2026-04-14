#!/bin/bash
set -e

echo "==========================================="
echo " Deploying Distributed Traffic Service to Swarm"
echo "==========================================="

# We assume Docker Swarm is already initialized.

echo "[1/4] Ensuring local registry is running..."
# Start the registry separately first so we can push to it before the full stack deploy
docker service ls | grep "registry" || docker service create --name registry --publish published=5000,target=5000 registry:2

# Give registry a second to boot securely
sleep 5

echo "[2/4] Building custom Docker images..."
SERVICES=("user-service" "journey-service" "conflict-service" "notification-service" "enforcement-service" "analytics-service")

for service in "${SERVICES[@]}"; do
    echo "  > Building $service..."
    docker build -t 127.0.0.1:5000/$service:latest -f $service/Dockerfile .
done

echo "  > Building postgres-custom (with replication init)..."
docker build -t 127.0.0.1:5000/postgres-custom:latest -f postgres-custom/Dockerfile postgres-custom/

echo "[3/4] Pushing images to local Swarm registry..."
for service in "${SERVICES[@]}"; do
    echo "  > Pushing $service..."
    docker push 127.0.0.1:5000/$service:latest
done

echo "  > Pushing postgres-custom..."
docker push 127.0.0.1:5000/postgres-custom:latest

echo "[4/4] Deploying stack via docker-compose.swarm.yml..."
docker stack deploy -c docker-compose.swarm.yml traffic-service

echo "==========================================="
echo "Deployment triggered! Run 'docker service ls' to monitor deployment status."
echo "==========================================="
