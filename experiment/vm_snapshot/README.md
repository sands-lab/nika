# VM snapshot runs: measurements

This experiment checked whether benchmark trials can restore a converged,
verified lab from a stateful Incus VM snapshot instead of deploying it, and what
that gains and costs. It led to the optional `nika.snapshot` mode of
`nika benchmark run`:

* code: `src/nika/snapshot/`;
* usage and contracts: [VM snapshot runs](../../docs/operations/vm-snapshots.md).

## Measure it yourself

Run the same tasks twice with `nika benchmark run`, with `nika.snapshot.enabled`
off and on, each into its own `--result_dir`. Then compare the two:

```shell
uv run python experiment/vm_snapshot/compare.py results/cold results/snap
```

[`compare.py`](compare.py) reads only standard trial artifacts (`run.json`,
`nika.jsonl`):

* It splits phases with `phase_windows` from
  [experiment/profile](../profile/README.md).
* It first shifts lab-host events (`"origin": "remote"`) by
  `remote_clock_offset_sec` onto the host clock.
* Snapshot trials add the restore timings that `run.json` records under
  `snapshot`.

## Results

Measured 2026-10-13/14 on a shared 16 vCPU / 62 GiB host that is itself a KVM
guest, so the VMs ran nested. VMs had 16 vCPU and 8 GiB; `isp_pdh` had 20 GiB.
Most cells are single runs, and other labs often ran on the host at the same
time, so treat differences under about 20% as noise.

### Environment cost per trial

Columns (seconds):

* **Cold**: `env_start` + `env_stop` on the host.
* **Restore → ready**: until the NIKA daemon in the copy answers.
* **→ verified**: the trial identity is adopted and the startup verification has
  passed again.
* **Saved / trial**: Cold minus "→ verified" minus 2 s to discard the copy.
* **Break-even**: how many trials repay the golden build.

| Environment | Golden build | Cold | Restore → ready | → verified | Saved / trial | Break-even | RAM state raw / gz (MB) |
|---|---:|---:|---:|---:|---:|---:|---:|
| k8s_lab | 658 | 301 | 28 | 57 | 242 | 3 | 4944 / 1200 |
| llmd_lab | 455 | 190 | 26 | 54 | 134 | 4 | 4739 / 1199 |
| isp_pdh (Containerlab, SR Linux) | 704 | 234 | 60 | 124 | 108 | 7 | 14930 / 2566 |
| min3clos (Containerlab, SR Linux) | 277 | 102 | 25 | 50 | 50 | 6 | 4804 / 928 |
| campus_lan -s s | 183 | 72 | 11 | 24 | 46 | 5 | 1160 / 261 |
| isp_abilene ospf/ebgp | 268 | 66 | 10 | 26 | 38 | 8 | – / 251 |
| sdn_l3_clos -s s | 201 | 66 | 14 | 34 | 30 | 7 | 1911 / 435 |
| p4_dc_gateway -s s | 165 | 49 | 9 | 27 | 20 | 9 | 1177 / 257 |
| enterprise_branch -s s | 136 | 22 | 9 | 18 | 2 | 69 | 1053 / 233 |
| dc_clos -s s | 110 | 17 | 9 | 16 | −1 | never | 796 / 203 |

* **Sources:**
  * `nika benchmark run` trials, one cold and one snapshot result dir per
    environment.
  * The golden build is the latest serial build. Builds that ran alongside other
    labs took up to 1.6× longer.
  * RAM state sizes come from the lab-only runs.
* **Ground truth and scores:** identical between cold and snapshot trials for
  every environment, with the mock agent, which always submits the ground truth.
* **Release 0.2.0 tasks on these environments (61):**

  | Runs per task | Net saving after all golden builds | Mostly from |
  |---|---:|---|
  | 1 | about 30 min | k8s_lab, llmd_lab |
  | 10 | about 13 h | k8s_lab, llmd_lab |

### Medium and large topologies

Measured on 2026-10-15 with the mock agent, through `nika benchmark run`:

* one cold trial;
* two snapshot trials, where the first builds the golden VM and the second
  reuses it.

Column definitions:

* **Env:** setup + inject + cleanup (seconds).
* **Snapshot env:** the reuse trial, plus 2 s to discard the copy.
* **Break-even:** golden build divided by the saving.

Every trial ended `success`.

