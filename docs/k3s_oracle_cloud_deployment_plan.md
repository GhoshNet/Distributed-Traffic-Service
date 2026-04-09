# k3s on Oracle Cloud — Step-by-Step Deployment Plan

Target: deploy the Distributed Traffic Service as a true multi-node Kubernetes
cluster on Oracle Cloud's Always Free ARM tier, packaged with Helm. Each phase
produces a working, verifiable checkpoint — don't skip the verification steps,
they're what you'll screenshot for the report.

---

## Phase 0 — Prerequisites (local, ~30 min)

- [ ] Oracle Cloud free-tier account with ARM A1 quota available (4 OCPU / 24 GB total)
- [ ] GitHub account for container registry (GHCR — free for public images)
- [ ] Install locally: `kubectl`, `helm` (v3.14+), `docker buildx` (for ARM64 cross-builds from your Windows box)
- [ ] SSH keypair generated for VM access
- [ ] Decide a domain name or just use `nip.io` (e.g., `<node-ip>.nip.io`) so you don't need to buy DNS

**Checkpoint:** `kubectl version --client` and `helm version` both work.

---

## Phase 1 — Provision Oracle Cloud infrastructure (~45 min)

1. **Create VCN** `traffic-vcn` with CIDR `10.0.0.0/16`, public subnet `10.0.1.0/24`.
2. **Security List — Ingress rules** (source `0.0.0.0/0` unless noted):
   - TCP 22 (SSH — restrict to your IP)
   - TCP 6443 (k3s API)
   - TCP 80, 443 (ingress HTTP/S)
   - UDP 8472 (Flannel VXLAN — source `10.0.0.0/16` only)
   - TCP 10250 (kubelet — source `10.0.0.0/16` only)
   - TCP 2379-2380 (etcd — source `10.0.0.0/16` only, HA mode only)
3. **Create 3× `VM.Standard.A1.Flex` instances**, Ubuntu 24.04 aarch64, **1 OCPU / 8 GB each**:
   - `k3s-server` → Fault Domain 1
   - `k3s-agent-1` → Fault Domain 2
   - `k3s-agent-2` → Fault Domain 3
4. On **each VM**, open host firewall:
   ```bash
   sudo iptables -I INPUT 6 -p tcp -m multiport --dports 22,80,443,6443,10250 -j ACCEPT
   sudo iptables -I INPUT 6 -p udp --dport 8472 -j ACCEPT
   sudo netfilter-persistent save
   ```

**Checkpoint:** You can SSH into all 3 nodes and ping between them by private IP.

---

## Phase 2 — Install k3s cluster (~20 min)

1. **On `k3s-server`** (replace `<PRIVATE_IP>` / `<PUBLIC_IP>`):
   ```bash
   curl -sfL https://get.k3s.io | sh -s - server \
     --node-external-ip=<PUBLIC_IP> \
     --advertise-address=<PRIVATE_IP> \
     --flannel-backend=vxlan \
     --disable=traefik \
     --write-kubeconfig-mode=644
   sudo cat /var/lib/rancher/k3s/server/node-token   # save this
   ```
   *Traefik is disabled because we'll install `ingress-nginx` — more standard, nicer for the report.*

2. **On both agents**:
   ```bash
   curl -sfL https://get.k3s.io | K3S_URL=https://<SERVER_PRIVATE_IP>:6443 \
     K3S_TOKEN=<token-from-step-1> sh -
   ```

3. **Copy kubeconfig to your laptop**:
   ```bash
   scp ubuntu@<SERVER_PUBLIC_IP>:/etc/rancher/k3s/k3s.yaml ~/.kube/config-traffic
   # edit: replace 127.0.0.1 → <SERVER_PUBLIC_IP>
   export KUBECONFIG=~/.kube/config-traffic
   ```

4. **Label nodes** for scheduling / anti-affinity:
   ```bash
   kubectl label node k3s-server   topology.kubernetes.io/zone=fd1
   kubectl label node k3s-agent-1  topology.kubernetes.io/zone=fd2
   kubectl label node k3s-agent-2  topology.kubernetes.io/zone=fd3
   ```

**Checkpoint:** `kubectl get nodes -o wide` from your laptop shows 3 `Ready` nodes.

---

## Phase 3 — Cluster-wide infrastructure (~30 min)

