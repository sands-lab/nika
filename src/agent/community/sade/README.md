# SADE — Symptom-Aware Diagnostic Escalation (community agent)

SADE is a methodology-grounded Claude Code agent for the NIKA network-troubleshooting
benchmark. It pairs a **phase-gated diagnostic workflow** (separating evidence
acquisition from hypothesis commitment) with a **15-skill library** that maps symptoms
to confirmation patterns, and plugs into NIKA's standard pluggable-agent interface
(`agent.protocols.TroubleshootingAgent`).

> Built on top of **[NIKA](https://github.com/sands-lab/nika)** — SADE uses NIKA's
> unmodified orchestrator, fault-injection environment, and evaluation pipeline, and
> adds only the agent under this directory.

## Paper / citation

This work is presented in the paper **"SADE: Symptom-Aware Diagnostic Escalation for
LLM-Based Network Troubleshooting"** — https://arxiv.org/abs/2605.04530

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

SADE drives Claude Code through the Anthropic Agent SDK, declared as an optional extra:

```bash
uv sync --extra sade          # or: pip install -e ".[sade]"
```

Set Anthropic-compatible credentials in the repo-root `.env` (same as
`cli.claude`; see [`.env.example`](../../.env.example)):

- `ANTHROPIC_API_KEY`, or
- `ANTHROPIC_AUTH_TOKEN` + optional `ANTHROPIC_BASE_URL` (e.g. DeepSeek)

## Run

```bash
nika agent run -a community.sade -n 20
```

Produces the same session artifacts as the other agents (`messages.jsonl`,
`submission.json`) and submits through NIKA's task MCP server.

## Layout

```
src/agent/community/sade/
├── agent.py            # SadeAgent (TroubleshootingAgent contract)
├── h.py                # helper-script launcher used by the skills (python h.py <script>)
├── prompts/            # sade_prompt.py (phase-gated workflow)
└── .claude/
    ├── CLAUDE.md       # fault-routing + tool index
    └── skills/         # 15 skills: 12 fault-family books, diagnosis-methodology
                        # (with read-only helper scripts), and 2 utility books
```

## How it works

- **Diagnosis** runs inside a single Claude Code session against NIKA's Kathara MCP
  servers. The system prompt enforces five phases (blind start → branch → symptom-first
  diagnosis → broad-search escalation → submission); the skill library and `CLAUDE.md`
  index gate which fault family is entered, and only after a real symptom implicates it.
- **Submission** calls the task MCP server's `submit` tool with the canonical
  `root_cause_name` / `faulty_devices`.
- The skills' read-only helper scripts (`infra_sweep`, `ospf_snapshot`, `bgp_snapshot`,
  …) are invoked through `h.py`, which runs them with the project interpreter and injects
  the active lab name from the running NIKA session.
