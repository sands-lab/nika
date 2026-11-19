# Audit lessons

What earlier audits learned: the 2026-10 trajectory audit (3,060 trials), the red-team leak audit, the healthy-hunter baseline audit, the release audit, and the runs of this skill. This file is the learned part of the skill, alongside the reviewed issues in `src/nika/audit/kb_data/issues/`. Add a lesson when an audit goes wrong in a way the next one should avoid, in the same change that adds the issue.

The issue classes, what each looks like, and the evidence rules are in [the guidelines](guidelines.md). The lessons below add what earlier audits found about where each class hides and how audits went wrong.

## Judging lures and hidden faults

* The configs and source win over the description when they disagree, and the disagreement is itself worth reporting (healthy-hunter H6, H7).
* Check the vantage point: a ping from the wrong source interface, an RIB entry that is not in the FIB, ECMP sampling one path (repeated HTTP connections from one client are not independent ECMP trials).
* Isolation by design (VRFs, client isolation, north-south only intent) shows up as "unreachable" pairs. `probe_intent` in the session metadata lists it.
* A fault-like reading that holds on every model and every healthy trial of a scenario is a benchmark property, not an agent mistake.

## Judging leaks

* Same-role peer check: a log line, file, or mtime is a leak only when it appears on the injected device and not on its identical peers.
* Deployment checks are not injection: read-only probes right after deploy leave CLI history and logs on one device.
* The fault mechanism itself is not a leak: a frag-needed filter rule *is* the fault. Names an injector chose for it can be.
* Asymmetry is its own leak class: an extra rule, table, qdisc, or process on the faulted node only, or a mechanism its same-role peers do not use. Compare peers for state, not only for logs, files, and mtimes.
* Values that do not come from the network (a placeholder MAC, sequentially allocated flood prefixes, conspicuous rule priorities) point at the injection. Ask whether the value is derived from this network and looks like the baseline's.
* A copy of the pre-fault state (backup, save file, pristine startup copy, saved request body) leaks the change whenever it differs from the live state in the faulted part.
* Compare the prompts of healthy and faulted tasks of one environment. Wording that differs only by task variant tells the agent whether a fault exists, even when it reads like a harmless instruction (k8s_lab fault prompts said "Report faulty devices"; audit runs saw it and dismissed it).
* Tie leaks to payoff: the `lift` field shows whether trials that saw a marker scored higher than comparable trials that did not.

## Diagnosing a miss

When a known issue slipped through, decide whether it was a coverage failure (no packet or output the reviewer read contained it: widen leads or sampling) or a recognition failure (it was read and not reported: fix the guideline or prompt). Over-broad exemptions in prompts ("deploy-time noise", "normal design") are the usual cause of recognition failures.

## Pitfalls seen before

* Reviewers dismissed real violations as "expectation wrong" (healthy-hunter B1). Never refute a finding without a check that *holds*.
* Unconstrained exploration burns the budget (healthy-hunter B2). Start from the hits and the leads; sample the rest.
* Judge node inspections against the declared tool policy. Authorized node tools expose processes, files, and configuration; distinguish that node state from the harness workspace. Identify the prohibited boundary and the observed access before claiming a violation. In `campus_lan__dns_port_blocked__h6a276d3ed5cb__t01`, step 39 targets `web_server_0`; the audit incorrectly called it the DNS server and treated the search itself as a violation. In `p4_dc_gateway__int_insuffici__h65e77e85f180__t03`, step 19 runs `ps aux | grep nginx` on the named topology node `service_1_1`; reading its process table does not cross the stated boundary. Source: `nika-bench/nika-trajectories`, commit `04fcd6ea825295d0655085b67e6eeefefdff7685`.
* Most unchecked citations were paraphrased or invented (red-team postfix: 153 of 173). Run `cite-check` before anything else reads the findings.
* Timestamps, PIDs, and sizes differ between runs; compare masked text.
* One noisy signature buries real hits. If a signature fires on most trials of every leaderboard submission, narrow it before it trains reviewers to skip it.
* Benchmark-identity leaks (a path or header that says "nika") and fault-revealing leaks (a name that says which fault) are different. Only the second kind must be absent from healthy trials; scope those issues to their fault types.
* Claude Code traces record each tool input twice (`message.content` and `wire_tool_inputs`) and each result twice (`content` and `tool_use_result`). If the copies disagree, the trace was edited (pilot B found planted mutations this way).
* Check submitted metrics for internal consistency (F1 against its precision and recall), not only against a re-grade.

## Leak sweep

How to look for traces an injection left behind, on a live environment or in the tool outputs of a trajectory. Distilled from the red-team trace audit, whose detector reached 98% recall (103/105 planted leaks, 0/10 false alarms after healthy-baseline replay) once these rules were in its prompt.

### Where leaks survive

Injectors avoid their own leftovers, so what survives is mostly side channels they do not control:

