#!/bin/bash
set -e
# Allow replication connections from any host in the Docker/Swarm network (trust — internal only)
echo "host replication all all trust" >> "${PGDATA}/pg_hba.conf"
# Also grant the REPLICATION privilege to all DB users so pg_basebackup works
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    ALTER USER "$POSTGRES_USER" REPLICATION;
EOSQL
echo "[init] Replication pg_hba.conf entry added and REPLICATION privilege granted."
