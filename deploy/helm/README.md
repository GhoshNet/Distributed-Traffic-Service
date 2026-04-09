# Helm charts — Distributed Traffic Service

Scaffolding produced in **Phase 5 + Phase 6** of
[docs/k3s_oracle_cloud_deployment_plan.md](../../docs/k3s_oracle_cloud_deployment_plan.md).

## Layout

```
deploy/helm/
├── traffic-service/          # umbrella chart — installed with `helm install`
│   ├── Chart.yaml            # 6 app subcharts + 4 Postgres + RabbitMQ + Redis deps
│   ├── values.yaml           # shared passwords, backend config, per-app overrides
│   ├── charts/               # populated by `helm dep update`
│   └── templates/
│       ├── secret.yaml       # traffic-secrets (JWT, composed DB/Redis/AMQP URLs)
│       ├── configmap.yaml    # traffic-config (Redis sentinel addrs, etc.)
│       └── ingress.yaml      # single ingress routing /api/* to each service
└── charts/                   # per-service app subcharts (Phase 5)
    ├── user-service/
    ├── journey-service/
    ├── conflict-service/
    ├── notification-service/
    ├── enforcement-service/
    └── analytics-service/
```

## App subchart templates

Every app subchart ships the same 5 templates:

| File                | Purpose                                                              |
|---------------------|----------------------------------------------------------------------|
| `deployment.yaml`   | Deployment with pod anti-affinity on `topology.kubernetes.io/zone`   |
| `service.yaml`      | ClusterIP service on port 8000                                       |
| `hpa.yaml`          | HorizontalPodAutoscaler (CPU-based, 2–6 replicas)                    |
| `pdb.yaml`          | PodDisruptionBudget (minAvailable: 1)                                |
| `_helpers.tpl`      | Shared label / selector named templates                              |

Only `values.yaml` differs between subcharts (env vars, service name, image
repo). Keeping templates identical means a fix in one place (say, adding
`topologySpreadConstraints`) propagates via a single `cp`.

## Phase 6 — stateful data plane

The umbrella `Chart.yaml` now pulls **six** external dependencies from the
Bitnami repo:

| Dep                 | Alias(es)                                   | Pattern                                  |
|---------------------|---------------------------------------------|------------------------------------------|
| `postgresql-ha`     | `postgres-users`, `postgres-journeys`       | Primary + standby + repmgr + pgpool      |
| `postgresql`        | `postgres-conflicts`, `postgres-analytics`  | Standalone (memory saver for 8 GiB nodes)|
| `rabbitmq`          | —                                           | 3-node cluster, hard anti-affinity       |
| `redis`             | —                                           | Replication + Sentinel (quorum: 2)       |

**Why 2 HA + 2 standalone?** All four Postgres HA releases on 3× 8 GiB Ampere
A1 nodes leaves almost no headroom for the app pods. Users/journeys are the
"hot" services that benefit most from HA; conflicts/analytics run
standalone. Switch any of them back to HA in `values.yaml` when you move
off free-tier hardware.

### Password management

All backend passwords live in a single `_passwords` block in
`traffic-service/values.yaml` and are referenced everywhere via **YAML
anchors** (`&name` / `*name`). This means:

- Editing a password in `_passwords` automatically propagates to (a) the
  Bitnami subchart config, and (b) the composed DB/Redis/AMQP URLs in
  `traffic-secrets`.
- No risk of the app's `DATABASE_URL` drifting from the actual Postgres
  password — they're the same literal string at render time.

The `templates/secret.yaml` template composes each URL at Helm render time
using `printf`, e.g.:

```yaml
users-database-url: {{ printf "postgresql+asyncpg://%s:%s@%s:5432/%s"
    $db.users.username $db.users.password $db.users.host $db.users.database | quote }}
```

## Usage

```bash
cd deploy/helm/traffic-service

# 1. Add Bitnami repo (once) and resolve every dependency
helm repo add bitnami https://charts.bitnami.com/bitnami
helm dependency update

# 2. Dry-run render (Phase 5 + 6 checkpoint)
helm template traffic . > /tmp/rendered.yaml
helm lint .

# 3. Install (Phase 7 — after Phase 4 images exist and secrets.yaml is ready)
helm install traffic . -n traffic --create-namespace -f secrets.yaml
```

A clean render currently produces **89 resources**:

| Kind                     | Count |
|--------------------------|------:|
| Service                  | 20    |
| PodDisruptionBudget      | 16    |
| Secret                   | 10    |
| NetworkPolicy            | 8     |
| Deployment               | 8     |
| StatefulSet              | 6     |
| ServiceAccount           | 6     |
| HorizontalPodAutoscaler  | 6     |
| ConfigMap                | 6     |
| Role / RoleBinding       | 2     |
| Ingress                  | 1     |

The 6 StatefulSets are: postgres-users (HA), postgres-journeys (HA),
postgres-conflicts, postgres-analytics, rabbitmq (3 replicas), redis
(3 replicas with sentinel).

## Gotchas discovered during Phase 6

1. **Do NOT set `global.imageRegistry`** at the umbrella level. Bitnami
   charts consume it and rewrite every `docker.io/bitnami/<img>` to
   `<yourRegistry>/bitnami/<img>`, which breaks the pulls and trips the
   "non-standard containers" guard in their NOTES.txt. App image repos are
   configured per-subchart under `<service>.image.repository` instead.
2. **Erlang cookie length** — RabbitMQ requires the cookie be at least 32
   characters. The placeholder in `_passwords.rabbitmqCookie` is long
   enough; don't shorten it when replacing.
3. **Redis sentinel DNS** — the default host names in `config.redisSentinelAddrs`
   assume `fullnameOverride: redis` on the Redis release. If you rename
   the release, update those addresses.
4. **Bitnami free chart access** — as of 2025 Bitnami is pushing their
   paid "secure images". The charts themselves remain free but image
   availability can change. If a pull starts failing, substitutions to
   consider: CloudNativePG (operator-based Postgres), `redis-operator`
   from ot-container-kit, or the official `rabbitmq-cluster-operator`.

## Still to do (Phase 7)

- **Frontend** — add a `frontend` subchart or Deployment serving the
  static files from `frontend/`; route `/` to it in the Ingress.
- **Real secrets** — every `REPLACE_ME_*` in `values.yaml` must be
  overridden. Recommended: copy `values.yaml` → `secrets.yaml`, strip
  everything except the `_passwords` + `secrets` blocks, gitignore it,
  pass via `helm install -f secrets.yaml`.
- **Image registry** — replace `ghcr.io/REPLACE_ME/<service>` in each
  per-app block with your real GHCR namespace after Phase 4.
- **Ingress host** — set `ingress.host` to `<node-public-ip>.nip.io` or a
  real domain.
