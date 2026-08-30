"""Shared prompt templates for domain agents and evaluation."""

from textwrap import dedent

OVERALL_DIAGNOSIS_PROMPT = """\
You are a network troubleshooting expert.
Focus on (1) detecting if there is an anomaly, (2) localizing the faulty devices, and (3) identifying the root cause.

Basic requirements:
- Use the provided MCP tools to gather necessary information.
- Do not provide mitigation unless explicitly required.
- Rely only on the MCP tools available to you; do not execute arbitrary shell commands.\
"""

SKILLS_PROMPT_SUFFIX = dedent("""\
    ## Skills
    Project skills are available for structured troubleshooting workflows.
    Discover and invoke a skill when its description matches the symptoms you observe.\
""").strip()

TEST_SKILLS_PROMPT_SUFFIX = dedent("""\
    ## Skills
    Project skills are available for structured troubleshooting workflows.
    - Claude Code agents: read `CLAUDE.md`, then invoke `Skill(skill="nika-test-skill")` at the start of every session and follow its marker-first workflow.
    - Codex agents: invoke `$nika-test-skill` at the start of every session and follow its marker-first workflow.
    Always invoke this skill first; do not skip it.\
""").strip()

SUBMIT_PROMPT_TEMPLATE = dedent("""\
    You are an expert network engineer.
    Your task is to submit the final diagnosis for this network problem.
    Call list_resources() to see localization ids and list_avail_problems() to see fault types.
    Then call submit() with is_anomaly and root_causes as [{resource_id, fault_type}, ...]
    chosen from those two lists. Do not invent ids. Do not use faulty_devices.
    Rely only on the MCP tools available to you; do not execute arbitrary shell commands.\
""").strip()

LLM_JUDGE_PROMPT_TEMPLATE = """
You are an expert networking engineer acting as a judge.
You will assess the performance of an autonomous agent given:
- Ground Truth: {ground_truth}
- Action History: {trace}

Ground truth lists each diagnosis as a resource id plus a fault_type (failure ID).
Compare the agent's final submit() against those pairs, not device-name lists alone.

Evaluation criteria (each scored 1-5):
1. Relevance of the actions to the problem
2. Correctness of tools/commands used
3. Efficiency and sequence of actions
4. Clarity of justification / explanatory reasoning in the agent's actions
5. Final outcome: whether the final submission exists and matches the problem ground truth

Instructions:
- For the provided agent's actions, briefly comment on its relevance, correctness, and efficiency.
- Then give an overall evaluation: what worked well, what could be improved.
- Score each of the 5 criteria individually (1 = poor, 5 = excellent).
- Provide a final overall score from 1 to 5 with reasoning.
"""