1. **Daemon and service logs** recording a restart or a write: zebra `client N disconnected ... routes removed`, journal `Stopping ... server`, bmv2 `/sw.log` `Entry N added/removed from table`, `Removed member N from group`, `Modified entry`, ONOS `No longer listening`, OVS `flow_mods`.
2. **Backup and save files** next to a config: `*.sav`, `*~`, `*.bak`, `*.orig`, `*.leases~`.
3. **Config mtime/ctime skew**: the injected device's config is newer than the same file on its same-role peers.
4. **Helpers in `/tmp`, `/var/tmp`, `/root`**: load logs (`http-load-*.log`, `iperf3_s_*.log`), scripts, sleep loops in `ps`.
5. **Injector-chosen names**: tc filters, nft tables, flows, prefix-lists, processes, files whose name states the fault (`switch_bitflip`, `TE-MORE-SPECIFICS`, `ab-loop-N`). `LEAK-FAULT-NAME-IN-OUTPUT` checks trajectories for this automatically.
6. **Pristine copies**: `/hostlab/<device>/` holds the startup config; a diff against the live file shows exactly what changed.
7. **Shell and vtysh history** (`/root/.history_frr`, `.bash_history`).

### Sweep every device first

Before going deep anywhere, write the full device list: the brief's role lists **and** every name in its `Topology:` links. Role lists can omit devices (campus_lan named `backend_web_0` only in the links; 4 of 5 red-team trials never swept it). Then run one sweep per device:

```bash
echo ==HIST==; cat /root/.*history /root/.history_frr /home/*/.*history 2>/dev/null | tail -n 40
echo ==PS==; ps -eo pid,lstart,args 2>/dev/null | grep -vE '\[|ps -eo' | tail -n 40
echo ==NEW==; timeout 6 find / -xdev -type f -newer /etc/hostname \( -path '*/log/*' -o -path '/tmp/*' -o -path '/var/tmp/*' -o -path '/root/*' -o -path '/etc/*' -o -path '/var/lib/*' -o -name '*.sav' -o -name '*~' -o -name '*.bak' -o -name '*.orig' -o -name '*.leases*' \) -not -path '/proc/*' -not -path '/sys/*' 2>/dev/null | head -n 50
echo ==TMP==; ls -la --time-style=full-iso /tmp /var/tmp /root 2>/dev/null | head -n 40
echo ==LOGSIG==; grep -rhiE 'disconnect|says hello|routes removed|Stopping |Starting |restart|Removed member|Modified entry|Entry [0-9]+ (added|removed)|No longer listening|flow_mods' /sw.log /var/log 2>/dev/null | tail -n 30
echo ==HOSTLAB==; ls -d /hostlab/*/ 2>/dev/null
echo ==MTIME==; stat -c '%Y %n' /etc/hostname /etc/*.conf /etc/*/*.conf 2>/dev/null | head -n 30
echo ==HLDIFF==; for d in /hostlab/*/; do (cd "$d" && find etc -type f 2>/dev/null | while read -r f; do cmp -s "$f" "/$f" || echo "DIFF /$f"; done); done 2>/dev/null | head -n 20
```

The first blind sweep missed 5 of 8 bmv2 tasks because `==LOGSIG==` lacked `Modified entry|Removed member`; extend it when a new log shape appears.

### Judge what the sweep shows

* **Report what you see; do not dismiss it.** A device-unique backup file, helper, fault-named file, newer config, or `/hostlab` diff is a trace even when you cannot explain it. The red team's first prompt saw backup files 10 times and reported them 0 times; helper files 14 and 2.
* **Peer check.** Boot writes `Starting`/`restart` lines on every device. A log line, file, or mtime is a trace only when it is on the injected device and not on its identical peers.
* **Deploy window.** Within about two minutes of `/etc/hostname`'s mtime the harness probes one device with read-only `show` and ping commands; CLI history, `sr_cli` logs, and `.pyc` mtimes from those probes are not traces. The exemption covers those probe commands only: a daemon log line present on one device and absent on its peers stays a (low-confidence) trace. Deployment also leaves fixed decoys (for example artifacts that exist only on leaf_1).
* **Mechanism vs leak.** The fault itself (a frag-needed filter rule, a running flood) is not a leak; a name or leftover the injector chose is.

### Verify against a healthy baseline

Replay every cited command on a fresh healthy deployment of the same environment. Anything that also appears there is not a leak (`in_baseline`). Compare masked text:

* timestamps (ISO, `2026/10/10`, syslog `Oct 10 12:00:01`, optional weekday) become `<time>`, and slash and dash date separators are the same;
* PID suffixes (`isisd.68`) become `.<n>`;
* on lines that carry a timestamp, standalone numbers become `<n>`; elsewhere numbers stay, so ASNs, MTUs, and prefixes must still match.

Masking misses counters (`16 flow_mods` vs `3 flow_mods`), inodes, and sizes on untimed lines, so confirm every surviving trace by hand. Discard excerpts that only repeat the agent's own earlier commands (`self_inflicted`, for example its vtysh lines read back from `.history_frr`).

### Trust the detector before trusting "no leak"

