# High Availability Deployment: Strategy & Execution Runbook

This document serves as the comprehensive guide for transitioning the **Distributed Traffic Service** from a prototype into a production-ready, fault-tolerant system deployed across multiple virtual machines.

---

## Part 1: Deployment Strategy

### Why Docker Swarm?
For a distributed systems demo, Docker Swarm provides the ideal balance between complexity and power:
- **Zero-Rewrite Pipeline:** It natively consumes the existing `docker-compose` logic.
- **Built-in Load Balancing:** The ingress routing mesh automatically handles traffic across pods, even during node failures.
- **Self-Healing:** Swarm continuously monitors container health and automatically re-provisions failed replicas on healthy nodes.

### Infrastructure Environment: Oracle Cloud (Always Free)
The target environment is a 3-node cluster using **ARM64 (Ampere A1)** instances. 
- **Topology:** 1 Manager node, 2 Worker nodes.
- **Redundancy:** Each node is assigned to a different **Fault Domain** (Availability Zone) to ensure the system survives physical data center failures.

---

## Part 2: Networking & Connectivity

### Bypassing Network Restrictions
If deploying from a restricted environment (like a college Wi-Fi), use one of these strategies:
1.  **Tailscale (Recommended):** Create a peer-to-peer VPN between physical laptops to bypass client isolation.
2.  **Cloud VCN:** Use a private Cloud network (as detailed in the Oracle Cloud guide) to keep inter-node traffic isolated from your local network.

### Required Port Mappings
The following ports MUST be open in both the Cloud Security Lists and the local OS firewall:
| Port | Protocol | Purpose |
| :--- | :--- | :--- |
| 2377 | TCP | Swarm Cluster Management |
| 7946 | TCP/UDP | Node Discovery / Gossip |
| 4789 | UDP | Overlay Network traffic (VXLAN) |
| 8080 | TCP | API Gateway (HAProxy) Entry |
| 3000 | TCP | Frontend Web Application |

---

## Part 3: Execution Runbook

### 🚀 Phase 1: OS Preparation (On ALL Nodes)
Apply these firewall rules and install the Docker engine:
```bash
# Update local firewall
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 2377 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 7946 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p udp --dport 7946 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p udp --dport 4789 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8080 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 3000 -j ACCEPT
sudo netfilter-persistent save

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker ubuntu
```
*Note: Log out and back in after adding the user to the docker group.*

### 🔗 Phase 2: Building the Cluster
1.  **Initialize Manager:** Run `docker swarm init --advertise-addr <PRIVATE_IP>` on the manager node.
2.  **Join Workers:** Paste the generated `docker swarm join` command on both worker nodes.
3.  **Verify:** Run `docker node ls` on the manager to see all 3 nodes marked as "Ready".

### 📦 Phase 3: Deployment
1.  **Transfer Code:** Upload your project folder to the manager node.
2.  **Deploy Stack:**
    ```bash
    chmod +x deploy-swarm.sh
    ./deploy-swarm.sh
    ```
    *This script builds ARM64 images, pushes them to the local Swarm registry, and deploys the `traffic-service` stack.*

---

## Part 4: Verification & Fault Tolerance Demo

### Functional Check
- **Service Status:** Run `docker service ls`. Wait for all replicas to reach `2/2`.
- **Live Access:** Access the dashboard at `http://<ANY_NODE_PUBLIC_IP>:3000`.

### Fault Tolerance Demo (Kill Test)
To demonstrate the system's resilience:
1.  Identify where a service is running: `docker service ps traffic-service_user-service`.
2.  Power off that node or run `sudo systemctl stop docker` on it.
3.  **Observe:** Swarm will detect the failure and immediately spin up new replicas on the remaining healthy nodes. The frontend (`:3000`) and API (`:8080`) will remain accessible throughout the process.

---

> [!IMPORTANT]
> The Postgres and RabbitMQ nodes require an initial sync period of ~2 minutes. If you experience "Database not ready" errors immediately after a fresh deployment, wait for the replication logs to finalize.
