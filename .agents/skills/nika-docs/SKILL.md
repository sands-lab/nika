---
name: nika-docs
description: Write or revise NIKA's human-facing documentation, including guides, the homepage README, and integration examples, around reader purpose, the shortest useful path, and consistent benchmark terminology.
---

# NIKA documentation

Help readers understand the purpose, essential contract, and next action with minimal effort.

## Writing principles

* **Lead with reader goals and benefits.** Make the reason clear before the action or mechanism. Apply this to document structure, section introductions, and sentence construction: organize around what readers want to achieve, establish the need a section addresses, and connect instructions to their outcome. Explain internals only after readers know what they gain. Convey purpose naturally; avoid formulaic “Why?” headings and extra preamble when context already supplies the reason.
* **Lead with the shortest useful path.** Once the purpose is clear, give readers the essential steps. Put alternatives and secondary details later or in linked references. Cut repetition rather than merely moving it into more headings. Separate the main path from reference material with a visible divider, and say in one sentence what the reference part is for. In some cases is better to title reference sections by when the reader needs them ("If an upload fails", "How validation works"), rather than by the mechanism they describe.
* **State the problem before the solution.** For an optional or extra step, first describe what goes wrong without it, then what the step gives the reader. “To run it by name” states the result, not the reason.
* **Name sections after what the reader builds.** Use precise nouns for the thing the reader is creating, such as “Write your agent.” Avoid a nearby activity (“Write the investigation”) or internal jargon.
* **Write clear, complete sentences.** Use the conventional language of network and AI engineering. Rewrite compressed, clause-packed, or cryptic sentences, and cut prose that a code example already shows.
* **Describe capabilities concretely, without hype.** Make NIKA appealing through specific things readers can do and observe. Avoid superlatives and unsupported claims.
* **Keep sections in proportion.** Secondary mechanics, such as registration internals or dependency caveats, should not outweigh the main path.
* **Keep essential contracts visible.** Show the smallest accurate code snippet that communicates what readers must implement. Preserve required inputs, outputs, and behavior; brevity should not erase the interface.
* **Keep internals out of user guides.** Describe what the reader sees and does: the command, its outcome, and the error with its cause and fix. Leave out internal labels, flags, module names, intermediate mechanisms, and file or schema layouts. Show an artifact only if the reader edits it, inspects it, or must act on it; otherwise say in one sentence what it holds and who produces it. If one internal concept maps to several user-facing outcomes, describe each outcome and drop the concept. Explain constructors, logging, orchestration, and submission only when readers must handle them; otherwise rely on existing abstractions and link to implementation references.
* **Respect configured defaults.** Include command options and configuration fields only when the example needs them. Avoid model, step-limit, or other overrides that add no value to the reader's task. When examples form a sequence, keep one continuous story that reuses the same defaults.
* **Give each idea one home.** Explain a concept once, in the place readers look for it, and link to it elsewhere. Merge overlapping points, such as fair evaluation and comparable results, into one statement. Point to maintained implementations and example configurations rather than copying their scaffolding.
* **Use meaningful link labels.** Link the harness or component name directly when it identifies the destination. Avoid catalogs with repeated generic labels such as “YAML” or “Example config.”
* **Choose structure for readability.** Use tables when readers need to compare meaningful attributes, lists for simple catalogs, and reference sections for detail. Do not impose a fixed document template.
* **Let figures speak.** Do not narrate what a diagram or screenshot already shows. Keep descriptive alt text so the figure remains accessible.
* **Prefer concrete evidence to dense explanation.** When an output excerpt makes behavior clearer, use a short, verified example and identify its source accurately. Distinguish terminal output from recorded logs; do not fabricate observations or add transcripts without a reader benefit.
* **Keep simplification accurate.** Check the current implementation before describing an interface. Verify every command, flag, path, and configuration field against the code or a real run. If the desired simpler contract requires runtime changes, explain the limitation briefly and track the proposed change in an issue when authorized.
* **Stay within the reader's task.** Include only what a reader outside the project needs to complete the guide's task. Leave out internal limitations, roadmap issues, and evaluator or runtime internals; put them in references or issues instead. Skip second-order mechanics, such as credential plumbing, platform fallbacks, and what maintainers or downstream services do afterwards, unless the reader must act on them; when something is needed only in passing, say it in a few words.
* **Define terms before you use them.** Recap the terms a guide depends on near the top, using glossary wording, and keep one word per concept throughout. Define or link an artifact (a file, a package, a config) at its first mention.
* **Do not hard-code values the user controls.** Counts, sizes, and defaults decided by a release, a configuration, or an option stay out of prose. State the rule ("every trial the release defines") instead of an example number.
* **Document only the committed contract.** Describe behavior the repository implements today. Do not tell readers to do by hand what NIKA may soon handle, and do not hint at planned changes. If the current behavior is uncertain, ask before writing.
* **Put a rule where it applies.** A requirement that holds for every path goes once into the prerequisites, not under one variant of the task. Before placing a paragraph, ask which readers need it.
* **Do not restate what the workflow already guarantees.** Skip verification checklists that repeat steps the tool performs and reports itself. Include a checklist only for checks the reader must make by hand.