A clean result means something only if the same detector finds planted leaks. Plant each leak type on healthy environments, add clean controls, and report recall per type and the false-alarm rate. When recall is low, separate **observed** (the leak was in an output the auditor read) from **reported**: low observed means coverage (sweep more), low reported means recognition (fix the rules above).

A white-box auditor, told the answer and the injector source, that must still cite only commands a blind agent would run, gives the upper bound (qwen38: 72 of 75 fault types with a confirmed trace white-box vs 35 blind).

## Live confirmation

Use a live environment when a finding is about the benchmark itself and the trace cannot settle it. Run it on a host that can start NIKA environments, close every session you start, and never run `nika session wipe`.

### Hidden fault or lure in a healthy task

```bash
uv run nika env run <scenario> --size <size>                 # prints the session id
uv run nika exec --session_id <id> <host> "<read-only check from the finding>"
uv run nika session close --session_id <id>
```

Run the check twice, 15 s apart, from the vantage point the agents used and from one that avoids the suspected component. A reading that holds in both rounds on a fault-free environment is a lure or a hidden fault; mixed results are flaky, not a finding, and a check that errors (including an exec `[TIMEOUT]`) is no evidence either way. Compare against the scenario brief to tell which. Every expectation should hold for every member of a group, not only the first, and intended isolation is checked as "unreachable".

### Fault without a symptom, or an invisible signal

Rerun the release audit for the task:

```bash
uv run python -m scripts.benchmark_audit.matrix --scenario <scenario> --fault <fault> --repeats 2 --force
```

The `symptom`, `persistence_symptom`, and `agent_view` stages show whether the symptom exists and whether an agent's default tools see it.

### Healthy-environment check from the agent's view

Run what an agent would run with default arguments: a PingMesh snapshot with its defaults, plus differential probes (endpoint pairs, DNS names from every resolver, BGP peer states, k8s Warning events). Run two rounds 15 s apart and count a check as failed only when it fails in both. Compare every "unreachable" against the session's `probe_intent` before calling it a fault. `scripts/benchmark_audit/agent_view.py` implements this (stages `agent_view` and `baseline_agent_view`).

### Leak

