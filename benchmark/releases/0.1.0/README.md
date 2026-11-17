# Release 0.1.0

Release 0.1.0 is the benchmark of the NIKA paper (arXiv 2512.16381 v1). Its code is commit `249d5fdb` (2026-08-15) on the `release/0.1.0` branch, the last commit whose sources match every file `RELEASE.yaml` pins, with `rule-based-v2` scoring on `(resource, fault_type)` root causes. The branch adds only what lets the current NIKA run this code as a benchmark engine; environments, failures, tools, and scoring are those of `249d5fdb`.

## Splits

| Split | Tasks | Failures | Sizes |
| --- | --- | --- | --- |
| `test.yaml` | 150 | 54 | `s`, `m`, `l` where the scenario scales (48 failures × 3 sizes, 6 failures once) |
| `dev.yaml` | 56 | 56 | `s` |

### Test split: the paper's 150 incidents

`test.yaml` holds the 150 incidents the paper evaluates and the Zenodo record publishes: `benchmark/additional_splits/outputs/benchmark_selected_150.yaml` from pull request #15. The release first shipped a 56-task test split. The test split changed so that 0.1.0 scores are comparable with the paper's results.

The rows were not edited. `nika benchmark migrate` of this commit added their ground truth (`root_causes`); all 150 resolved, 111 at node level and 39 at interface level. `rebuild_release_manifest` wrote the counts, split hashes, source pins, and digest in `RELEASE.yaml`.

### Dev split

`dev.yaml` is unchanged. Dev and Test are therefore not fully held out from each other, which `RELEASE.yaml` declares under `dev_test_isolation`, and the release preflight checks exactly:

- 26 tasks appear in both splits (`shared_tasks`). Dev's other 28 `s` tasks share their scenario, failure, and size with a test task but inject into a different device.
- The test split has no `dhcp_spoofed_subnet` or `p4_aggressive_detection_thresholds` task; only Dev covers them (`dev_only_failures`).

Tune agents on Dev knowing that results on its 26 shared tasks are also test results.

## Running the release

The current NIKA runs this release through its benchmark engine (engine API version 1), declared in the `engine` block of `RELEASE.yaml`; the frontend's manifest names the engine commit. The engine image installs the dependencies in `uv.lock`, resolved as of 2026-08-16.