For example, “To investigate incidents with your own agent, implement the diagnosis contract” makes the purpose clear before the instruction.

Avoid notes like this one, taken from an earlier integration guide. It refers to internal evaluator behavior, an open issue, and a full contract that the reader does not need to finish the task:

```markdown
The complete NOOA agent also records steps and tool calls for the evaluator and honors the step limit.

Inheriting from LangGraph is a current limitation; a framework-independent base class is tracked in #172. The integration reference describes the full contract.
```

## Structure and formatting

* **Give actions and options a structure.** Use a bullet list for what a command does, a table for flags and options, and a numbered list only for steps the reader performs. Do not put what a command does in a paragraph, or a list of flags in comments inside a code block.
* **Use emphasis to counter long or dense pages.** Callouts and emoji exist to keep an important detail from disappearing in long, repetitive text. Plain prose and headings are the default. Reach for emphasis when a page is long or verbose enough that readers will skim past something they must not miss.
* **Scale emphasis to the page's importance.** Judge how critical the page is and where its stakes are. A page that readers must follow exactly, such as the leaderboard submission guide, justifies more visual aid. Detailed reference pages usually need none.
* **Use callouts for what readers must not miss.** `[!IMPORTANT]` marks a requirement that blocks success, `[!TIP]` an optional shortcut, `[!NOTE]` context that changes a decision, and `[!WARNING]` a failure path or risk. Prefer few, and prefer prose when the point does not need to stand out.
* **Use emoji sparingly and only where they help scanning.** Typically that means the title and a few top-level headings on a high-stakes, long guide. Skip them on reference pages and in body text. If they start to feel decorative or repetitive, remove them.
* **Keep link targets stable.** Emoji and renames change a heading's anchor. Leave linked headings plain, and search for inbound links (`grep` for the old anchor) whenever you rename, move, or remove a section.

## Homepage README

The README is the entry point, not the reference manual. It should tell a new reader what NIKA is, what they can do with it, and how to run a first task, then link to the docs for everything else.

* **Features.** Keep a stable, curated list of NIKA's most salient benchmark characteristics at one consistent, high level. Do not turn it into an inventory of recent pull requests. Preserve the curated wording and structure unless it is materially inaccurate or a change is explicitly requested. Leave out exhaustive network lists, scenario or failure IDs, Dev/Test splits, and ground-truth mechanics.
* **Evaluation.** Describe supported detection, localization, and diagnosis capabilities rather than reducing evaluation to root-cause identification. Do not imply separate metrics without verification. State fair, reproducible comparison once.
* **Ground truth.** Treat ground truth as a baseline expectation of any benchmark, not as a headline distinction.
* **News.** Keep release news on the website, not in the README.
* **Prerequisites.** Show prerequisites as a visible, labeled list before the commands that need them. Keep essential safety constraints visible rather than hiding them in linked detail.
* **Configuration.** Show the minimal path: NIKA ships preconfigured agents and per-agent templates, the installer creates `config/nika.yaml` from `config/nika.example.yaml`, and `uv run nika config show` displays the active settings. Distinguish settings in `config/nika.yaml` from secrets in `.env`. Move custom providers, endpoint protocols, and other detail to linked docs.
* **Quick start.** Tell one continuous story that relies on configured defaults and adds only the flag needed to select a task. Show how to discover tasks with a verified CLI command or the interactive shell.
* **Interactive shell.** Present `uv run nika` as a first-class entry point, with its own visible section and, optionally, an authentic screenshot or terminal excerpt.
* **Reference images.** An earlier README on `main` can guide accessible presentation, but current terminology and accuracy take precedence over its wording.

## Terminology

Use the terms defined in the [glossary](../../../GLOSSARY.md), choosing the one that fits what the reader is doing. A NIKA benchmark is a set of **tasks**; each task combines a **network environment**, a **network incident**, and a **network traffic pattern**.

* Prefer “task” to “case” and “environment” to “lab.” “Scenario” is an acceptable alternative to “environment,” but prefer “environment.”
* Write “benchmark release,” not “frozen benchmark release.” Readers “try” or “run” a task, not “smoke” an incident.
* Keep literal code, API, CLI, and configuration identifiers and product names as they are, such as `nika case browse`, `--problem`, `scenario:`, or LangGraph. Explain how a prose term maps to an identifier only when the reader needs it. Documentation terminology does not rename runtime interfaces.
* Add a term to the glossary when a document introduces it. Link relevant research when it helps readers understand the approach.

## Review

Read the draft as someone trying to complete the task: is the purpose clear, is the next action easy to find, and does each paragraph, figure, or snippet earn its place? Check links, defaults, commands, and examples against their sources. Apply the repository's verification rules when executable behavior or examples change.

Also check:

* Does any term appear before it is defined?
* Does any detail serve only a rare path or a maintainer?
* Could the reader act without this internal term, file, or flag?
* Does every inbound link and anchor still resolve?
* Does each callout or emoji stop a reader from missing something, or is it decoration?