Deploy the task twice, once healthy and once injected, and run the cited read-only command on the injected device and on one same-role peer in both. A leak shows only on the injected device of the injected environment. Follow [the leak sweep](#leak-sweep) for where to look and how to compare.

For a scripted comparison, snapshot agent-readable state (files under /etc, /tmp, /var, /opt, /root, processes, rules, routes, qdiscs, controller state) before and after injection and diff them after masking per-deployment values (timestamps, PIDs, counters on timestamped lines); the release audit's `leak_scan` stage does this.

### Empty tool replies and scheduled recovery

An empty tool reply is not a successful observation until the request itself is known to have succeeded.
The ONOS REST provider used `curl ... || echo '{}'`; a real connection rejection reproduced an empty
body, while the repaired MCP call reported the transport failure. Verify a successful inventory request,
an invalid REST path, a blocked connection, and restoration. Keep the API's empty object distinct from
an inventory containing an empty list. `TOOL-ONOS-EMPTY-REST-REPLY` is a review lead scoped to the producing
tool and device-inventory path, not an automatic defect verdict. Evidence: `results/prerelease-cleanup/onos-final/`
and `tests/nika/service/kathara/test_kathara_sdn_mcp_live.py`.

For fault-presence findings, reconcile each active occurrence with the actual schedule journal before
attributing absence. Planned recovery, premature artifact removal, a failed read, and scheduler failure
have different meanings. A recovered occurrence does not exempt a different persistent fault from its
presence requirement. Evidence: `tests/benchmark/test_schedule_presence_e2e.py` and local repair `dfc50018`.

### Judging a symptom claim

A claim that a failure has no symptom, or a label is not identifiable, must itself be sound (rules of the release audit, `scripts/benchmark_audit/`):

* **Not tautological:** the symptom is measured on traffic, not by reading back the injected artifact.
* **Not confounded:** a control path that avoids the root cause still works. Choose it so it does not depend on the fault: never target a prefix the fault withdraws or hijacks, and start from a node whose path avoids the faulted component.
* **Statistically sound:** repeated reads, a healthy baseline of the same metric on the same path, and a sham read on the healthy environment that does not fire.
* **Semantically faithful:** the fault does what its name says (a loop loops; a corruption selector actually selects flows).
* **Timer-aware:** read protocol faults after the OSPF dead or BGP hold timer, and read again later to see the fault persist.

### Host and endpoint

Run each task on two fresh environments, and keep the full traceback of every failed stage: the llmd audit failure was only located once the traceback was kept. Host contention produces false failures that look like benchmark defects:

* another checkout rebuilding an image tag NIKA uses makes Kathara fail with an `IndexError` on `tags[0]`;
* a too-small neighbour table (`gc_thresh`) drops pings in large environments;
* high CPU load raises PingMesh latency and times out probes.

Rerun on a quiet host before reporting such a failure.

* One heavy environment (Containerlab, k8s/llmd, size `l`) at a time; about 6 light environments at once is the safe ceiling on a 16-32 core host. Wait for 12-16 GB of free memory before a deploy.
* `nika session wipe` closes every session on the host, including other people's; never use it.
* Large or concurrent environments need the host neighbour table raised (`gc_thresh1/2/3` 8192/16384/32768), or pings fail with "No buffer space available".
* `pkill -f` can kill its own shell; anchor patterns (`[w]eb_server`).
* Kathara VDE links carry about 3.5 kpps / 40 Mb/s: load you generate for a check can itself cause latency or loss.
* Model endpoints: check `GET /v1/models` first (served ids change); an HTTP 500 with no tool call is an endpoint failure, not an agent result.

Record what you ran and saw in the finding's `evidence` with `found_by: live_audit`.

### Verify the injected component before accepting a symptom

A probe can confirm the expected traffic failure while the injector changes a different component. For `p4_tcam_entry_corruption`, the old implementation computed a mask but dropped traffic in a VDE proxy. Read the named device's table before and after injection, compare it with controller intent, test a same-prefix control, and verify recovery. Keep physical-device claims separate from a software-switch model. See `FNR-TCAM-ROOT-CAUSE-NOT-INJECTED` and the retained evidence paths in its record. A source review is not a trajectory signature; check each run's pinned engine before applying the finding.

### Client deadlines and probe ownership

Prometheus can fail target-file discovery when a shared host exhausts inotify instances even though endpoint probers and the target generator are healthy. Compare generator activity with actual scrape pools and retain the collector error. Reuse an existing local HTTP endpoint for standard HTTP discovery when possible; keep target batches, labels, rotation, budgets, and readiness assertions intact. See `HARNESS-PINGMESH-FILE-WATCHER-LIMIT`.

A successful CLI exit does not establish successful MCP execution. Inspect tool results for `is_error` and timeout text. Connection waits, tool deadlines, and automatic backgrounding are separate controls. Verify client settings with the installed CLI; an isolated local model protocol does not establish model diagnosis performance. See `src/agent/cli/claude/config.py`.

A throughput probe must own its listener, preserve existing files and services, and clean up after a failed client. An occupied port is unavailable. Resolve the actual destination endpoint from the topology. Verify resource ownership with `nika exec` and `tests/nika/net_env/test_throughput_probe_live.py`.

Process readiness does not establish traffic readiness. Wait for received data on every declared bulk receiver before measuring a healthy baseline. Retain the same recovery threshold and inspect sender window-limited time. See `tests/support/failure_e2e_hooks.py` and `src/nika/net_env/enterprise_branch/workload.py`.

### Gateway lifecycle evidence

A reachable port does not prove that the intended MCP service bound it. Require the owned server's startup state and cleanup coverage for setup failures. Bound SSE draining, verify thread exit, and retain the manager when shutdown fails. Check a live sibling connection, an unrelated occupied listener, and owned cleanup. See `tests/nika/service/mcp_gateway/test_gateway_lifecycle.py` and `tests/nika/service/test_mcp_access_e2e.py`.

### TCP recovery and declared MCP capabilities

Snapshot restores must preserve the task's declared telemetry and diagnosis tools. Compare the restored gateway inventory with a matched native deployment; a successful restore does not establish tool parity. Include telemetry in the image identity and deployment request so disabled and enabled variants cannot share an image. See `HARNESS-FIRECRACKER-MISSING-TELEMETRY`.

Restored sysctls do not establish recovery of an existing TCP transfer. Compare socket identity, receive buffer, and delivered bytes. Reconnect only fault-created sockets that retain the cap, verify their disappearance, and preserve healthy connections on repeated recovery. Unavailable optional sysctls are null; required settings need valid numeric readback. See `tests/nika/problems/test_tcp_window_recovery_live.py`.

A completed Claude process can return a report with unavailable MCP servers. Require every declared phase server to connect and reject failed, pending, or missing capabilities. The SDK checks before querying; the CLI stops its child on failed init, but a request may already have been dispatched. Retain connected positive controls and distinguish ordinary tool denials from startup failure. See `src/agent/utils/mcp_client.py`, the Claude workers, and `tests/agent/test_mcp_startup.py`.

Reference playbooks must fit the same output limits as agent reads. Use observed neighbor addresses for BGP detail queries and retain every Pod that can match a Deployment selector when reducing Kubernetes output. A remaining matching Pod must still reject a zero-replica diagnosis. See `HARNESS-ORACLE-READ-OUTPUT-LIMIT`.

A healthy BGP session does not establish declared customer reachability. Compare expected-reachable probes and accepted routes with the rendered vendor policies; raw received-prefix counts can include rejected routes. Keep explicit isolation separate. See `HIDDEN-ROUTEROS-ISP-CUSTOMER-POLICY`.

Kubernetes bootstrap manifests can remain outside `/hostlab`, in controller directories such as `/shared` and `/k8s`. Compare a retained manifest with the live object and a healthy control before calling it a pre-fault copy; healthy replica counts can already differ from bootstrap values. See `LEAK-K8S-BOOT-MANIFEST-COPY`.

On restored healthy Kubernetes labs, compare Warning history with current Pod readiness and repeated application reads. An old event can remain after the workload recovers, and wall-clock resynchronization can age monitoring timestamps. Preserve current incident evidence when deciding how to clean up pre-episode events. See `LURE-K8S-BOOTSTRAP-WARNINGS`.

Native k3s and Prometheus can exhaust the same host's per-user inotify instance ceiling through different watchers. HTTP discovery removes the Prometheus file watcher; it does not establish kubelet capacity. Require the owned worker's startup log, distinguish infrastructure capacity from an injected incident, and verify failed-start cleanup. An isolated kernel can retain the normal native workflow without changing a shared host. See `NATIVE-K8S-INOTIFY-CAPACITY`.

After a restore, keep current HTTP/Pod readiness, Warning history and historical telemetry windows separate. A faster startup can expose a different saved probe window. Compare the same image and retain negative outcomes before accepting a warm-up repair; event deletion after a fault restore can erase valid incident evidence.

A stopped network device can correctly make reads unavailable while its controller remains healthy. Separate that expected status from an unhandled tool traceback, and retain a liveness control. Generic utility names under operator paths do not establish benchmark branding. See `HARNESS-P4RT-UNAVAILABLE-TRACEBACK`.

Seal a Kubernetes baseline after startup traffic Pods are Ready. A paused UFFD restore must prefill memory before resuming; warming alone cannot remove saved bootstrap events or prove a clean historical probe window. Verify the native baseline and an injected restore separately, retaining incident warnings after recovery. A k8s_lab receipt does not close an unverified llmd_lab lead. See `K8S-HEALTHY-WARNING-HISTORY`.

### Vendor file namespaces

Router shell restrictions do not cover a vendor CLI's file namespace. RouterOS `/file print detail` returned the complete uploaded `config.auto.rsc`; a normal ASN injection changed the live export while that file kept the original value. Inspect vendor file inventories and compare retained startup imports with live configuration and a healthy peer. Remove the bootstrap copy after import while preserving the import receipt and native telemetry. IOS XR similarly exposed `disk0:/startup-config.cfg` through `more`, retaining the old ASN after a live change. Require the native missing-file response rather than assuming Unix error wording. See `LEAK-ROUTEROS-STARTUP-CONFIG`, `LEAK-IOSXR-STARTUP-CONFIG`, and their native bootstrap regression tests.

### Scheduled image controls

An image for a scheduled task can contain the healthy initial state. Start its authored timeline through the normal scheduler and honor the agent-start delay before judging fault presence or labels. Compare the complete observed root-cause inventory with the authored set, and retain incomplete setup attempts separately. A probe that crosses injection or recovery is not a healthy control or a persistence result.

Run prepare, start, agent readiness and completion for static restored tasks too. Preparation writes an initial healthy ground-truth document; comparing that document with an already injected image before the schedule starts creates an auditor label error. Preserve the live observations, identify the invalid artifact, and compare labels only after the restored schedule completes. See `src/nika/remote/schedule.py` and `src/nika/workflows/schedule.py`.

### Repeated diagnostic flows

A failed fixed-port transfer can be a local bind error rather than network loss. Check client TCP ownership, including FIN-WAIT and TIME-WAIT, and retain the actual curl exit code. Repeated probes need fresh available tuples and matching capture filters; a previous probe’s retransmissions cannot establish current-probe evidence. Verify matched healthy, injected, repeated and recovered controls. See `HARNESS-CORRUPTION-PROBE-PORT-REUSE` and `tests/nika/service/test_corruption_reference_live.py`.

### Baseline fixtures in case images

A healthy symptom baseline can start validation helpers that survive into the saved case. Compare processes, listeners and files on restored cases with a matched healthy lab; do not limit shortcut scans to the injector. Prefer declared workload sessions and services for validation, and keep temporary requests bounded. Verify the actual case-baseline API through the agent gateway before injection, after injection and after recovery. Inspect retained packet captures as well: a saved healthy ICMP capture identifies the task family and exposes pre-fault packets. Reuse owned capture lifecycles, check failure cleanup, and preserve operator files. See `LEAK-NAT-BASELINE-PROBE`, `LEAK-ICMP-BASELINE-CAPTURE`, and their native baseline regression tests.

### Evidence across output bounds

A summary computed after an earlier byte limit can still be incomplete. Check the device command, host API buffer and gateway display bounds separately. Derive complete counts before the first bound, retain useful ordinary rows, and test both limits with live state. Distinguish clipped diagnostic evidence that changes a reference verdict from an ordinary broad list that has a supported narrower query. See `HARNESS-NAT-OCCUPANCY-OUTPUT-BOUND`.

### Native statistics and verification provenance

A nonempty parser result does not establish correct measurements. Compare frame counts and byte totals against the same capture's packet records, including traffic above the native formatter's unit threshold. Preserve SI units and allow only the formatter's measured rounding. The first live conversation control accepted positive but incorrectly scaled bytes; the packet-sum comparison caught it. See `HARNESS-PCAP-IP-CONVERSATIONS-EMPTY` and `tests/nika/service/test_packet_capture.py`.

Pin native controls to an immutable source checkout and record the provider hashes before and after the run. A source edit during deployment invalidates a paired control even when the runtime assertions pass. Keep auditor command, parsing and missing-utility failures separate from benchmark findings; retain valid earlier observations and verify owned cleanup after the observer fails.

## Restored-case readiness

* Artifact presence does not establish that a restored workload already realizes the declared symptom. Compare fresh early and later observations on repeated copies before blaming the fault or a measurement threshold. A condition-based handoff must preserve the same predicate, bound every remote call by the remaining deadline, and close a failed rebound session. Keep timed cases on their authored schedule. Source: `HARNESS-CASE-SYMPTOM-READINESS`, host fix `0b600fda`, frozen image `705dd29d205e`.

* **Restored probe history.** Environment health and direct paths do not establish that a healthy restored task has a clean monitoring window. Keep the unfiltered history and declared policy intent. Require readable post-verification scrapes and live exporters before the final history read, so newly completed failed probes cannot hide behind saved successes. Rotating targets can hide an unavailable prober from current failed-scrape rows, and current observations do not cover every path. Use raw lab-clock timestamps, repeated matched restores and real unavailable/recovered controls; native success alone does not prove corrected snapshot handoff. See `LURE-RESTORED-PROBE-HISTORY`.

## Native look-alike target pools and vendor keywords

Before attributing an admission failure to a reference Oracle, check whether control construction selected an eligible target in the actual deployment. Hybrid profiles may put native routers only in transit roles while the control requires client-facing eBGP roles. An empty capability-filtered pool must produce an explicit unavailable-control result through the existing target-selection exception. Keep valid controls and unexpected planning errors visible. The IOS XR enterprise prefix-list case demonstrates this mechanism; its RouterOS counterpart retains a valid native target.

Derived fault-ID phrases can also be ordinary vendor syntax. A changed `prefix-list` command and native FRR commit-schema key do not disclose the fault ID. Use the failure-owned native-keyword declaration, retain longer fault phrases and harness markers, and keep file, backup and pre-fault-copy scanning. Preserve the original verdict and verify the complete native case after correcting the scanner.

## Require sustained received media for QoS degradation

An idle RTP receiver can estimate loss from its previous interval without observing new source packets. A large resume burst can also dominate pooled loss while the following intervals are healthy. For a model that declares continuous media with partial loss, require the declared window of distinct intervals to contain received media and meet its existing loss threshold individually; retain pooled loss and baseline-delta checks. Preserve general RTP logging and complete-outage models. Compare live mechanism counters and repeated post-handoff observations before attributing a restore cause.

Keep verification verdicts separate: passing the repaired fault checks does not make an entire native matrix pass when its healthy latency control fails. A fixed-validator control on an unchanged image is diagnostic evidence; retain the original admission and identify the validator source explicitly.

Bound reference context reads as well as diagnosis evidence. A successful later assertion can hide a failed truncated context check. For P4, compare only the target forwarding state and retain the original context flag, mismatch predicates and healthy/recovery negative controls. See `HARNESS-ORACLE-READ-OUTPUT-LIMIT`.

Before paced reference traffic, compute the minimum duration from destinations, flows, repeats and sleep. Select affected destinations through declared topology roles and batch commands without reducing samples or loss thresholds. Capture a restored lab's telemetry-window start through its allowed host shell; a frontend timestamp can exclude all guest reports. An empty delivery result alone does not establish a clock defect. See `HARNESS-ORACLE-PACED-PROBE-TIMEOUT` and `HARNESS-ORACLE-REMOTE-CLOCK`.

A paginated telemetry API can still time out if it transfers the retained raw log before filtering. Confirm the producing collector command and preserve retained-row limits, trace collapse before filtering, delivery grace, paging and error behavior when moving the query to the collector. See `HARNESS-INT-QUERY-RAW-TRANSFER`.

TCP can recover corrupted packets while the original request succeeds. For `device_forwarding_packet_corruption`, compare sender retransmissions and receiver checksum errors before and after fresh probe tuples on both declared disjoint paths, with filtered packet evidence and complete probe counts. Keep healthy/recovery and local-bind controls; an arbitrary request-timeout requirement can reject a realized fault. See `HARNESS-CORRUPTION-TCP-RECOVERY-REJECTED`.

An INT delivery summary can confuse missing data packets with missing reports. On a healthy matched deployment, compare complete sender counts with agent-visible endpoint UDP counters. Keep collector socket/drop statistics as administrative causal evidence, and retain healthy, injected and recovered controls before accepting the reference. Nonzero missing counts alone do not identify the mechanism. See `LURE-P4-INT-DELIVERY`.

A JSON decoder error can hide a device command timeout or a nonzero exit. Validate the observation status before decoding, preserve its device and error text, and distinguish command failure from device unavailability. Verify a successful native port read and a real timeout before accepting the repair. See `HARNESS-BMV2-PORTS-UNCHECKED-JSON`.

A symptom API can fail after measuring the network because its result schema reads a removed baseline field. Compare the saved multi-endpoint inventory with both sender inputs and returned evidence. Preserve target and independent-control measurements, and verify the native lifecycle before accepting a reporting repair. Keep admission-only errors separate from agent-visible tool failures. See `HARNESS-SILENT-LOSS-SERVICE-INVENTORY`.

P4 case baselines can retain the same fixture shortcut as NAT baselines: inspect saved UDP receivers and persistent HTTP clients, their command lines and returned log/status inventories against the matched healthy image. Finite measurement receivers must exit before baseline capture returns, including after a failed sender. Verify the real baseline, repeated fault, recovery and public process/file/listener surface. See `LEAK-P4-BASELINE-PROBES`.

A validation client can label an established flow broken while unread response bytes remain in its receive queue. Correlate the exact ports, socket state, response bytes and request errors; verify independent progress and complete native controls before accepting an IO repair. Keep administrator evidence separate from agent-tool behavior. See `HARNESS-LB-CONTROL-IO-TIMEOUT`.

A negative control must reach the intended measurement predicate. Missing bound failure, listener setup, report coverage or clock context can return false before any valid measurement. Assert the measured fields as well as the Boolean result, and preserve valid earlier components when correcting an auditor control. See `tests/nika/service/test_p4_loss_baseline_live.py`.

A numeric process verifier must distinguish an empty result from an observation timeout. Preserve raw runtime output and timing, compare the same command with a bounded sufficient budget, and check status before parsing. A live collector does not prove the process-count command succeeded. See `HARNESS-PROCESS-CHECK-TIMEOUT-AS-ZERO`.

A finished capture can still disclose a case family through a saved filename and pre-fault packets. Compare restored host inventories with matched healthy controls, and wait for bounded capture processes before deleting only their own temporary files. Share healthy, repeated fault, recovery and public artifact assertions across selected deployment profiles. See `LEAK-ICMP-BASELINE-CAPTURE`.

A positive ECN baseline does not prove that a later healthy burst exercises marking. Retain a measured healthy sham after changes to workload cadence, CPU allocation or receiver timing. Preserve target and sibling counts and label native candidate sources separately from original snapshot evidence. See `HARNESS-ECN-HEALTHY-SHAM`.

A passing link-flap reference does not establish that the admission echo train observed its required cycles. Compare actual sample count, reply deadline, carrier transitions and path recovery; retain the minimum run and cycle assertions. See `HARNESS-FLAP-SYMPTOM-FALSE-NEGATIVE`.

### DNS asset metadata

A single DNS server has no identical peer, so compare its zone file with its other BIND assets as well as matched healthy and recovered states. Preserving mtime alone leaves the edited zone's ctime as a marker. Refresh regular assets on matching DNS roles and capabilities while preserving contents, access/modification times and permissions; leave symlink targets and unrelated nodes alone. Verify the served answer, application symptom and exact recovery through the normal gateway. See `LEAK-CONFIG-MTIME-SKEW` and `tests/nika/problems/test_dns_record_error_e2e.py`.

### Exact-startup monitoring controls

Read current and recorded telemetry before a long healthy sweep, then repeat it. A broad audit can age out the failed startup window it should measure. Pair the early history with current data-plane checks, exporter health and lab-clock scrape timestamps; preserve negative readings and reject a stopped exporter. Keep an external repair open in the KB until an integrated ancestor contains it. See `LURE-RESTORED-PROBE-HISTORY`.

A normal gateway query of current or historical reachable-path failures can supply a startup lead even when a long healthy sweep finishes clean. Bind the detector to the reachable selector and the exact current or historical query; compare with current failures, isolation intent and repeated direct checks. One early positive count followed by an empty vector is a transient observation, and shared repair verification on other profiles does not establish selected-profile verification.

### Connection-return failures

A completed Docker request can still fail while the HTTP library returns its connection. Keep the original gateway error and distinguish it from a separately measured library race. Do not replay commands to hide disposal errors. A guard must identify the original cleanup frame and already closed pool/connection, preserve primary and unrelated errors, and pass parallel native reads and owned-cleanup controls. See `HARNESS-DOCKER-POOL-RETURN-RACE`.

### Media witness windows

An empty receiver window does not by itself establish QoS degradation. Pair the acceptance decision with received media, sender activity, rewrite-counter growth and workload contention. Later live traffic does not erase an earlier failed restored admission. Keep a stricter predicate, a native control blocked before injection and an unresolved restore cause as separate results. See `HARNESS-RTP-DEGRADATION-WITNESS`.

### Occupancy under concurrent demand

A port-pool reference can fail between workload bursts even when the configured restriction is present. Keep bounded diagnostic connections open while counting mapped ports, and require excess-connection failure. Retain the occupancy threshold and verify healthy, injected and recovered controls. Remove owned diagnostic processes and files after each read; a timed expiry also covers an interrupted client. Never place those helpers in saved case images.

A recovered pool can retain mappings from an earlier probe. Occupancy alone then reports the previous demand; the restored policy and successful fresh connections must reject the diagnosis. Run this reference before admission's symptom probes can supply its missing traffic. See `HARNESS-SNAT-QUIESCENT-OCCUPANCY` and `tests/nika/service/test_snat_reference_live.py`.

### Service memory and persistent evidence

For `web_dos_attack`, repeat the public source and victim connection reads after the initial symptom. Pair a missing listener with the serving process and readable cgroup OOM counters; keep privileged kernel diagnostics separate. Verify a service bound with the original reference thresholds, late observations, an independent control, and recovery through the injecting instance. Native validation leaves corrected snapshot admission for integration. See [the service-exit issue](../../../../src/nika/audit/kb_data/issues/HARNESS-WEB-DOS-CONNECTION-EVIDENCE.yaml) and [the native service regression](../../../../tests/nika/service/test_static_http_capacity_live.py).

### Native physical capture ports

A native device can report a running port while the capture provider expects another namespace. Check the public interface inventory and topology binding before classifying a rejected name as an invalid argument. Use the environment's declared mapping: converted and reserved RouterOS routers use different port offsets. Compare the native and Linux names with the same endpoint traffic, and retain a missing-port negative control. See [the RouterOS capture issue](../../../../src/nika/audit/kb_data/issues/HARNESS-ROUTEROS-CAPTURE-PORT-NAME.yaml) and [the capture interface checks](../../../../tests/nika/service/test_packet_capture.py).

### Nested headers and capture totals

An ICMP error can include a quoted IP header. TShark field output can therefore contain several source and destination addresses for one frame. Compare IPv4 conversation totals with the outer header addresses and complete frame lengths. Retain quoted header values in the raw observations. Check pagination and native unit rounding before attributing a disagreement to the provider. See [protocol field extraction](../../../../src/nika/service/packet_capture/protocol_fields.py) and [capture accounting checks](../../../../tests/nika/service/test_packet_capture.py).

### Reference retries and isolated reads

Record requested and completed observation attempts. A replay that stops after its first timeout does not exercise the configured retry policy. Judge reference failure with the complete procedure and retain isolated failed reads. Verify corrections through normal native admission while preserving budgets and evidence predicates. See [the CPU reference observation issue](../../../../src/nika/audit/kb_data/issues/HARNESS-CPU-STARVED-ORACLE-READ.yaml).

### Native prefix delivery and origin evidence

Advertisement and best-path selection do not prove that a native prefix origin answers the benchmark endpoint. Compare the healthy control, selected path, traceroute endpoint and bounded ICMP result separately. Read the origin AS from the public native CLI and decode the selected native path; a FRR-only parser cannot establish a RouterOS verdict. Retain inactive-path, wrong-origin and wrong-prefix negative controls. See [prefix delivery](../../../../src/nika/audit/kb_data/issues/HARNESS-ROUTEROS-PREFIX-LOCAL-DELIVERY.yaml) and [native origin comparison](../../../../src/nika/audit/kb_data/issues/FNR-ROUTEROS-BGP-ORIGIN-PATH.yaml).

A worker-count repair does not prove quota-counter readability. Under the same contention, inspect every required reference operation with its complete retry policy. Preserve thresholds and budgets; remove avoidable observer process startups when the target cgroup is throttled. Keep unreadable counters as failed reads rather than accepting partial output.

A successful capture stop does not verify the inspection copy path. Compare a valid native port with its Linux mapping on the same capability, then inspect a nonempty capture. Vendor init can create private mounts unseen by Docker archive; a privileged byte-copy diagnostic explains a public inspection error but must remain separate from agent-reproducible evidence. Preserve binary bytes and negative missing-file/port controls.

Before changing a failing symptom measurement, read the authored behavior contract and deployment exclusions. A different metric can pass native validation while changing the benchmark question. Preserve that comparison separately and prefer the existing eligibility gate when the deployment violates the documented prerequisite.

### Native warning history in healthy restores

RouterOS prior-boot kernel/service errors and FRR scheduler-starvation messages can remain readable after healthy restored path checks pass. Compare event times with the lab clock and task handoff, repeat the native log read, and verify current protocol and endpoint state before classifying a persistent fault. Keep recovered history, a vendor defect of unknown cause, and self-generated CLI errors separate. A clean endpoint sweep does not establish complete protocol coverage. See `NATIVE-HEALTHY-WARNING-HISTORY`.

### Native configuration logs and export destinations

RouterOS system-info records can retain full original and changed commands after the uploaded startup file is gone. Compare the same connection in the returned log, live export and healthy control, and inspect local and remote logs. Native log records can prefix commands with an internal item number; validate signatures against the exact returned records. Keep routing and error events observable when removing command disclosures. For flow export, check the configured destination and received collector records before accepting an enabled flag as readiness. A malformed target block left every supported RouterOS exporter empty while startup passed. See `LEAK-ROUTEROS-CONFIGURATION-LOG` and `HARNESS-ROUTEROS-FLOW-TARGET-SETUP`.
