# SADE community agent

SADE (Symptom-Aware Diagnostic Escalation) is a community agent that runs Claude Code through a phase-gated troubleshooting workflow and a library of 15 network troubleshooting skills. It is also a worked example of a large skill library; see [Give an agent skills](../agent-skills.md).

## Cite the method

The paper ["SADE: Symptom-Aware Diagnostic Escalation for LLM-Based Network Troubleshooting"](https://arxiv.org/abs/2605.04530) describes the method. If you use SADE in academic research, please cite it:

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

## Set up

NIKA installs the Anthropic Agent SDK and Claude Code in SADE's sandbox. The host needs only a [sandbox runtime](../../operations/agent-sandbox.md).

Set the provider key in the repository's `.env`, as for `cli.claude` (see [`.env.example`](../../../.env.example)), then select the provider with `agent.provider` in `config/nika.yaml`:

- `DEEPSEEK_API_KEY` with `agent.provider: deepseek`
- `ANTHROPIC_API_KEY` with `agent.provider: anthropic`
- `NIKA_CUSTOM_API_KEY` with `agent.provider: custom` and `agent.base_url`, for an authenticated custom endpoint

## Run

```shell
uv run nika agent run -a community.sade
```

SADE writes `messages.jsonl` and `submission.json`, then submits through NIKA's task MCP server. `agent.max_tokens` does not apply to SADE.

## How it works

- **Diagnosis** runs in one Claude Code session against NIKA's diagnosis MCP servers, within the [step limit](../agent-reference.md#step-limit). The system prompt sets the SADE workflow: a blind start, branching, symptom-first diagnosis, and broad-search escalation. After observed evidence points to a failure family, the skill library and its `CLAUDE.md` index select the matching skills. When the agent reaches half of its step budget, a `PostToolUse` hook adds one reminder to the next tool result, asking SADE to write its final report once the evidence is conclusive.
- **Submission** runs in a second Claude Code session that has only the task MCP server. SADE receives the frozen report, the fault ontology, and the resource catalog, and calls `submit` with `root_causes` as `[{resource_id, fault_type}, ...]`. See the [root-cause ground truth and scoring reference](../../benchmarks/root-cause-evaluation.md).
- The diagnostic skills guide network inspection through the session's MCP tools. The `h.py` launcher runs `parse_large` to summarize oversized saved tool output. It does not access the network.

---

The rest of this page is reference for maintainers.

## Layout

```
src/agent/community/sade/
├── agent.py            # SadeAgent (DiagnosisAgent subclass)
├── h.py                # saved-output parser launcher (python h.py parse_large <file>)
├── prompts/            # sade_prompt.py (phase-gated workflow)
└── .claude/
    ├── CLAUDE.md       # fault-routing and tool index
    └── skills/         # 15 diagnostic and utility skills
```

Implementation: [`agent.py`](../../../src/agent/community/sade/agent.py) implements the protocol. [`prompts/`](../../../src/agent/community/sade/prompts/) and [`.claude/skills/`](../../../src/agent/community/sade/.claude/skills/) hold the diagnostic workflow.