| Variant | Nodes | Golden build | Cold env | Snapshot env | Saved / trial | Break-even | RAM state (MB, gz) |
|---|---:|---:|---:|---:|---:|---:|---:|
| dc_clos m | 28 | 179 | 52 | 27 | 25 | 8 | 332 |
| dc_clos l | 104 | 428 | 277 | 43 | 234 | 2 | 730 |
| campus_lan m | 42 | 171 | 81 | 36 | 45 | 4 | 330 |
| campus_lan l | 182 | 330 | 243 | 98 | 146 | 3 | 776 |
| enterprise_branch m | 46 | 230 | 42 | 20 | 22 | 11 | 279 |
| enterprise_branch l | 134 | 198 | 80 | 24 | 56 | 4 | 368 |
| p4_dc_gateway m | 26 | 198 | 91 | 44 | 47 | 5 | 323 |
| p4_dc_gateway l | 50 | 328 | 188 | 64 | 124 | 3 | 438 |
| sdn_l3_clos m | 46 | 266 | 140 | 40 | 100 | 3 | 472 |
| sdn_l3_clos l | 90 | 463 | 294 | 39 | 255 | 2 | 571 |
| isp_nobel-eu m (isis/ibgp_rr) | 56 | 169 | 79 | 39 | 41 | 5 | 409 |
| isp_cost266 l (isis/ibgp_rr) | 74 | 195 | 100 | 49 | 51 | 4 | 480 |

* **Gains grow with size.** Cold setup grows with the node count (dc_clos: 17 s
  for s, 52 s for m, 277 s for l), while a restore stays at 10–25 s plus the
  re-verification. From m upward, most variants repay their golden build in
  2–5 trials; at l the saving per trial reaches 2–4 minutes.
* **Injection time is not saved.** `campus_lan l` spends about 65 s injecting
  in both modes, which caps its gain.

### Packaging the base VM (once)

[`scripts/snapshot-base.sh`](../../scripts/snapshot-base.sh) with
`--prepare-all` took 1682 s:

| Step | Time (s) |
|---|---:|
| Cloud image boot | 94 |
| Docker and modules | 97 |
| `install.sh` | 58 |
| `nika images prepare` | 1397 |
| Guest preparation | 36 |
| Snapshot | 0.2 |

Image preparation (83%) is also part of a first host install.

### End to end with a real agent

Agent: `byo.langgraph`. "Env" is setup + inject + close, in seconds.

| Task | Model | Arm | Env | Total |
|---|---|---|---:|---:|
| k8s_lab `k8s_coredns_isolated` | Qwen3.6-35B | host cold | 353 | 655–669 |
| | | snapshot | 87 | 216–220 |
| dc_clos `link_down` | Qwen3.6-35B | host cold | 26 | 107–127 |
| | | snapshot | 16 | 112–171 |
| isp_abilene `bgp_max_prefix_exceeded` | Qwen3.5-27B | host cold | 77 | 203–227 |
| | | snapshot | 33–37 | 168–190 |

* **Scores:** the same in every arm.
* **Agent time:** varies run to run with the model; it masks the environment
  saving except on k8s_lab.
* **GLM-4.7-Flash** ran dc_clos, k8s_lab and isp_pdh through snapshot mode:
  * every MCP server listed its tools and answered direct calls;
  * snapshots aged about 1 h (lab clock 3730 s behind) showed no difference: the
    agent only sees lab-clock timestamps, which stay consistent with each other.

## Findings behind the design

* **Restore time follows guest RAM, not disk.** Incus stores the state
  gzip-compressed; decompression is single-threaded, at about 4 s per GB of raw
  stream.
  * `init_on_free=1` and dropping the page cache before the snapshot shrank
    dc_clos's state from 2.96 GB to 0.2 GB.
  * On the same stream, zstd decompressed in 0.9 s where gzip took 4.6 s; using
    it would need an Incus change.
* **Give the VM the host's CPU count.** With 4 vCPU, pingmesh on isp_abilene
  took 87 s instead of 41 s. vCPUs do not enlarge the snapshot.
* **Do not step the guest clock.** Routers in one VM share the paused monotonic
  clock, so BGP sessions never flap. Wall-clock state (DHCP leases, heartbeats,
  k3s) stays consistent, and NIKA records the offset rather than hiding it.
* **First operations after a restore run slower,** for example the first
  verification and the first injection. Keeping the page cache did not help,
  which points at nested-virtualization memory mapping on this host.
* **Copies of one snapshot share the guest MAC and IP.** Each copy therefore gets
  its own Incus project and a host-less bridge, and the host reaches it over
  vsock only.
