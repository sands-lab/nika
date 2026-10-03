# Scenario startup performance

Resource and time cost of the labs and cases in benchmark release 0.2.0 (test and
dev splits), measured without an agent. Use it to size benchmark `batch_size` and
host capacity. Metric definitions and script options:
[experiment/profile/README.md](../../experiment/profile/README.md).

The page has two measurements:

- **Single-lab baseline:** every lab variant the release deploys, one lab at a time,
  3 runs each. Normal NIKA startup (deploy, workload preparation, light runtime
  verification), a 10 s hold, and cleanup.
- **Concurrency sweep:** every release case, with its failure injected and verified
  as the benchmark runner does before the agent starts, at `batch_size` 8, 16 and 24.

## Run

| Item | Value |
| --- | --- |
| Dates | 2026-10-01 20:41 UTC to 2026-10-03 11:39 UTC |
| Host | 32 vCPU (AMD EPYC 9754, 1 thread per core), 125 GiB RAM, no swap, Linux 5.15.0-191, cgroup v2, Docker 28.0.1, Containerlab 0.79.0 |
| Cases | Release 0.2.0, test and dev splits: 169 cases, 49 distinct lab variants (scenario, backend, size, `topo`, `igp`, `bgp_mode`, `rpki`, `device_profile`) |
| Run config | `config/nika.yaml`: `runtime_validation.depth: light`, `static_validation.enabled: false`, `serialize_heavy: true` |
| Sampling | 1 s interval, lab container cgroups and the NIKA worker process tree |
| Other load | No other NIKA lab ran. An unrelated libvirt VM ran from 2026-10-02 10:21 to 11:43 UTC and overlapped 37 of the 84 runs in the ISP part of the baseline (see [Sampling quality](#sampling-quality)) |
| Leftovers | After every step: no containers and no sessions from this run |

`--release` loads one split, so the case files were built from the release YAML
files. The baseline used one row per lab variant; the sweep used every row:

```bash
uv run python - <<'EOF'
import yaml
rows = []
for split in ("test", "dev"):
    rows += yaml.safe_load(open(f"benchmark/releases/0.2.0/{split}.yaml"))["cases"]
yaml.safe_dump({"seed": 42, "cases": rows}, open("cases-release-0.2.0-all.yaml", "w"), sort_keys=False)
EOF
```

Single-lab baseline, `batch_size` 1, 3 runs per lab variant:

```bash
uv run python experiment/profile/profile_scenarios.py --run-config config/nika.yaml \
  --config cases-release-0.2.0-all.yaml --batch-size 1 --repeats 3 --hold-seconds 10 \
  --output results/profile-20261001/scenarios-c1
```

The baseline was collected in parts with the same settings. 39 runs (15 non-ISP
variants) came from a run that selected labs from the scenario registry before
profiling switched to benchmark cases; only runs whose lab variant matches a release
variant exactly were kept. The 28 ISP variants, the 6 `campus_lan` and
`enterprise_branch` variants, and the third run of 6 variants were profiled from
case files with one row per variant.

Concurrency sweep, run once per `batch_size`, one after another:

```bash
for c in 8 16 24; do
  uv run python experiment/profile/profile_scenarios.py --run-config config/nika.yaml \
    --config cases-release-0.2.0-all.yaml --inject --batch-size $c --repeats 1 \
    --hold-seconds 5 --output results/profile-20261001/cases-c$c
done
```

## Single-lab baseline

Every one of the 49 lab variants passed 3 of 3 runs. Values are medians over the 3
runs; parentheses show min-max. Times are seconds.

- **Variant:** `igp/bgp_mode` set by the benchmark case, and the device image when it
  is not the FRR default. `-` means scenario defaults.
- **Containers:** peak lab container count.
- **Peak mem:** peak sampled container memory, including page cache. **WS:** peak
  working set (memory minus inactive file cache).
- **CPU:** aggregate lab container CPU, where 100% is one core. Peak is the highest
  1 s sample; mean is total CPU time over the run's wall time.

Rows for the same scenario are adjacent, so Kathara and Containerlab variants of an
ISP topology sit next to each other.

| Scenario | Backend | Size | Variant | OK | Containers | Deploy | Convergence | Cleanup | Total | Peak mem GiB | WS GiB | Peak CPU % | Mean CPU % |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| campus_lan | kathara | s | - | 3/3 | 20 | 10 (10-10) | 49 (49-49) | 3 (3-4) | 73 (73-74) | 0.44 (0.39-0.45) | 0.44 | 222 | 28 |
| campus_lan | kathara | m | - | 3/3 | 42 | 15 (15-16) | 49 (48-49) | 7 (7-9) | 83 (82-84) | 0.37 (0.37-0.37) | 0.37 | 67 | 35 |
| campus_lan | kathara | l | - | 3/3 | 182 | 50 (49-52) | 26 (25-26) | 32 (31-32) | 120 (118-122) | 0.79 (0.79-0.79) | 0.78 | 103 | 69 |
| dc_clos | kathara | s | - | 3/3 | 8 | 8 (8-8) | 1 (1-1) | 2 (2-2) | 21 (21-21) | 0.23 (0.21-0.25) | 0.20 | 209 | 28 |
| dc_clos | kathara | m | - | 3/3 | 28 | 19 (19-19) | 1 (1-1) | 8 (8-8) | 39 (38-39) | 0.81 (0.80-0.84) | 0.62 | 267 | 48 |
| dc_clos | kathara | l | - | 3/3 | 104 | 103 (103-104) | 1 (1-2) | 62 (61-64) | 181 (181-184) | 3.20 (3.20-3.21) | 2.31 | 364 | 48 |
| enterprise_branch | kathara | s | - | 3/3 | 16 | 10 (10-10) | 9 (9-9) | 3 (2-3) | 32 (32-32) | 0.24 (0.24-0.25) | 0.18 | 224 | 31 |
| enterprise_branch | kathara | m | - | 3/3 | 46 | 16 (15-16) | 10 (10-10) | 5 (5-6) | 42 (41-42) | 0.33 (0.33-0.33) | 0.27 | 244 | 44 |
| enterprise_branch | kathara | l | - | 3/3 | 134 | 29 (28-29) | 3 (2-3) | 15 (14-15) | 58 (57-58) | 0.55 (0.55-0.56) | 0.48 | 422 | 64 |
| isp_abilene | kathara | s | isis/none | 3/3 | 24 | 12 (11-12) | 36 (36-37) | 5 (5-5) | 63 (62-63) | 0.45 (0.41-0.47) | 0.45 | 394 | 77 |
| isp_abilene | kathara | s | ospf/ebgp | 3/3 | 24 | 12 (11-12) | 53 (53-53) | 4 (4-4) | 79 (79-79) | 0.60 (0.58-0.60) | 0.60 | 401 | 79 |
| isp_abilene_ebgp_rpki | kathara | s | - | 3/3 | 25 | 12 (12-12) | 48 (47-53) | 4 (4-5) | 74 (73-80) | 0.59 (0.59-0.61) | 0.59 | 398 | 81 |
| isp_abilene_ebgp_rtbh | kathara | s | - | 3/3 | 24 | 12 (12-12) | 48 (47-54) | 4 (4-4) | 74 (73-80) | 0.54 (0.53-0.58) | 0.54 | 383 | 79 |
| isp_cost266 | kathara | l | isis/ibgp_rr | 3/3 | 74 | 28 (28-28) | 38 (38-39) | 17 (17-17) | 94 (94-94) | 1.46 (1.42-1.48) | 1.46 | 588 | 149 |
| isp_dfn-bwin | kathara | s | isis/none | 3/3 | 20 | 17 (17-17) | 35 (35-35) | 10 (10-11) | 73 (72-74) | 0.35 (0.32-0.39) | 0.35 | 474 | 57 |
| isp_dfn-bwin | containerlab | s | ospf/ebgp, SR Linux | 3/3 | 20 | 181 (176-185) | 23 (23-24) | 4 (4-4) | 219 (214-223) | 14.25 (14.24-14.26) | 14.17 | 1327 | 395 |
| isp_dfn-bwin_ebgp_rtbh | kathara | s | - | 3/3 | 20 | 18 (17-18) | 54 (49-54) | 9 (9-10) | 92 (86-92) | 0.36 (0.35-0.41) | 0.36 | 492 | 59 |
| isp_dfn-gwin | containerlab | s | isis/none, SR Linux | 3/3 | 22 | 196 (190-197) | 9 (8-9) | 4 (4-4) | 219 (213-220) | 15.23 (15.22-15.23) | 15.13 | 1322 | 403 |
| isp_di-yuan | containerlab | s | isis/ibgp_rr, SR Linux | 3/3 | 22 | 193 (190-194) | 30 (14-30) | 4 (4-4) | 235 (221-239) | 15.66 (15.64-15.66) | 15.58 | 1138 | 405 |
| isp_di-yuan | kathara | s | isis/none | 3/3 | 22 | 17 (17-17) | 39 (33-39) | 10 (9-10) | 75 (70-76) | 0.46 (0.44-0.46) | 0.46 | 363 | 60 |
| isp_di-yuan | containerlab | s | isis/none, SR Linux | 3/3 | 22 | 192 (190-194) | 8 (8-8) | 4 (4-5) | 216 (214-217) | 15.19 (15.19-15.22) | 15.09 | 1343 | 404 |
| isp_di-yuan | containerlab | s | ospf/ebgp, SR Linux | 3/3 | 22 | 201 (199-201) | 29 (28-29) | 4 (4-4) | 244 (242-245) | 15.60 (15.60-15.62) | 15.51 | 1041 | 401 |
| isp_geant | kathara | m | ospf/ebgp | 3/3 | 44 | 19 (19-20) | 46 (46-53) | 9 (9-10) | 86 (85-91) | 0.87 (0.83-0.89) | 0.86 | 555 | 113 |
| isp_geant_ebgp_rpki | kathara | m | - | 3/3 | 45 | 19 (19-19) | 45 (45-52) | 10 (9-10) | 85 (85-91) | 0.87 (0.86-0.93) | 0.87 | 548 | 113 |
| isp_germany50 | kathara | l | isis/none | 3/3 | 100 | 38 (38-39) | 30 (30-31) | 26 (25-26) | 106 (105-107) | 1.47 (1.42-1.47) | 1.46 | 646 | 143 |
| isp_germany50 | kathara | l | ospf/none | 3/3 | 100 | 38 (38-38) | 46 (45-46) | 25 (25-26) | 120 (119-121) | 1.28 (1.25-1.29) | 1.27 | 661 | 141 |
| isp_india35 | kathara | l | isis/none | 3/3 | 70 | 33 (32-33) | 38 (38-39) | 21 (21-21) | 103 (102-104) | 1.10 (1.05-1.15) | 1.10 | 523 | 122 |
| isp_janos-us | kathara | m | isis/ibgp_rr | 3/3 | 52 | 31 (31-31) | 33 (33-34) | 19 (19-19) | 95 (94-95) | 1.04 (1.04-1.05) | 1.04 | 627 | 112 |
| isp_janos-us-ca | kathara | l | isis/none | 3/3 | 78 | 43 (43-43) | 31 (31-39) | 30 (28-30) | 116 (115-122) | 1.25 (1.23-1.31) | 1.25 | 556 | 118 |
| isp_nobel-eu | kathara | m | isis/ibgp_rr | 3/3 | 56 | 21 (21-22) | 34 (34-34) | 12 (12-15) | 79 (78-82) | 1.10 (1.05-1.10) | 1.10 | 699 | 130 |
| isp_nobel-germany | kathara | m | isis/none | 3/3 | 34 | 16 (15-16) | 38 (37-38) | 8 (8-8) | 72 (72-73) | 0.59 (0.58-0.59) | 0.59 | 381 | 91 |
| isp_pdh | containerlab | s | isis/ibgp_rr, SR Linux | 3/3 | 22 | 197 (196-203) | 14 (14-31) | 4 (4-4) | 226 (224-248) | 15.64 (15.63-15.64) | 15.56 | 1071 | 423 |
| isp_pdh | containerlab | s | isis/none, SR Linux | 3/3 | 22 | 198 (191-199) | 8 (8-8) | 4 (4-5) | 221 (214-222) | 15.19 (15.14-15.20) | 15.09 | 1061 | 403 |
| isp_pdh | containerlab | s | ospf/ebgp, SR Linux | 3/3 | 22 | 202 (200-206) | 43 (28-44) | 4 (4-4) | 258 (249-261) | 15.60 (15.60-15.61) | 15.51 | 1084 | 399 |
| isp_pioro40 | kathara | l | isis/ibgp_rr | 3/3 | 80 | 36 (35-36) | 39 (39-39) | 24 (23-25) | 113 (113-114) | 1.61 (1.54-1.65) | 1.60 | 609 | 138 |
| isp_ta1 | kathara | m | ospf/none | 3/3 | 48 | 24 (24-25) | 48 (46-48) | 14 (14-14) | 96 (95-98) | 0.73 (0.72-0.75) | 0.72 | 529 | 102 |
| isp_ta2 | kathara | l | isis/none | 3/3 | 130 | 47 (47-47) | 25 (25-26) | 33 (32-33) | 116 (116-117) | 1.96 (1.87-2.02) | 1.96 | 591 | 158 |
| k8s_lab | kathara | - | - | 3/3 | 20 | 230 (230-231) | 76 (76-95) | 14 (14-14) | 331 (330-348) | 14.20 (14.15-14.23) | 1.96 | 817 | 126 |
| llmd_lab | kathara | - | - | 3/3 | 8 | 145 (144-147) | 37 (37-37) | 5 (5-5) | 197 (196-200) | 8.24 (8.23-8.37) | 1.57 | 745 | 136 |
| min3clos | containerlab | - | - | 3/3 | 5 | 65 (62-67) | 4 (3-4) | 2 (2-2) | 81 (78-83) | 4.23 (4.23-4.29) | 4.21 | 1049 | 253 |
| p4_dc_fabric | kathara | s | - | 3/3 | 15 | 33 (32-33) | 12 (12-13) | 3 (3-3) | 58 (58-58) | 0.15 (0.13-0.15) | 0.15 | 55 | 18 |
| p4_dc_fabric | kathara | m | - | 3/3 | 45 | 46 (46-47) | 13 (13-13) | 8 (8-8) | 78 (77-78) | 0.29 (0.29-0.29) | 0.29 | 84 | 29 |
| p4_dc_fabric | kathara | l | - | 3/3 | 89 | 84 (81-84) | 13 (13-13) | 23 (23-23) | 131 (129-132) | 0.56 (0.56-0.56) | 0.56 | 139 | 34 |
| p4_dc_gateway | kathara | s | - | 3/3 | 14 | 33 (33-33) | 5 (5-5) | 3 (3-3) | 51 (51-52) | 0.24 (0.23-0.24) | 0.23 | 100 | 25 |
| p4_dc_gateway | kathara | m | - | 3/3 | 26 | 44 (43-45) | 7 (7-8) | 6 (6-7) | 68 (68-69) | 0.44 (0.44-0.44) | 0.44 | 143 | 34 |
| p4_dc_gateway | kathara | l | - | 3/3 | 50 | 80 (80-82) | 12 (12-12) | 19 (19-20) | 123 (121-125) | 0.87 (0.87-0.87) | 0.86 | 190 | 43 |
| sdn_l3_clos | kathara | s | - | 3/3 | 16 | 56 (55-66) | 4 (3-4) | 3 (3-4) | 74 (73-83) | 1.05 (1.03-1.08) | 0.96 | 341 | 120 |
| sdn_l3_clos | kathara | m | - | 3/3 | 46 | 102 (94-104) | 4 (4-4) | 8 (8-9) | 125 (116-126) | 1.17 (1.14-1.20) | 1.08 | 316 | 118 |
| sdn_l3_clos | kathara | l | - | 3/3 | 90 | 207 (206-208) | 4 (4-4) | 25 (23-25) | 247 (247-248) | 1.59 (1.49-1.64) | 1.50 | 656 | 120 |

### Baseline observations

- **Runs are repeatable without contention.** Across the 3 runs of a variant, total
  time varied by a median of 2.2% (max 13%) of the median, and peak memory by 3.3%
  (max 20%).
- **Containerlab SR Linux labs cost about 30x the memory of the Kathara FRR labs.**
  The one variant the release runs on both backends with the same routing setup,
  `isp_di-yuan` size s with `isis/none`, takes 75 s, 0.46 GiB and 60% mean CPU on
  Kathara with FRR, and 216 s, 15.19 GiB and 404% mean CPU on Containerlab with
  SR Linux. The difference includes the device image, not only the backend. All 8
  Containerlab SR Linux variants peak at 14.2-15.7 GiB and take 181-202 s to deploy;
  the 20 Kathara ISP variants peak at 0.35-1.96 GiB and deploy in 12-47 s.
- **k3s labs are memory- and disk-heavy, mostly through page cache.** `k8s_lab`
  peaks at 14.2 GiB with a 1.96 GiB working set and writes 13.1 GiB per run;
  `llmd_lab` peaks at 8.2 GiB with a 1.57 GiB working set and writes 7.0 GiB.
- **25 of 49 variants stay below 1 GiB.** Apart from the Containerlab SR Linux labs,
  k3s labs and `min3clos` (4.2 GiB), the largest is `dc_clos` size l at 3.2 GiB.
- **Convergence is short.** The longest median convergence is 76 s (`k8s_lab`); the
  Kathara ISP labs converge in 25-54 s.

## Concurrency and throughput

The sweep ran all 169 cases at `batch_size` 8, 16 and 24 with `serialize_heavy`.
Light cases share the batch limit; the 78 heavy cases (45 topo_size `l`, 21
k8s/llmd, 12 Containerlab) run alone after the light cases finish. Throughput is ok
cases per hour of sweep wall time. Light and heavy phase times are measured from the
first to the last resource sample of the cases in each phase. Phase times below are
medians over ok cases, in seconds.

| `batch_size` | Wall h | OK / cases | Failed | Throughput ok/h | Light phase h | Light ok/h | Heavy phase h |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | 4.53 | 169/169 | 0 | 37.3 | 0.43 | 213 | 4.10 |
| 16 | 4.51 | 169/169 | 0 | 37.5 | 0.44 | 206 | 4.07 |
| 24 | 4.57 | 169/169 | 0 | 37.0 | 0.50 | 181 | 4.06 |

Light cases (91 ok at every level):

| `batch_size` | Deploy | Convergence | Inject | Cleanup | Total | Docker daemon CPU s per case | Peak host load1 | Min MemAvailable GiB |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | 65 | 8.8 | 2.7 | 20 | 118 | 146 | 193 | 110.2 |
| 16 | 162 | 8.9 | 3.0 | 46 | 248 | 375 | 188 | 106.6 |
| 24 | 266 | 8.2 | 3.2 | 60 | 385 | 830 | 372 | 101.3 |

Heavy cases (78 ok at every level) are unaffected by `batch_size` because they run
alone: median deploy 103-104 s, convergence 19-25 s, inject 3.4 s, cleanup 15-16 s,
total 176-177 s. Their minimum MemAvailable was 71.2-81.0 GiB.

Docker daemon CPU is the CPU time of `dockerd` and `containerd` during each case's
window. The daemons are shared, so this is a measure of contention, not a per-case
cost. Host load1 is a 1-minute average, so the first heavy cases still show load from
the light phase.

### Concurrency observations

- **Throughput does not change with `batch_size` between 8 and 24.** The heavy phase
  takes 4.06-4.10 h of the 4.5 h sweep at every level, because the 78 heavy cases run
  one at a time.
- **Light cases saturate the host by `batch_size` 8.** Going from 8 to 16 to 24 does
  not shorten the light phase (0.43, 0.44, 0.50 h). It makes each light case slower:
  median deploy rises from 65 to 162 to 266 s and cleanup from 20 to 46 to 60 s, while
  Docker daemon CPU per case rises from 146 to 375 to 830 s. Convergence and inject
  times stay flat, so the contention is in container and network creation and
  removal.
- **Memory is not the limit.** MemAvailable never dropped below 71 GiB of 125 GiB.
- **Load does not cause failures.** All 169 cases passed at every level.

### Sizing guidance for this host class (32 vCPU, 125 GiB)

- Use `batch_size` 8. Values above 8 add per-case latency and Docker daemon load
  without improving throughput. Values below 8 were not measured.
- Keep `serialize_heavy`. Expect a full 0.2.0 release (169 cases, both splits) to
  take about 4.5 h of lab time before agent time, about 4.1 h of it in the heavy
  cases. Adding CPU cores or raising `batch_size` does not shorten that part; this
  run did not measure running heavy cases in parallel.
- Plan memory per lab from the baseline table: about 15.7 GiB for a Containerlab
  SR Linux lab and 14.2 GiB for `k8s_lab`, below 2 GiB for every Kathara ISP lab.
- Keep at least 13 GiB of free disk per `k8s_lab` run and 7 GiB per `llmd_lab` run,
  on top of the images.

## Sampling quality

| Output | Runs | Runs with sampling errors | Sampling errors (total, max per run) | Max gap between samples, median over runs | Max gap, worst run | Runs with zero containers |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline, first part | 39 | 0 | 0, 0 | 1.1 s | 1.4 s | 0 |
| Baseline, ISP variants | 84 | 4 | 5, 2 | 1.7 s | 4.5 s | 0 |
| Baseline, `campus_lan` and `enterprise_branch` | 18 | 0 | 0, 0 | 1.1 s | 1.9 s | 0 |
| Baseline, third runs | 6 | 0 | 0, 0 | 1.2 s | 1.4 s | 0 |
| Sweep, `batch_size` 8 | 169 | 24 | 38, 5 | 1.1 s | 7.1 s | 0 |
| Sweep, `batch_size` 16 | 169 | 50 | 138, 8 | 1.2 s | 4.5 s | 0 |
| Sweep, `batch_size` 24 | 169 | 54 | 238, 10 | 1.2 s | 7.3 s | 0 |

- The median gap between samples is 1.00-1.07 s in each of the 654 runs. Gaps above 2 s in the
  baseline come from large Kathara ISP labs (44-130 containers), where one sample
  takes longer than the interval.
- Sampling errors grow with concurrency because Docker API reads time out under
  load. They reduce sample coverage and do not change case status.
- The libvirt VM overlapped 37 baseline ISP runs. For the 28 ISP variants with runs
  inside and outside that window, the median ratio of total time (inside / outside)
  is 0.99 (range 0.94-1.09).