Install once, used by everything. Use Helm for all of it.

1. **ingress-nginx** (exposes everything on node ports 80/443):
   ```bash
   helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx
   helm install ingress ingress-nginx/ingress-nginx -n ingress-nginx --create-namespace \
     --set controller.service.type=NodePort \
     --set controller.service.nodePorts.http=30080 \
     --set controller.service.nodePorts.https=30443
   ```
2. **local-path-provisioner** — already bundled with k3s, check with `kubectl get sc`. Set as default storage class.
3. **cert-manager** (optional, only if you want real HTTPS):
   ```bash
   helm repo add jetstack https://charts.jetstack.io
   helm install cert-manager jetstack/cert-manager -n cert-manager --create-namespace --set crds.enabled=true
   ```
4. **kube-prometheus-stack** (for report screenshots — this alone earns marks):
   ```bash
   helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
   helm install monitoring prometheus-community/kube-prometheus-stack -n monitoring --create-namespace
   ```

**Checkpoint:** `curl http://<server-public-ip>:30080` returns the ingress-nginx 404 page. Grafana reachable via `kubectl port-forward`.

---

## Phase 4 — Build & push ARM64 images (~1 hr)

You have 6 services with Dockerfiles (`user-service/Dockerfile` etc.). Oracle A1 is **ARM64** — you must cross-build from your Windows/x86 machine.

1. **GHCR login** on your laptop:
   ```bash
   echo $GHCR_PAT | docker login ghcr.io -u <username> --password-stdin
   ```
2. **Create buildx builder** (one-time):
   ```bash
   docker buildx create --name multi --use
   ```
3. **Build & push all services** (script this):
   ```bash
   for svc in user-service journey-service conflict-service \
              notification-service enforcement-service analytics-service; do
     docker buildx build --platform linux/arm64 \
       -f $svc/Dockerfile \
       -t ghcr.io/<user>/$svc:v1 --push .
   done
   ```
4. Make the packages **public** in GHCR UI (otherwise k3s needs an imagePullSecret).

**Checkpoint:** Each image visible in your GHCR packages page, `arm64` variant present.

---

## Phase 5 — Helm umbrella chart scaffolding (~2 hrs)

Create `deploy/helm/` in the repo:

```
deploy/helm/
├── traffic-service/          # umbrella chart
│   ├── Chart.yaml            # declares all subchart deps
│   ├── values.yaml           # global config
│   └── charts/               # (populated by `helm dep update`)
└── charts/                   # your own subcharts
    ├── user-service/
    ├── journey-service/
    ├── conflict-service/
    ├── notification-service/
    ├── enforcement-service/
    └── analytics-service/
```

1. `helm create charts/user-service` — then strip the default NOTES/tests, keep `deployment.yaml`, `service.yaml`, `hpa.yaml`, `ingress.yaml`.
2. In each subchart's `deployment.yaml` add:
   - `replicas: 2` (from values)
   - **Pod anti-affinity** on `topology.kubernetes.io/zone` → forces replicas across fault domains (this is the money shot for the report)
   - Readiness/liveness probes hitting `/health` (services already expose this)
   - Env vars from a `ConfigMap` + `Secret` (DB URLs, JWT secret, RabbitMQ URL)
   - `resources.requests` small (100m CPU / 128Mi) so they fit in 8 GB nodes
3. Add `PodDisruptionBudget` (`minAvailable: 1`) per service.
4. **Umbrella `Chart.yaml`** declares third-party dependencies:
   ```yaml
   dependencies:
     - name: postgresql-ha
       version: "~14.x"
       repository: https://charts.bitnami.com/bitnami
       alias: postgres-users
     # ...repeat alias for journeys, conflicts, analytics
     - name: rabbitmq-cluster-operator
       repository: https://charts.bitnami.com/bitnami
     - name: redis
       repository: https://charts.bitnami.com/bitnami
       # enable sentinel in values
   ```
5. `cd deploy/helm/traffic-service && helm dep update`

**Checkpoint:** `helm template traffic-service ./traffic-service` renders without errors.

---

## Phase 6 — Stateful dependencies (~1.5 hrs)

Replace the hand-rolled primary/replica shell scripts from `docker-compose.yml`. **Do not port them** — the community charts do it better.

