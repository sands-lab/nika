# NIKA Generalization Splits

A set of train / validation / test splits built from the public NIKA reasoning traces, used to measure generalization along three independent axes: the *problem* (root cause), the *scenario* (topology family), and the *topology size*.

Each split is produced as a CSV and then converted to a fully-filled YAML (with per-case `inject:` details) using the 640-config benchmark as a pivot.

---

## What the splits are

Every experiment uses one `(train, validation)` pair and two test sets:

1. A generalization test set: held-out groups, taken from the 490-config pool, to measure transfer to unseen conditions.
2. `benchmark_selected_150` (the 150): a fixed test set common to all experiments, so different methodologies can be compared on the same data.

The splits come in four types:
- three that hold out **problem categories**,
- three that hold out **scenario families**,
- one that holds out **topology size**, and
- three **no-generalization** controls.

### Splits by problem category (`problem{1,2,3}`)

Holds out root-cause families, to test whether a method handles fault types not seen in training.

| Seed | train | validation | test |
|------|-------|------------|------|
| `problem1` | link_failures (138) | misconfigurations, network_under_attack (96+42) | end_host_failures, resource_contention, network_node_errors (124+59+31) |
| `problem2` | end_host_failures (124) | misconfigurations, resource_contention (96+59) | link_failures, network_under_attack, network_node_errors (138+42+31) |
| `problem3` | misconfigurations (96) | link_failures, resource_contention (138+59) | end_host_failures, network_under_attack, network_node_errors (124+42+31) |

Train is smaller than validation here, following the GEPA setup where train ≈150 sits below validation ≈300.

### Splits by scenario family (`scenario{1,2,3}`)

Holds out topology families (`dc*` = data-center CLOS, `ospf*` = 3-tier campus, `rip*` = ISP backbone, `sdn*` = SDN fabric, `p4*` = P4 testbed, `simple*` = simple BGP), to test transfer across network architectures.

| Seed | train | validation | test |
|------|-------|------------|------|
| `scenario1` | dc* (126) | sdn*, rip* (99+69) | ospf*, p4*, simple* (102+72+22) |
| `scenario2` | ospf* (102) | sdn*, p4* (99+72) | dc*, rip*, simple* (126+69+22) |
| `scenario3` | sdn* (99) | dc*, p4* (126+72) | ospf*, rip*, simple* (102+69+22) |

### Split by topology size (`topo_size`)

Holds out scale: train on small, validate on medium, test on large.

| train | validation | test |
|-------|------------|------|
| s (132) | m (132) | l (132) |

(`-`, the non-scalable configs, is discarded for this axis.)

### No-generalization control (`wo_generalization{1,2,3}`)

Three random partitions of the 490 into ~163 / 163 / 164, seeded for reproducibility. Here train/val/test are drawn from the same distribution.

### Files

Each split is written as a CSV (one row per configuration, columns `problem, scenario, topo_size`, plus `category` and `scenario_prefix`) and then as a matching YAML (the same rows, fully filled with their `inject:` details).

```
problem1_train.csv      problem1_validation.csv      problem1_test.csv
problem2_train.csv      problem2_validation.csv      problem2_test.csv
problem3_train.csv      problem3_validation.csv      problem3_test.csv
scenario1_train.csv     scenario1_validation.csv     scenario1_test.csv
scenario2_train.csv     scenario2_validation.csv     scenario2_test.csv
scenario3_train.csv     scenario3_validation.csv     scenario3_test.csv
topo_size_train.csv     topo_size_validation.csv     topo_size_test.csv
wo_generalization{1,2,3}_train.csv   …_validation.csv   …_test.csv
```

The CSVs land in `benchmark/additional_splits/outputs/csv/`; the converted YAMLs (same basenames, `.yaml`) land one level up in `benchmark/additional_splits/outputs/`.

<!--
> To also emit `category` and `scenario_prefix`, set `information_added_regarding_category = True` (or call `add_prefix_and_category`). These go to `benchmark/splits_with_additional_columns/` so the canonical 3-column files are not overwritten.
-->

---

## How the splits are built (methodology)

### The two universes: 150 vs 640

