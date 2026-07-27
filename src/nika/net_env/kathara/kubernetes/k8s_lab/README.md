# k8s_lab — Fat-Tree BGP + k3s

Two-pod fat-tree datacenter network with EBGP (FRR) and a k3s cluster running sample microservices.

![Topology](../../../../../assets/images/kathara_k3s_lab_topo.png)

## Topology

| Layer | Devices | AS |
|-------|---------|-----|
| Core | `core_1_1`, `core_1_2` | 64518 |
| Spine (pod 1) | `spine_1_1`, `spine_1_2` | 64514 |
| Leaf (pod 1) | `leaf_1_1`, `leaf_1_2` | 64512, 64513 |
| Spine (pod 2 / exit) | `spine_2_1`, `spine_2_2` | 64517 |
| Leaf (pod 2 / exit) | `leaf_2_1`, `leaf_2_2` | 64515, 64516 |
| Exit | `dc_exit` | — |
| External | `as1r1`, `as2r1`, `client` | AS1, AS2 |

The exit PoD (`spine_2_*`, `leaf_2_*`, `dc_exit`) and external AS (`as1r1`, `as2r1`, `client`) can be removed to keep only the pod-1 fat-tree with more worker nodes.

Inter-switch links use BGP unnumbered (IPv6-enabled FRR). Leaf routers advertise host subnets to the fabric.

## k3s Nodes

| Node | Leaf | Subnet |
|------|------|--------|
| `controller` | `leaf_1_1` | 201.1.1.0/24 |
| `worker1`, `worker2` | `leaf_1_1` | 201.1.2.0/24, 201.1.3.0/24 |
| `worker3`, `worker4`, `worker5` | `leaf_1_2` | 201.2.1.0/24, 201.2.2.0/24, 201.2.3.0/24 |

k3s starts with `--disable servicelb --disable traefik`. Workers join via `K3S_URL=https://controller:6443`.

## Control-Plane Services (`controller/k8s/`)

Applied automatically by `controller.startup`:

| Component | File | Role |
|-----------|------|------|
| MetalLB (BGP mode) | `metallb-frr.yaml`, `metallb-conf.yaml` | Announces LoadBalancer IPs (101.0.0.0/8) via BGP to leaf routers (ASN 65001 → 64512/64513) |
| NGINX Ingress | `ingress-nginx.yaml` | HTTP ingress for cluster services |
| PersistentVolumes | `pv.yaml` | Local storage on workers |

## Sample Workloads (`shared/`)

| App | Namespace | Components |
|-----|-----------|------------|
| word | `word-ns` | app + PostgreSQL, exposed at `datacenter.com/word` |
| weather | `weather-ns` | app + PostgreSQL, exposed at `datacenter.com/weather` |

Manifests: `ns.yaml`, `deploy_db.yaml`, `deploy_app.yaml`, `svc.yaml`, `ing.yaml`, `pvc.yaml`.

## Verification

Deploy the lab and wait for `controller.startup` to finish (k3s + image pulls can take a while):

```shell
nika env run k8s_lab
```

### Device names vs container names

Topology and lab configs use **device names** (`controller`, `client`, `leaf_1_1`, …). After deployment, Docker containers are named differently:

```
kathara_{user}_{device}_{lab_hash}
```

Example: device `controller` → container `kathara_p4_controller_7f3a2b1c`.

| Tool | What to pass as host |
|------|----------------------|
| `nika exec HOST …` | Device name — auto-resolved via session |
| `kathara connect/exec HOST …` | Device name (same lab instance) |
| `docker exec -it …` | Full container name |

List a running lab's devices:

```shell
kathara list
docker ps --format '{{.Names}}\t{{.Label "name"}}' | grep k8s_lab
```

`kubectl get nodes` shows lab device names (`controller`, `worker1`, …): Kathara sets each container's hostname to the device name, and k3s uses the hostname as the node name. Run `kubectl` on the k3s server device **`controller`** — workers ship the binary but have no kubeconfig.

### 1. BGP fabric — device `leaf_1_1`

| Command | Expected |
|---------|----------|
| `vtysh -c 'show bgp summary'` | Spine neighbors in **Established** state |
| `vtysh -c 'show ip route 101.0.0.0/8'` | Route to MetalLB pool via BGP |

```shell
nika exec leaf_1_1 vtysh -c 'show bgp summary' --timeout 30
nika exec leaf_1_1 vtysh -c 'show ip route 101.0.0.0/8' --timeout 30
```

### 2. k3s cluster — device `controller` (k3s server)

| Command | Expected |
|---------|----------|
| `kubectl get nodes` | 6 nodes **Ready** |
| `kubectl get pods -A` | All pods **Running** |
| `kubectl get svc -n ingress-nginx ingress-nginx-controller` | **EXTERNAL-IP** in `101.0.0.0/8` (typically `101.0.0.1`) |

```shell
nika exec controller kubectl get nodes --timeout 30
nika exec controller kubectl get svc -n ingress-nginx ingress-nginx-controller --timeout 30
```

Cross-leaf reachability (pod-1 fabric):

```shell
nika exec controller ping -c 3 201.2.1.2 --timeout 30   # worker3 on leaf_1_2
```

### 3. External client — device `client`

`client` (3.0.0.2) reaches the DC through `as2r1 → as1r1 → dc_exit`. `/etc/hosts` maps `101.0.0.1` → `datacenter.com`.

| Command | Expected |
|---------|----------|
| `ping -c 3 201.1.1.2` | Replies from k3s controller |
| `ping -c 3 101.0.0.1` | Replies from ingress LoadBalancer VIP |
| `curl -s http://datacenter.com/word` | HTTP 200, word app response |
| `curl -s http://datacenter.com/weather` | HTTP 200, weather app response |

```shell
nika exec client ping -c 3 201.1.1.2 --timeout 30
nika exec client ping -c 3 101.0.0.1 --timeout 30
nika exec client curl -s http://datacenter.com/word --timeout 30
nika exec client curl -s http://datacenter.com/weather --timeout 30
```
