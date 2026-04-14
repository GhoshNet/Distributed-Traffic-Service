FROM postgres:16-alpine
COPY init/01_allow_replication.sh /docker-entrypoint-initdb.d/01_allow_replication.sh
RUN sed -i 's/\r$//' /docker-entrypoint-initdb.d/01_allow_replication.sh \
 && chmod +x /docker-entrypoint-initdb.d/01_allow_replication.sh