The [arXiv v1](https://arxiv.org/abs/2512.16381v1) draft does not use the full benchmark. The published traces cover 150 `(problem, scenario, topo_size)` configurations; the authors separately provide a 640-config benchmark. The 150 are a subset of the 640, which partitions into:

- 150 → `benchmark_selected_150`, used as the shared comparison test set.
- 490 (= 640 − 150) → the pool from which the generalization splits are drawn.

The original 150 are not modified when building the generalization splits; all splitting happens on the 490.

### The three axes

Each configuration has three attributes, each defining a generalization question:

- `category` — the root-cause family (6). Taken from the authors' grouping (Table 3 / README "Network issues"), encoded as the top-level subfolder in `NIKA Traces.zip`, rather than from string matching.
- `scenario_prefix` — the topology family (6), the first underscore token of the scenario (`dc_clos_bgp → dc*`). `dc*`/`sdn*`/`p4*` map directly; `rip*` ↔ ISP backbone, `ospf*` ↔ campus, and `simple*` (kept separate) are checked against Table 5.
- `topo_size` — `s` / `m` / `l`, with `-` for non-scalable experiments.

The groupings ("seeds") are chosen so each of train/validation/test has a usable size.

### From CSV to fully-filled YAML

The split CSVs only carry `(problem, scenario, topo_size)`; the YAML shape additionally needs the per-case `inject:` block (`host_name`, `intf_name`, ports, rates, etc.), which is not in the CSV. The conversion is therefore a two-step lookup, using the 640-config benchmark as a pivot:

1. Build the fully-filled `benchmark_selected_640.yaml` once. The 640 CSV is first converted to a YAML skeleton (correct per-problem `inject:` keys, values set to `<MISSING>`), then those `<MISSING>` values are filled by matching each case against the canonical `benchmark_full.yaml` on the `(scenario, topo_size, problem)` key.
2. Convert every other split CSV by looking each row up in `benchmark_selected_640.yaml` and copying the matching fully-filled case. CSV row order is preserved, and `topo_size: -` maps to YAML `null`.

The `(scenario, topo_size, problem)` triple is unique across the 640 (and across the 685-case `benchmark_full.yaml`), so each CSV row maps to exactly one case; the converter aborts if any row is unmatched or any reference key is duplicated.

---

## Statistics

### The trace dataset

- 904 experiments in `NIKA Traces.zip`.
- Each of the 150 configs is run by 3 models (`gpt-5`, `gpt-5-mini`, `gpt-oss:20b`), twice → 900, plus 4 configs run a third time → 904 (`150·2·3 + 4`). All three models cover the same set of 150.
- The 4 triple-runs are all `dc_clos_bgp`: `link_down/m/gpt-5-mini`, plus `link_flap/s`, `link_flap/m`, `link_fragmentation_disabled/s` for `gpt-oss:20b`.

### The 640-config benchmark — by category

Counts are the same whether derived from Table 3's 41 problems or the 55 problems in the trace data:

| category | count |
|----------|------:|
| link_failures | 156 |
| end_host_failures | 154 |
| misconfigurations | 137 |
| resource_contention | 77 |
| network_under_attack | 69 |
| network_node_errors | 47 |
| **total** | **640** |

Topology sizes: `s` 180 · `m` 180 · `l` 180 · `-` 100.

### The 490-config split pool — counts across axes

| category | n | scenario_prefix | n | topo_size | n |
|----------|--:|-----------------|--:|-----------|--:|
| link_failures | 138 | dc* | 126 | s | 132 |
| end_host_failures | 124 | ospf* | 102 | m | 132 |
| misconfigurations | 96 | sdn* | 99 | l | 132 |
| resource_contention | 59 | p4* | 72 | - | 94 |
| network_under_attack | 42 | rip* | 69 | | |
| network_node_errors | 31 | simple* | 22 | | |
| **total** | **490** | | **490** | | **490** |

### The 150 (`benchmark_selected_150`) — counts across axes

- category: misconfigurations 41 · end_host_failures 30 · network_under_attack 27 · link_failures 18 · resource_contention 18 · network_node_errors 16
- topo_size: `l` 48 · `m` 48 · `s` 48 · `-` 6
- scenario_prefix: ospf* 84 · dc* 42 · sdn* 15 · p4* 6 · rip* 3

The 150 cover all three axes, so the set is used as the shared test set without modification.

---

## Reproducing the splits

```bash
python splitting.py --git_path '/path/to/github/repo/nika'
```

This reads the inputs from the repo, checks they are consistent, writes every split as a CSV under `benchmark/additional_splits/outputs/csv/`, and converts each split (plus the 640 pivot) to a YAML under `benchmark/additional_splits/outputs/`.

### Inputs expected (under `--git_path`)

| File | What it is | Source |
|------|------------|--------|
| `benchmark/additional_splits/inputs/NIKA Traces.zip` | The published reasoning traces | [Zenodo 17971675](https://zenodo.org/records/17971675) |
| `benchmark/additional_splits/inputs/benchmark_selected_640.csv` | All 640 `(problem, scenario, topo_size)` configurations | [Legacy NIKA repo](https://github.com/sands-lab/nika/blob/002502666752630066319858de2c7273b2ce85a6/benchmark/benchmark_full.csv) |
| `benchmark/additional_splits/inputs/benchmark_selected_150.csv` | The 150 configurations used in the paper | extracted previously by us, rechecked in this code |
| `benchmark/additional_splits/inputs/benchmark_selected_32.csv` | A 32-config subset of the 150 for quick tests | extracted by us |
| `benchmark/additional_splits/inputs/benchmark_full.yaml` | Frozen copy of the canonical full YAML (685 cases, the 640 in original order, with `inject:` details) used as the pivot to fill `<MISSING>` values | copied from [`benchmark/benchmark_full.yaml`](https://github.com/sands-lab/nika/blob/1ecf23b05107e315b10a0639828b7fee7a3c8bf2/benchmark/benchmark_full.yaml) |

### Built-in checks

The script verifies:

- Traces ↔ paper: the 150 reconstructed from `NIKA Traces.zip` match `nika_selected.csv` (`identical up to ordering: True`).
- Model coverage: all three LLM models used in Nika cover the same 150 (`all three cover the same configs: True`).
- Subset: every one of the 150 is in the 640 full benchmark csv (`missing: set()`), and the complement is 490.
- The 32-config quick-test set is a subset of the 150 (`all 32 present in selected: True`).
- YAML conversion: the `(scenario, topo_size, problem)` key is unique across the pivot, and every split-CSV row resolves to exactly one fully-filled case.

### Outputs also copied

For convenience the script also copies the canonical YAMLs into the outputs folder, suffixed with their current sizes (update the numbers if the benchmark grows):

- `benchmark_full_685.yaml` — the full benchmark (685 cases).
- `benchmark_selected_56.yaml` — the 56-case selection (fully inside the 685; 55 of its 56 are in the 640, the exception being `(ospf_enterprise_dhcp, s, dhcp_spoofed_subnet)`).

### Helper

`add_prefix_and_category(df)` takes a DataFrame with `problem, scenario, topo_size` and returns it with `category` and `scenario_prefix` added, for re-annotating an existing split without rerunning the pipeline.
