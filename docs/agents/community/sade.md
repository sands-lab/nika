# SADE community agent

This reference is for operators and maintainers of NIKA's Symptom-Aware Diagnostic Escalation (SADE) community agent. SADE runs Claude Code through a phase-gated diagnostic workflow and a 15-skill network troubleshooting library. It subclasses `agent.base.DiagnosisAgent`.

Implementation: [`agent.py`](../../../src/agent/community/sade/agent.py) implements the protocol; [`prompts/`](../../../src/agent/community/sade/prompts/) and [`.claude/skills/`](../../../src/agent/community/sade/.claude/skills/) contain the diagnostic workflow.

## Cite the method

The paper ["SADE: Symptom-Aware Diagnostic Escalation for LLM-Based Network Troubleshooting"](https://arxiv.org/abs/2605.04530) describes the method.

If you use SADE in academic research, please cite the paper:

```bibtex
@misc{sade2026,
  title         = {SADE: Symptom-Aware Diagnostic Escalation for LLM-Based Network Troubleshooting},
  year          = {2026},
  eprint        = {2605.04530},
  archivePrefix = {arXiv},
  primaryClass  = {cs.NI},
  url           = {https://arxiv.org/abs/2605.04530}
}
```

## Install

SADE drives Claude Code through the Anthropic Agent SDK. NIKA installs the SDK and Claude Code in SADE's sandbox, so the host needs nothing beyond a [sandbox runtime](../../operations/agent-sandbox.md).

Set provider credentials in the repo-root `.env` (same as `cli.claude`; see [`.env.example`](../../../.env.example)), and select the provider under `agent.provider` in `config/nika.yaml`:

- `DEEPSEEK_API_KEY` with `agent.provider: deepseek`
- `ANTHROPIC_API_KEY` with `agent.provider: anthropic`
- Optional `NIKA_CUSTOM_API_KEY` with `agent.provider: custom` and `agent.base_url`

## Run

```bash
nika agent run -a community.sade -n 20
```

SADE writes `messages.jsonl` and `submission.json`, then submits through NIKA's task MCP server.

## Layout

```
src/agent/community/sade/
├── agent.py            # SadeAgent (DiagnosisAgent subclass)
├── h.py                # saved-output parser launcher (python h.py parse_large <file>)
├── prompts/            # sade_prompt.py (phase-gated workflow)
└── .claude/
    ├── CLAUDE.md       # fault-routing + tool index
    └── skills/         # 15 diagnostic and utility skills
```

## How it works

- **Diagnosis** runs inside one Claude Code session against NIKA's diagnosis MCP servers, with the host-enforced [request budget](../agent-reference.md#step-limit). The system prompt enforces the SADE workflow: blind start, branch, symptom-first diagnosis, and broad-search escalation. The skill library and `CLAUDE.md` index select a fault family after observed evidence implicates it. At half the budget, a `PostToolUse` hook adds one reminder to the next tool result that asks SADE to write its final report once evidence is conclusive.
- **Submission** runs in a second Claude Code session with only the task MCP server. SADE gets the frozen report plus the fault ontology and resource catalog, and calls `submit` with `root_causes` as `[{resource_id, fault_type}, ...]`. See the [root-cause ground truth and scoring reference](../../benchmarks/root-cause-evaluation.md).
- The diagnostic skills guide network inspection through the session's MCP tools. `h.py` launches `parse_large` to summarize oversized saved tool output; it does not access the network.