1. **Postgres (×4 instances)** — one `postgresql-ha` release per DB (users/journeys/conflicts/analytics), each with 1 primary + 1 standby + Repmgr. Configure in `values.yaml`:
   ```yaml
   postgres-users:
     postgresql:
       replicaCount: 2
       database: users_db
       username: users_user
     persistence:
       size: 2Gi
   ```
2. **RabbitMQ** — install the **RabbitMQ Cluster Operator** once, then a `RabbitmqCluster` CR with `replicas: 3` and quorum queues. Replaces all the `join_cluster` shell in the Compose file.
3. **Redis Sentinel** — Bitnami `redis` chart with `sentinel.enabled=true`, `replica.replicaCount=2`. Replaces the 3 hand-rolled sentinel containers.
4. Create a `Secret` with all DB/AMQP passwords; reference it from app deployments.

**Checkpoint:**
- `kubectl get pods -n traffic` shows all statefulset pods `Running`
- `kubectl exec` into one app pod → can reach each DB and RabbitMQ by service DNS
- `kubectl get pdb,hpa` shows expected entries

---

## Phase 7 — Deploy apps + ingress (~45 min)

1. Fill in values for each app subchart: image repo/tag, env, dependencies.
2. Create one `Ingress` resource routing paths to services (mirrors `api-gateway/nginx.conf`):
   ```
   /api/users/*         → user-service
   /api/journeys/*      → journey-service
   /api/conflicts/*     → conflict-service
   /api/notifications/* → notification-service
   /api/enforcement/*   → enforcement-service
   /api/analytics/*     → analytics-service
   /                    → frontend
   ```
3. **Frontend** — either a tiny Deployment running `nginx:alpine` with static files baked into a custom image, or serve via a ConfigMap mount.
4. Install:
   ```bash
   helm install traffic ./deploy/helm/traffic-service -n traffic --create-namespace
   ```

**Checkpoint:** `http://<node-public-ip>:30080/` loads the frontend; a login/journey-create round-trip works end-to-end.

---

## Phase 8 — Fault tolerance demonstration (~30 min, this is your demo script)

Record each of these for the report:

1. **Rolling update**: `helm upgrade traffic ... --set userService.image.tag=v2` → watch `kubectl rollout status`.
2. **Pod kill**: `kubectl delete pod -l app=journey-service --force` → replacement pod schedules on a different node within seconds.
3. **Node kill**: `sudo systemctl stop k3s-agent` on `k3s-agent-1`. Show:
   - `kubectl get nodes` → `NotReady`
   - Pods reschedule to remaining nodes
   - Frontend still reachable (thanks to anti-affinity + PDB)
4. **Postgres failover**: kill the primary pod → Repmgr promotes the standby → writes resume.
5. **HPA under load**: `kubectl run -it load --image=busybox -- wget -q -O- ...` in a loop → `kubectl get hpa` shows replica count climbing.
6. **Scale test**: `kubectl scale deploy/journey-service --replicas=6` → verify distribution across nodes with `kubectl get pods -o wide`.

**Checkpoint:** Screenshots + `kubectl` output captured for each scenario → paste into `docs/final_report.tex`.

---

## Phase 9 — Observability + polish (~1 hr)

1. **Grafana dashboards** — expose via ingress, import dashboard IDs 315 (Kubernetes cluster) and 7249 (nginx). Screenshot.
2. **ServiceMonitors** — if services expose Prometheus metrics, wire them to kube-prometheus-stack.
3. **CI** (optional but impressive): GitHub Actions workflow that builds multi-arch images on push to `main` and runs `helm upgrade` via a kubectl context.

---

## Estimated total first-time effort

~8-10 hours spread over 2-3 sessions. Phases 1-3 are mostly waiting for VMs/installs; phases 5-7 are the actual engineering.

---

## Risks / gotchas to watch for

- **Oracle free tier reclamation**: ARM instances get reclaimed if idle. Run a tiny cron that hits the API every hour.
- **Memory**: 8 GB per node is tight with 4 Postgres HA pairs. Set aggressive `resources.requests` and use single-instance `postgresql` instead of `postgresql-ha` for 2 of the 4 DBs if you hit OOMs.
- **Image pulls**: first deploy will be slow over Oracle's egress — expect 5-10 min.
- **ARM compatibility**: verify the Python base image in each `Dockerfile` has an arm64 variant (most `python:3.x-slim` tags do).
