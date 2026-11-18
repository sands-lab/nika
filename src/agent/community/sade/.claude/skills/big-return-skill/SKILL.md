---
name: big-return-skill
description: Parse oversized MCP tool output. When a tool returns "Output has been saved to <file>.txt" because the result exceeded Claude Code's token limit, run this skill's parser instead of re-reading the file in chunks. Covers `get_reachability` on large topologies.
---

# Oversized Tool Outputs

When a tool reports:

```text
Error: result (N characters) exceeds maximum allowed tokens.
Output has been saved to <file_path>.txt
```

Run the parser via the launcher:

```
python h.py parse_large "<file_path>"
```

Auto-detection picks one of two formatters based on payload shape:

- **Reachability**: payload carries `hosts` + `results` (as returned by `get_reachability()`). The reachability formatter groups failures by source/destination, surfaces clustered suspect destinations, and preserves the host/IP inventory.
- **Generic**: any other oversized JSON falls through to a key/structure summary; plain-text payloads are emitted truncated.

Override with `--type reachability` or `--type generic` only if auto-detection picks the wrong formatter.

## Reading the parser output

- Rows missing `tx`, `rx`, `loss_percent`, or `rtt_avg_ms` are NOT healthy — treat as investigation leads.
- If many sources show suspicious rows toward a small destination set, that's a shared-path symptom: check the path or the shared upstream router before blaming the source hosts.
- The parser output is triage guidance, not final proof. Confirm locally before naming a faulty device.
