#!/bin/bash
set -e
# Allow replication connections from any host in the Docker network (md5 auth)
echo "host replication all all md5" >> "${PGDATA}/pg_hba.conf"
echo "[init] Replication pg_hba.conf entry added."
