# Start-state consistency experiment

Measure whether every trial of an environment starts from the same network state. The experiment deploys (native) or restores (Firecracker) an environment repeatedly, exports each node's forwarding state at the point where a trial would inject its incident, and decides with [KATch](https://github.com/julesjacobs/katch) whether two runs forward packets identically. Use it to compare NIKA's native redeploys with Firecracker snapshot restores, or to check whether a new environment converges deterministically.

Native redeploys of one configuration can converge to different data planes: equal-cost next hops are installed in the order routes arrive, and Linux sends each flow to the next hop at a fixed position in that list. Under Firecracker execution, every trial restores the same memory image, so this order is fixed.

The experiment is opt-in. It calls NIKA's deploy, restore and injection workflows and reads nodes through the session's MCP `exec_shell` tool, the same interface agents use; normal benchmark runs do not invoke it.

## Collect runs

Collect 10 runs of one environment variant into `runtime/convergence/<variant>/`:

```shell
uv run python -m experiment.convergence.collect --mode native \
  --scenario dc_clos -s m --out runtime/convergence/dc_clos_m
```

Each run writes `<mode>/run_NN/meta.json` and one `dump_<t>.json` per dump delay (`--delays`, default `0,60` seconds after the environment is ready). The last dump also holds per-flow probes: `ip route get` for sampled source and destination addresses through every ECMP route, which records the next hop the kernel picks for each flow. Where a router hashes on layer 4 (`fib_multipath_hash_policy=1`, as on `k8s_lab`'s core routers), a probe stands for the flow with protocol 0 and no ports.

ISP topologies take their routing variant as options, for example `--igp isis --bgp-mode ibgp_rr`. Add `--backend containerlab` to deploy SR Linux routers.

To collect Firecracker restores, set up Firecracker execution as in [Run benchmark trials in Firecracker](../../docs/operations/firecracker.md), point `NIKA_RUN_CONFIG` at a run config with `nika.execution.firecracker` settings, and pass `--mode firecracker`. The collector restores lab images directly, without the benchmark runner's admission control, so run one restore at a time whose `vm_memory` fits in the free huge pages.

## Select a uniform sample

Comparisons are only fair when every variant has the same number of runs, collected by the same code on one kernel boot. `canonical` lists the variants that are short of 10 qualifying runs and, with `--prune`, keeps the first 10 qualifying runs per variant and mode and deletes the rest:

```shell
uv run python -m experiment.convergence.canonical runtime/convergence
uv run python -m experiment.convergence.canonical runtime/convergence --prune
```

A run qualifies when its `meta.json` records the kernel boot, it was dumped at the default delays, every node's dump succeeded and, for variants with ECMP routes, every probed node returned its probes. Collect more runs with `--start` until no variant is short, then prune.

## Decide data-plane equivalence

`netkat_equiv` encodes each run's FIBs as a NetKAT forwarding relation and clusters runs into equivalence classes. `--ecmp set` treats ECMP next hops as interchangeable; `--ecmp hash` models Linux hash-threshold selection, so a different next-hop order is a different data plane:

```shell
uv run python -m experiment.convergence.netkat_equiv runtime/convergence/dc_clos_m --ecmp hash
```

KATch runs in the `julesjacobs/katch` image that NIKA's NetKAT verifier pins. The encoding covers Linux and SR Linux FIBs. It excludes host management networks (Docker's default bridge and Containerlab's management subnets) and does not model NAT, policy routing beyond the main table, TTL or ARP. A node whose dump failed in any run is left out of every run's model.

`flows` compares the probed forwarding decision of each flow with the first run of the same kernel boot; a flow that resolves in one run and is unreachable in another counts as changed. Its position agreement checks the hash model: within one boot, a flow that takes an ECMP member keeps its position in the next-hop list.

```shell
uv run python -m experiment.convergence.flows runtime/convergence/dc_clos_m
```

To run every analysis on every variant and print one Markdown table:

```shell
uv run python -m experiment.convergence.report runtime/convergence --run
```

## Compare incident symptoms

`symptoms` injects one incident with fixed parameters in every run and records a pingmesh between the environment's end hosts before and after injection; `symptoms_report` counts the distinct sets of host pairs that lose at least 10% of their pings after injection and none before:

```shell
uv run python -m experiment.convergence.symptoms --mode native \
  --scenario dc_clos -s m --problem link_packet_corruption \
  --set host_name=leaf_router_0_0 --set intf_name=eth0 --set corruption_percentage=30 \
  --out runtime/convergence/symptoms_dc_clos_m
uv run python -m experiment.convergence.symptoms_report runtime/convergence/symptoms_dc_clos_m
```

Each source host pings its targets one after another. Concurrent ping streams overload small Firecracker VMs and add loss that the incident did not cause.

## Files

| File | Role |
|---|---|
| `collect.py`, `node_dump.sh` | Deploy or restore, dump node state through MCP, probe ECMP flows |
| `parse.py` | Read dumps |
| `canonical.py` | Select and prune the uniform sample |
| `netkat_equiv.py` | NetKAT encoding and KATch equivalence classes (`<variant>/netkat_{set,hash}.json`) |
| `flows.py` | Per-flow next-hop comparison (`<variant>/flows.json`) |
| `report.py` | Results table |
| `symptoms.py`, `symptoms_report.py` | Same-incident symptom comparison |
