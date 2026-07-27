# llmd_lab — Star Topology + llm-d P/D Disaggregation

Single-switch star network with a k3s cluster running llm-d disaggregated Prefill/Decode inference. No GPUs — computation is simulated.

## Topology

```
                    ┌─────────────┐
                    │  controller │
                    └──────┬──────┘
                           │
     worker1 ─ worker2 ─ worker3 ─ worker4 ─ worker5 ─ client
                           │
                      (link A, bridged)
```

All nodes connect to one L2 domain (link `A`). k3s nodes are **bridged** to the host for internet access (image pulls). Subnet: `200.0.0.0/24`.

| Node | IP | Role |
|------|----|------|
| `controller` | 200.0.0.1 | k3s server |
| `worker1`–`worker5` | 200.0.0.2–6 | k3s agents |
| `client` | 200.0.0.7 | test client |

k3s starts with `--disable servicelb --disable traefik`.

## Control-Plane Services (`controller/k8s/`)

Applied automatically by `controller.startup`:

| Component | File(s) | Role |
|-----------|---------|------|
| MetalLB (L2 mode) | `metallb.yaml`, `metallb-l2.yaml` | LoadBalancer IPs 200.0.0.240–250 |
| Gateway API | `gateway-api-base.yaml` | CRDs and base controllers |
| Inference extensions | `gateway-api-inference.yaml` | InferencePool / EPP CRDs |
| agentgateway | Helm (OCI) | Gateway with `inferenceExtension.enabled=true` |

## llm-d P/D Stack (`pd-*.yaml`)

| File | Component |
|------|-----------|
| `pd-00-namespace.yaml` | `llm-d` namespace |
| `pd-01-configmaps.yaml` | EPP scheduling config; inference-sim timing params |
| `pd-02-rbac.yaml` | ServiceAccount / RBAC for EPP |
| `pd-03-epp.yaml` | Endpoint Picker (`llm-d-inference-scheduler`) — routes requests to prefill/decode |
| `pd-04-prefill.yaml` | 3× prefill pods (`llm-d-inference-sim`) |
| `pd-05-decode.yaml` | 2× decode pods (routing sidecar + `llm-d-inference-sim`) |
| `pd-06-inference-pool.yaml` | `InferencePool` `llm-d-pd` backed by EPP |
| `pd-07-gateway.yaml` | `Gateway` + `HTTPRoute` → InferencePool |

Traffic flow: **Gateway → HTTPRoute → InferencePool → EPP → prefill/decode pods**.

Since there are no GPUs, all inference uses **`llm-d-inference-sim`** with configurable latency in `inference-sim-config` (random mode, ~500 ms TTFT, ~50 ms inter-token).

## Verification

Deploy the lab and wait for `controller.startup` to finish (Helm installs + image pulls can take a while):

```shell
nika env run llmd_lab
```

### Device names vs container names

Topology and lab configs use **device names** (`controller`, `client`, `worker1`, …). After deployment, Docker containers are named differently:

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
docker ps --format '{{.Names}}\t{{.Label "name"}}' | grep llmd_lab
```

`kubectl get nodes` shows lab device names (`controller`, `worker1`, …): Kathara sets each container's hostname to the device name, and k3s uses the hostname as the node name. Run `kubectl` on the k3s server device **`controller`** — workers ship the binary but have no kubeconfig.

### 1. L2 connectivity — device `client`

All nodes share link `A` on `200.0.0.0/24`.

| Command | Expected |
|---------|----------|
| `ping -c 3 200.0.0.1` | Replies from controller |
| `ping -c 3 200.0.0.2` | Replies from worker1 |

```shell
nika exec client ping -c 3 200.0.0.1 --timeout 30
nika exec client ping -c 3 200.0.0.2 --timeout 30
```

### 2. k3s cluster — device `controller` (k3s server)

| Command | Expected |
|---------|----------|
| `kubectl get nodes` | 6 nodes **Ready** |
| `kubectl get pods -n llm-d` | prefill, decode, EPP pods **Running** |
| `kubectl get pods -n agentgateway-system` | agentgateway pods **Running** |
| `kubectl get gateway -n llm-d` | `llm-d-gateway` has an address assigned |

```shell
nika exec controller kubectl get nodes --timeout 30
nika exec controller kubectl get pods -n llm-d --timeout 30
nika exec controller kubectl get gateway -n llm-d --timeout 30
```

Gateway LoadBalancer IP should fall in `200.0.0.240–250` (preconfigured as `llmd` in `client/etc/hosts`):

```shell
nika exec controller kubectl get svc -A --field-selector spec.type=LoadBalancer --timeout 30
```

### 3. End-to-end inference — device `client`

`/etc/hosts` maps `200.0.0.240` → `llmd` (Gateway VIP).

| Command | Expected |
|---------|----------|
| `ping -c 3 llmd` | Replies from Gateway LoadBalancer |
| `curl -s http://llmd/v1/models` | JSON model list |
| `curl -s -X POST http://llmd/v1/chat/completions -H 'Content-Type: application/json' -d '{"model":"llm-d-sim","messages":[{"role":"user","content":"Hello"}]}'` | JSON completion response |

```shell
nika exec client ping -c 3 llmd --timeout 30
nika exec client curl -s http://llmd/v1/models --timeout 30
nika exec client curl -s -X POST http://llmd/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"llm-d-sim","messages":[{"role":"user","content":"Hello"}]}' \
  --timeout 120
```
