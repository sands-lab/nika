# Release 0.1.0

Release 0.1.0 is the benchmark of the NIKA paper (arXiv 2512.16381 v1). Its code is commit `249d5fdb` (2026-08-15) on the `release/0.1.0` branch, the last commit whose sources match every file `RELEASE.yaml` pins, with `rule-based-v2` scoring on `(resource, fault_type)` root causes. The branch adds what lets the current NIKA run this code as a benchmark engine. Environments, failures, tools, and scoring are those of `249d5fdb`, except for the correction under [Corrections](#corrections).

## Splits

| Split | Tasks | Failures | Sizes |
| --- | --- | --- | --- |
| `test.yaml` | 150 | 54 | `s`, `m`, `l` where the scenario scales (48 failures × 3 sizes, 6 failures once) |
| `dev.yaml` | 55 | 55 | mostly `l` (43 tasks); 3 `m`, 2 `s`, and 7 in scenarios that do not scale |

### Test split: the paper's 150 incidents

`test.yaml` holds the 150 incidents the paper evaluates and the Zenodo record publishes: `benchmark/additional_splits/outputs/benchmark_selected_150.yaml` from pull request #15. The release first shipped a 56-task test split. The test split changed so that 0.1.0 scores are comparable with the paper's results.

The rows were not edited. `nika benchmark migrate` of this commit added their ground truth (`root_causes`); all 150 resolved, 111 at node level and 39 at interface level. `rebuild_release_manifest` wrote the counts, split hashes, source pins, and digest in `RELEASE.yaml`.

### Dev split: the original held-out split

`dev.yaml` is the release's original test split as of `249d5fdb` (`sha256:98441c92`), which 0.1.0 used for scoring from 13 August until the October rebuild. `benchmark/generate_heldout.py` (#28, seed 43) built it from the first dev split: for each failure it took another instance from `benchmark_full.yaml`, on a different scenario where one supports the failure, otherwise at a different size, otherwise with a different injection target. Most of its tasks therefore run a failure in a different, larger network than the paper's size-`s` incident.

Seven of its 56 tasks share their scenario, failure, and size with a test task (two are identical), so they were replaced:

| Failure | Original held-out task | Dev task now |
| --- | --- | --- |
| `dhcp_missing_subnet` | `ospf_enterprise_dhcp` `l` | `ospf_enterprise_dhcp` `m`, client `pc_2_2_1_1` |
| `dhcp_service_down` | `ospf_enterprise_dhcp` `l` | `ospf_enterprise_dhcp` `m`, client `pc_1_2_1_2` |
| `dhcp_spoofed_dns` | `ospf_enterprise_dhcp` `l` | `ospf_enterprise_dhcp` `m`, client `pc_1_1_2_1` |
| `dhcp_spoofed_gateway` | `ospf_enterprise_dhcp` `l` | `ospf_enterprise_dhcp` `s`, client `pc_2_1_1_1` |
| `host_vpn_membership_missing` | `rip_small_internet_vpn` `l` | `rip_small_internet_vpn` `s`, `web_server_1_1` |
| `mpls_label_limit_exceeded` | `p4_mpls`, `switch_1` | `p4_mpls`, `switch_7` |
| `load_balancer_overload` | `ospf_enterprise_dhcp` `l` | none |

Each replacement is the `benchmark_full.yaml` instance of the failure at size `s` or `m` (`p4_mpls` does not scale) that is not a test task. `load_balancer_overload` has no such instance: only `ospf_enterprise_dhcp` supports it, with one target, and all three sizes are test tasks. `nika benchmark migrate` added the replacements' ground truth; the 49 retained tasks are byte-identical to the original split.

No task is in both splits, which the release preflight checks. The splits cover different failures: only Dev has `dhcp_spoofed_subnet` and `p4_aggressive_detection_thresholds`, which the paper's incident set lacks, and only Test has `load_balancer_overload`.

## Corrections

`sender_resource_contention`, `receiver_resource_contention`, and `load_balancer_overload` run only stress-ng's CPU stressors. At `249d5fdb` they also ran memory, disk, and I/O stressors sized from the host's memory, which the 256 MiB limit of a lab container killed within about 90 seconds, taking a web server with them. NIKA 0.2.0 made the same change (`7c682f44`). This changes what the agent encounters on 9 test tasks and 2 dev tasks, so it is a revision under the versioning policy.

## Running the release

The current NIKA runs this release through its benchmark engine (engine API version 1), declared in the `engine` block of `RELEASE.yaml`; the frontend's manifest names the engine commit. The engine image installs the dependencies in `uv.lock`, resolved as of 2026-08-16.
