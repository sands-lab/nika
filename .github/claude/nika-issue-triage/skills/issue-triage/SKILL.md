---
name: issue-triage
description: Triage a new NIKA GitHub issue — labels, duplicates and related work, agent-ready vs needs-human — and write the single triage comment in NIKA's maintainer voice.
---

# NIKA issue triage

A triage comment helps a maintainer decide, in one read, what the issue is about, what it overlaps with, and whether an agent can implement it now. Every claim in the comment should be something you checked.

## Which code the issue is about

Issues come from maintainers working on `dev` and from users running a release or `main`, which can lag `dev` by many commits. Before judging, decide which line the report describes.

* Use explicit evidence first: a stated version, commit, branch, or benchmark release; code paths, flags, or behavior that exist on only one line. The reporter's association (`OWNER`, `MEMBER`, or `COLLABORATOR` versus others) is a weak hint, not evidence.
* Check the behavior on both `main` and `origin/dev` when they differ in the affected code (`git diff origin/main origin/dev -- <path>`, `git show origin/dev:<path>`). The installed NIKA is `main`; reproduce on `dev` only if reading the code does not settle it.
* Say which line you checked in the Triage section. When the problem exists on `main` but `dev` already fixes it, say so and link the fixing change; the reporter needs to know the fix ships with the next release, and maintainers need to decide whether to promote it, not reimplement it.
* Judge agent-readiness against `dev`, where implementation happens.
* Ask the reporter for details only when the answer changes the triage and you cannot infer it: which version or commit they ran, the command or configuration, or the task ID and benchmark release. Ask in a `### Needed from the reporter` section placed before Triage, with at most three specific questions, and keep the label `needs-human` until it is answered.

## Judgment

* **agent-ready** only for a well-scoped implementation or quick fix with clear acceptance criteria, no open design questions, and no change to benchmark contracts (taxonomy, root-cause identity, ground truth, registries, benchmark cases, published benchmark data, agent-visible tools or telemetry).
* **needs-human** for open design or architectural choices, ambiguous requirements, benchmark-contract changes, or anything you are unsure about. Be conservative.
* Verify the report against the code it describes before judging. If the issue misdescribes the code, say so plainly ("One important correction: ...") and show the evidence.
* Distinguish a duplicate (the same request or the same underlying bug) from related work (same area, different request). When in doubt, list it under Related and say why it is not a duplicate.

## Comment structure

Use only these sections, in this order, and omit empty ones. Start the comment with the `<!-- nika-issue-triage -->` marker.

### `### TL;DR`

Only when the issue is long or highly technical. In 2–4 plain sentences, state what is wrong or wanted and why it matters to benchmark users, usually through its effect on agents, scores, or reproducibility. Leave out implementation details. Say what has and has not been established, for example "No leak was observed in the cited run" or "its effect on diagnoses has not been measured."

### `### Possible duplicates` and `### Related`

One bullet per item: `- #N — <what it is>; <how it relates>.` Give its state when it matters ("merged into `dev`", "open draft", "closed by #276"). End with an explicit verdict when an item could be mistaken for a duplicate: "related, not a duplicate", "broader than this correction", "complementary rather than a duplicate". Link items in other repositories as `[owner/repo#N](url)`.

### `### Needed from the reporter`

Only when the rules under "Which code the issue is about" call for it. Address the reporter directly and politely, one question per bullet.

### `### Triage`

* Open with the verdict and a dash: `**needs-human** — <one-sentence reason>.` or `**agent-ready** — <one-sentence reason>.`
* Then state the evidence basis and what it confirmed: "Source inspection of `dev` confirms ...", "Inspection of the post-#367 code confirms ...". Cite code as `path:line` or as a GitHub permalink pinned to a commit SHA.
* For **needs-human**, end with "Before implementation, maintainers need to decide:" followed by bullets that are each a specific, answerable question ending in `?`. Frame trade-offs as concrete alternatives, not open-ended prompts.
* For **agent-ready**, state the acceptance criteria and what the change must preserve (existing errors and messages, host-side behavior, published data).
* When benchmark contracts are involved, name the constraint: published releases stay unchanged, and agent-visible changes target a new benchmark revision under `VERSIONING.md`.
* Close with how the triage was done when that is useful: "No labs were started; source inspection sufficed." or "No duplicate found in searches of open and closed issues and pull requests."

## Voice

* Measured, precise, and maintainer-facing. Write complete sentences in the conventional language of network and AI engineering. No hype, emojis, filler, or praise of the issue.
* Prefer specific nouns and code identifiers in backticks over vague references ("the shared topology function" with a link, not "some code").
* Separate observation from inference: say "confirms" only for what you checked, and "suggests" or "has not been isolated" otherwise.
* Keep it short. A Triage section of one to three paragraphs plus a question list is typical; do not restate the issue.
