You are red-teaming a network troubleshooting benchmark. A fault may have been injected into the live network described below. Another agent will later be scored on finding the fault's type and location by real diagnosis. Your job is different: **do not diagnose**. Look only for *traces the injection process left behind* that would let an agent guess the answer without diagnosing.

## What counts as a trace

A trace is anything an agent can observe that gives away where the fault was injected or what kind of fault it is, without reasoning from network symptoms. Look for these trace types (use the `trace_type` values in the report):

- `command_history`: shell, vtysh, or editor history (`~/.bash_history`, `/root/.bash_history`, `~/.vtysh_history`, `~/.viminfo`, `~/.lesshst`).
- `orphan_rule`: tc qdiscs, iptables/nft rules or tables, routes, or ARP/neighbor entries whose name, comment, handle, or table name looks injected, or that appear on only one device with no design reason.
- `file_mtime`: config or system files changed much later than their neighbors or than boot/deploy time (`ls -la --time-style=full-iso`, `stat`, `find / -newer ... -mmin`).
- `leftover_process`: processes, scripts, or command lines left by the injector (`ps -eo pid,lstart,args`); unusual start times; helper tools such as stress, hping3, ab, scapy, or sleep loops.
- `naming`: device, container, interface, file, rule, chain, or Kubernetes object names that reveal the fault type or location.
- `config_artifact`: backup or temp files (`*.bak`, `*.orig`, `*~`, `/tmp/*`, `/root/*`), comments in configs, injector scripts.
- `log_artifact`: syslog, daemon logs, journal, or dmesg entries that record the injection command itself rather than its effect.
- `workspace_leak`: anything in your own local workspace, environment variables, or tool descriptions that reveals the answer.
- `other`: anything else that leaks the answer.

A trace is **not** a symptom you must reason about, such as a failed ping, a down BGP session, or high latency. Report those only when the evidence names the fault directly. For each trace, set `part_of_fault_mechanism`: `true` if what you saw *is* the injected faulty state itself (for example, the one netem qdisc that causes the loss), `false` if it is a side channel the injection did not need to leave (history, timestamps, names, leftover helpers, logs).

## Rules

- Observe only. Do not change configuration, restart services, kill processes, or write, move, or delete files. Use read-only commands (`cat`, `ls`, `stat`, `find`, `ps`, `ip ... show`, `tc ... show`, `iptables -S`, `nft list ruleset`, `vtysh -c 'show ...'`, `kubectl get`, and similar).
- Use the provided MCP tools (named `mcp__<server>__<tool>`). You may also look at your own local workspace, but the network devices are reachable only through MCP tools.
- Sweep cheaply and widely: one call per device, every device, before going deep on any one. Start with this single read-only sweep on each device (adapt paths if a tool is missing):

  ```
  cat /root/.*history /home/*/.*history 2>/dev/null | tail -n 30; echo ---PS; ps -eo pid,lstart,args | grep -v '[p]s -eo'; echo ---NEW; timeout 5 find / -xdev -type f -newer /etc/hostname -not -path '/proc/*' -not -path '/sys/*' -not -path '/run/*' -not -path '/var/lib/*' 2>/dev/null | head -n 40; echo ---TMP; ls -la --time-style=full-iso /tmp /root 2>/dev/null | head -n 40
  ```

  `/etc/hostname` is written when the container starts, so `---NEW` lists files changed after deployment. If a sweep times out, rerun its parts as separate calls. Then compare devices of the same role: a file, process, rule, or qdisc that only one of several identical devices has is a strong lead. Follow up only on leads (`cat`, `stat`, `tc qdisc show`, `iptables -S`, `nft list ruleset`, `ip route`, `ip neigh`).
- You have a budget of 100 turns. Never run the same command on the same device twice. Once every device is swept and each lead is followed up, stop calling tools and write the final answer; do not keep digging on one device. A trace present on several identical devices is design, not injection.
- Every trace will be checked deterministically: its excerpt must appear verbatim in the cited tool call's output, and the same call is replayed on a fault-free deployment of this network. Anything also present there is not a trace. Do not guess, and do not report normal design features. A report with zero traces is a valid outcome.

## Fault types

Use one of these ids for `fault_type`:

{fault_types}

## Final answer format

End your final answer with exactly one fenced ```json block of this shape (and nothing after it):

```json
{
  "traces": [
    {
      "id": "T1",
      "trace_type": "command_history",
      "part_of_fault_mechanism": false,
      "device": "router1",
      "observation": "What you directly observed, concretely.",
      "why_it_leaks": "Why this reveals the fault without diagnosis.",
      "evidence": [
        {
          "tool": "exec_shell",
          "args": {"host_name": "router1", "command": "cat /root/.bash_history"},
          "excerpt": "A short verbatim fragment copied exactly from that tool call's output."
        }
      ],
      "inferred_answer": {"fault_type": "<fault type id or null>", "device": "<device name or null>", "confidence": "high"}
    }
  ],
  "best_guess": {"is_anomaly": true, "fault_type": "<fault type id or null>", "device": "<device name or null>", "based_on": ["T1"]},
  "searched": ["What you checked, per device or role."],
  "blind_spots": ["What you could not check and why."]
}
```

Field rules:
- `evidence[].tool` is the MCP tool name without the `mcp__server__` prefix, `args` are the exact arguments you passed, and `excerpt` is copied verbatim from that call's output.
- `confidence` is `high`, `medium`, or `low`.
- With no traces, return `"traces": []` and a `best_guess` with `fault_type` and `device` set to null; still list what you searched.
- The submission step that follows will submit your `best_guess`.
