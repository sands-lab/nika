# Curator guide: turning a finding into knowledge

Add knowledge one issue at a time, so each change can be attributed, reviewed, and reverted.

## Write the issue

Create `kb/issues/<ID>.yaml` in the skill directory (or in the `--kb` snapshot you work on). IDs are upper-case words joined by `-`, starting with a class prefix (`LEAK-`, `HARNESS-`, `POLICY-`, `LURE-`, `FNR-`, `SIG-`, `LABEL-`, `GRADE-`, `AGENT-`, `OOS-`, `RUN-`, `HIDDEN-`).

```yaml
id: LEAK-EXAMPLE-NAME
title: <what is wrong, one line>
class: shortcut                     # a taxonomy.yaml id
severity: block                     # block | warn | info
status: open                        # open | fixed | by_design | wontfix
summary: <what an agent sees and why it matters>
guideline: <how a reviewer recognises it and what does not count>
scope:
  fault_types: [<fault_type>]      # empty: any
  scenarios: []
detect:
- kind: text
  where: [tool_result]
  pattern: <regex that matches the evidence>
exploit: true                       # trials that hit it score 0 in the audited score
provenance:
  source: <leaderboard submission slug and audit run>
  date: 'YYYY-MM-DD'
  refs: ['<trial_id>:<step>', '#<issue>']
  found_by: agentic                 # manual | deterministic | agentic | live_audit
evidence: ['<trial_id> step <n>: <excerpt>']
```

Signature kinds and fields are documented on `Signature` in `scripts/auditlib/kb.py`. Prefer the narrowest signature that still matches every source trial. Set `fixed_in` only together with `status: fixed`.

## Check it

```bash
uv run python $AUDIT kb-validate
uv run python $AUDIT kb-check <kb>/issues/<ID>.yaml \
  --source <trajectories>/trials/<source trial>
```

`kb-check` passes when the file loads (valid fields, patterns compile), its class exists, and its signatures fire on every trial passed with `--source`. These checks only show that the issue is well formed; whether it is right is decided by the person who reviews the change. Before adding a new issue, run `kb-list` and extend an existing issue instead when it is the same root cause.

Then rerun `audit.py run` on the leaderboard submission and check that the finding's trials moved from a lead to the new known issue. When the audit taught you something the next audit should do differently, add it to [the lessons](lessons.md) in the same change.

## Propose the change

Each issue file stays separate, so it can be attributed, reviewed, and reverted. Unless told otherwise, leave the files in the working tree and list them in your report; a maintainer reviews and commits them. When asked to open a pull request, put one audit's new issue files and lessons in one pull request, title `feat(audit): add issues from <leaderboard submission slug>` (or `fix(audit): narrow <ID>`), label `bug-from-audit` when the issue can bias diagnosis results, and put the finding, the skeptic verdict, and the `kb-check` report in the body. The maintainer who merges the pull request is the one who accepts the issue.

When the benchmark defect is fixed, a follow-up sets `status: fixed` and `fixed_in: <commit>`; from then on a hit on a newer commit is reported as a regression.
