# Audit lessons

What earlier audits learned: the 2026-10 trajectory audit (3,060 trials), the red-team leak audit, the healthy-hunter baseline audit, the release audit, and the runs of this skill. This file is the learned part of the skill, next to the issues in `kb/issues/`. Add a lesson when an audit goes wrong in a way the next one should avoid, in the same change that adds the issue.

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
uv run python -m experiment.audit.matrix --scenario <scenario> --fault <fault> --repeats 2 --force
```

The `symptom`, `persistence_symptom`, and `agent_view` stages show whether the symptom exists and whether an agent's default tools see it.

### Healthy-environment check from the agent's view

Run what an agent would run with default arguments: a PingMesh snapshot with its defaults, plus differential probes (endpoint pairs, DNS names from every resolver, BGP peer states, k8s Warning events). Run two rounds 15 s apart and count a check as failed only when it fails in both. Compare every "unreachable" against the session's `probe_intent` before calling it a fault. `experiment/audit/agent_view.py` implements this (stages `agent_view` and `baseline_agent_view`).

### Leak

Deploy the task twice, once healthy and once injected, and run the cited read-only command on the injected device and on one same-role peer in both. A leak shows only on the injected device of the injected environment. Follow [the leak sweep](#leak-sweep) for where to look and how to compare.

For a scripted comparison, snapshot agent-readable state (files under /etc, /tmp, /var, /opt, /root, processes, rules, routes, qdiscs, controller state) before and after injection and diff them after masking per-deployment values (timestamps, PIDs, counters on timestamped lines); the release audit's `leak_scan` stage does this.

### Judging a symptom claim

A claim that a failure has no symptom, or a label is not identifiable, must itself be sound (rules of the release audit, `experiment/audit/`):

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
