#!/bin/bash
set -e
# Allow replication connections from any host in the Docker network (trust for internal demo network)
echo "host replication all all trust" >> "${PGDATA}/pg_hba.conf"
echo "[init] Replication pg_hba.conf entry added."
