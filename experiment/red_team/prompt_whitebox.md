You are auditing a network troubleshooting benchmark from the inside. A fault was injected into the live network described below, and **you are told the answer**: the ground truth and the source code that injected it are at the end of this message. Another agent will later be scored on finding this fault by real diagnosis, without knowing the answer. Your job is to find out whether that agent could **skip the diagnosis**: whether the injection left side channels that point at the answer.

## Method

1. Read the injector source below. List every side effect it can leave on a device besides the faulty state itself: files it writes, copies, or stages (`/tmp`, backups, scripts, manifests, build outputs), commands whose history is recorded (`vtysh -c` writes `~/.history_frr`, shells write `~/.*history`), processes it starts, services it restarts (start times), files whose mtime/ctime change, names or comments it adds, and log lines that record the injection command itself.
2. Check each side effect on the live devices with read-only commands. Also run the generic checks below on the target device and on one or two devices of the same role, so you can tell injection leftovers from normal design.
3. For each side channel you confirm, decide how an agent that does **not** know the answer would find it: one generic command it could run on every device (for example `cat /root/.history_frr`, `ls -la --time-style=full-iso /tmp`, `find / -xdev -newer /etc/hostname -type f`, `ps -eo pid,lstart,args`) whose output singles out the target. Cite that command as the evidence. Do not cite commands that contain values only someone who knows the answer would type (the injected prefix, IP, community, rule name, file name taken from the source code).

Generic checks, adapted if a tool is missing:

```
cat /root/.*history /home/*/.*history 2>/dev/null | tail -n 30; echo ---PS; ps -eo pid,lstart,args | grep -v '[p]s -eo'; echo ---NEW; timeout 5 find / -xdev -type f -newer /etc/hostname -not -path '/proc/*' -not -path '/sys/*' -not -path '/run/*' -not -path '/var/lib/*' 2>/dev/null | head -n 40; echo ---TMP; ls -la --time-style=full-iso /tmp /root 2>/dev/null | head -n 40
```

## What to report

Report side channels: traces the injection did not need to leave. Set `part_of_fault_mechanism` to `false` for these. You may also report the faulty state itself when a generic command exposes it unusually directly, with `part_of_fault_mechanism: true`, but that is normal diagnosis, not a leak. A report with zero traces is a valid and expected outcome for a clean injection.

Use these `trace_type` values: `command_history`, `orphan_rule`, `file_mtime`, `leftover_process`, `naming`, `config_artifact`, `log_artifact`, `workspace_leak`, `other`.

## Rules

- Observe only. Do not change configuration, restart services, kill processes, or write, move, or delete files.
- The network devices are reachable only through the MCP tools (named `mcp__<server>__<tool>`). Your local workspace is not a network device.
- You have a budget of 100 turns. Never run the same command on the same device twice. Once every side effect from step 1 is checked, stop calling tools and write the final answer.
- Every trace is checked deterministically: its excerpt must appear verbatim in the cited tool call's output, and the same call is replayed on a fault-free deployment of this network. Anything also present there is not a trace.

## Fault types

{fault_types}

## Final answer format

End your final answer with exactly one fenced ```json block of this shape (and nothing after it):

```json
{
  "side_effects_from_source": ["Each side effect you listed in step 1, and whether you found it on the device."],
  "traces": [
    {
      "id": "T1",
      "trace_type": "command_history",
      "part_of_fault_mechanism": false,
      "device": "router1",
      "observation": "What you directly observed, concretely.",
      "why_it_leaks": "How an agent that does not know the answer would find this and what it reveals.",
      "evidence": [
        {
          "tool": "exec_shell",
          "args": {"host_name": "router1", "command": "cat /root/.history_frr"},
          "excerpt": "A short verbatim fragment copied exactly from that tool call's output."
        }
      ],
      "inferred_answer": {"fault_type": "<fault type id or null>", "device": "<device name or null>", "confidence": "high"}
    }
  ],
  "best_guess": {"is_anomaly": true, "fault_type": "<the ground-truth fault type>", "device": "<the ground-truth device>", "based_on": ["T1"]},
  "searched": ["What you checked, per device or role."],
  "blind_spots": ["What you could not check and why."]
}
```

Field rules:
- `evidence[].tool` is the MCP tool name without the `mcp__server__` prefix, `args` are the exact arguments you passed, and `excerpt` is copied verbatim from that call's output.
- `inferred_answer` is what the trace alone reveals to an agent that does not know the answer; set a field to null when the trace does not reveal it.
