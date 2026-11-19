# Benchmark audit

Use the benchmark audit to check that a task starts healthy, exposes its declared fault, and keeps that fault observable. The audit runs real NIKA environments and records evidence for release review.

## Run the audit

`nika images prepare` prepares public images. IOS XR and RouterOS tasks also require their respective images; see [Prepare vendor images](../operations/installation.md#prepare-vendor-images).

Run from a clean, committed checkout with Docker and the environment images available. The audit scripts and NIKA package must come from that checkout:

```shell
uv run python -m scripts.benchmark_audit.matrix --scenario campus_lan --fault dhcp_missing_subnet
```

The matrix reads the Dev and Test catalogs of benchmark release 0.3.0 and checks them against the current implementation. It does not change published tasks or scores. Omit the scenario and fault filters to audit the full catalog.

The default is two workers. Host admission checks available memory and load, and Containerlab environments share a serial slot. Native router tasks wait for their full topology memory estimate when it exceeds the class floor. If capacity drops before deployment, the audit waits again. Other classes retain their declared concurrency limits. Protocol convergence and persistence checks can make a task take several minutes. Each audit closes the session it created when it finishes or raises an exception.

| Option | Effect |
| --- | --- |
| `--scenario NAME`, `--fault NAME` | Select tasks to audit. |
| `--repeats N` | Run each selected task on N fresh deployments. Every repeat must pass. |
| `--force` | Replace the selected tasks' stored measurements. |
| `--retry-failed` | Rerun selected tasks whose current result did not pass. |
| `--shard K/N` | Audit only the cases in shard K of N. Shards come from a stable hash of the case, so hosts on the same commit can each run one shard and their records combine. |
| `--cases FILE` | Audit only the case names listed in FILE, one per line. A case name is its result file name without the `__r0.json` suffix. Unknown names fail before any environment starts. |
| `--allow-dirty` | Collect diagnostic measurements while editing. These results do not count toward admission. |
| `--case-images` | Audit each fault task on its [Firecracker case image](../operations/firecracker.md#case-images-optional), the saved state that trials restore, instead of a fresh deployment. Healthy tasks restore their lab image. Needs a host set up for Firecracker. |

## Review the evidence

Read the JSON records in `runtime/environment-audit-results/`. `NIKA_AUDIT_RESULTS_DIR` selects another output directory. Each record contains the task parameters, stage results, observations, Git commit, working-tree state, timestamps, session ID, and container image identities.

Startup checks include telemetry readiness. Healthy controls use declared data paths and exclude management links and endpoint pairs that the environment isolates by design.

For a single-fault task, the audit checks:

1. The environment and target path are healthy before injection.
2. The symptom predicate stays negative on the healthy environment, the fault's Oracle playbook does not confirm the fault there, and the default agent tools show no stable anomaly.
3. The standard NIKA injection workflow verifies the fault, aligns saved configurations, and writes the injection record and ground truth.
4. A scan compares agent-readable files and network state before and after injection.
5. The declared symptom appears while an independent control path stays healthy, where the topology provides one.
6. The reference Oracle diagnoses the injected task through the session's MCP tools and records a solved result in `oracle.json`. Each failure declares the failures whose symptoms are closest to its own, and none of their playbooks may confirm on the task. Device and link targets are mapped to the same attachment, including the forwarding peer of an endpoint link. A playbook inspects the task's target when the two failures share target parameters and the playbook can be built for it; otherwise it inspects its own failure's default target in the environment. When the environment supports none of them, the audit records that limitation.
7. The fault artifact and symptom persist through two further observation windows.

Multi-fault tasks use the catalog's combination checks. The audit injects each failure alone, checks its artifact and symptom, then verifies the joint outage or masking relation and the effect of each repair. It runs every planned playbook on the healthy environment, executes the task's authored schedule before diagnosis, and requires the Oracle to confirm every active failure. A latent member can change protocol state without causing an outage alone; the joint outage or masking relation is checked separately. Look-alike controls exclude the task's active failures. Schedule offsets are part of the audit identity. Recovery and a final healthy environment check complete the audit.

Look-alike planning uses the session's declared infrastructure identities when validating a deployed topology. Telemetry collectors and flow probes remain infrastructure, outside the fault target pools. A planning error fails the neighbor-Oracle stage with its traceback. Only declared deployment incompatibility or the absence of eligible look-alikes can produce `no_confusable_on_scenario`.

Redundant-link placements require two distinct data links: either link can fail alone without disconnecting the topology, and losing both separates end hosts. Generation omits placements that fail this precondition and records the rejection in its report. Partition probes exclude telemetry links, which carry monitoring traffic in a separate management VRF.

Sham symptom checks run before injection. Probes read their targets and protocol timers from the healthy environment when injection has not yet saved recovery state.

Healthy tasks run the environment, agent-view, and Oracle checks without injection. The Oracle runs on the same deployed lab; its evidence and failed steps remain in the session artifacts. A missing or unsolved Oracle result prevents admission. Only `pass` admits a task. Missing, unsupported, skipped, and failed checks stay distinct. When no independent control path exists, the audit records that limitation and relies on the baseline, negative control, and repeated symptom checks.

Trials that use case images start from a saved copy of the injected task rather than a fresh injection, so a pass on a fresh deployment does not show that the saved copy still has the fault and its symptom. `--case-images` checks the copy itself: the fault is present, the declared symptom differs from the healthy environment, the Oracle confirms the task while the playbooks of its closest failures, chosen as above, do not, and the fault and symptom persist through two further windows. The healthy-environment checks for fault tasks stay in the fresh-deployment audit. Healthy tasks restore the lab image and run their initial environment, agent-view, Oracle, and final environment checks there. For multi-fault tasks, case-image mode checks every member’s artifact and symptom, the catalog’s joint outage or masking relation, and the Oracle diagnosis of all active failures. Timed tasks restore their healthy baseline and execute the authored timeline before these checks. Records go to `case-images/` under the results directory.

A stored result counts only for the current clean commit, with completed provenance and the required agent-view and Oracle stages. Old or incompatible records require another run. The matrix reports how many selected tasks have current passing evidence; historical pass counts do not establish current admission.

HTTP baselines can take up to three bounded readings while startup traffic settles; the record retains every reading. HTTP status and timing come from the same completed request. Independent controls exclude management networks and endpoint pairs the environment isolates by design. The selector removes root-cause attachments from the topology and chooses endpoints in the largest remaining connected component, excluding branches that depend on those attachments for routing or DHCP renewal.

### Injection traces

The leak scan checks files, processes, sockets, links, addresses, routes, firewall rules, queueing state, and supported device tables. It flags new fault-revealing names or content, backup files, and copies of pre-fault state. It also flags added device-side TC objects, XDP attachments, and native interface-inventory queries recorded during injection, even when they contain no fault or framework token. It masks volatile values in text before comparison and lists ordinary process changes for review. Each member of a combination uses the same declared native configuration keyword exemptions as a single-fault task; full fault names, harness markers, and backup files remain subject to detection.

Native configuration keys such as `router-id`, `datapath-id`, and `ttl-security` can be evidence of the root cause in running and saved configurations. Faults declare these keyword exemptions, exact native log phrases, and vendor log rotation pairs. The scan also recognizes SR Linux's standard gNMI trace shadow. Full fault names, harness markers, and configuration backups still fail the scan. Log phrase exemptions mask only the matching phrase; other leak tokens on the same line and fault-naming content in rotated logs still fail the scan.

File times are separate evidence. `file_time_asymmetries` records mtime/ctime changes relative to unchanged peers with the same declared role, platform capabilities, and service type. `file_time_coverage` and `file_time_unreadable_nodes` show where metadata was available. Time differences are review leads, not automatic failures: native protocol and management logs can change as legitimate evidence.

The scan does not establish that an agent exploited a trace. Use the [submission audit skill](../../.agents/skills/nika-submission-audit/SKILL.md) to review recorded agent behavior.
