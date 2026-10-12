---
name: nika-docs
description: Write or revise NIKA's human-facing documentation, including guides, README files, and integration examples, around reader purpose and the shortest useful path.
---

# NIKA documentation

Help readers understand the purpose, essential contract, and next action with minimal effort.

## Writing principles

* **Lead with purpose.** Make the reason clear before the action or mechanism. Apply this to document structure, section introductions, and sentence construction: organize around reader goals, establish the need a section addresses, and connect instructions to their intended outcome. Convey purpose naturally; avoid formulaic “Why?” headings and extra preamble when context already supplies the reason.
* **Lead with the shortest useful path.** Once the purpose is clear, give readers the essential steps. Put alternatives and secondary details later or in linked references. Cut repetition rather than merely moving it into more headings.
* **Keep essential contracts visible.** Show the smallest accurate code snippet that communicates what readers must implement. Preserve required inputs, outputs, and behavior; brevity should not erase the interface.
* **Keep plumbing out of introductory guides.** Explain constructors, logging, orchestration, and submission only when readers must handle them. Otherwise rely on existing abstractions and link to implementation references.
* **Respect configured defaults.** Include command options and configuration fields only when the example needs them. Avoid model, step-limit, or other overrides that add no value to the reader's task.
* **Link instead of repeating.** Point to maintained implementations and example configurations rather than copying their scaffolding. Give each concept one primary explanation.
* **Use meaningful link labels.** Link the harness or component name directly when it identifies the destination. Avoid catalogs with repeated generic labels such as “YAML” or “Example config.”
* **Choose structure for readability.** Use tables when readers need to compare meaningful attributes, lists for simple catalogs, and reference sections for detail. Do not impose a fixed document template.
* **Prefer concrete evidence to dense explanation.** When an output excerpt makes behavior clearer, use a short, verified example and identify its source accurately. Distinguish terminal output from recorded logs; do not fabricate observations or add transcripts without a reader benefit.
* **Keep simplification accurate.** Check the current implementation before describing an interface. If the desired simpler contract requires runtime changes, explain the limitation briefly and track the proposed change in an issue when authorized.
* **Stay within the reader's task.** Include only what a reader outside the project needs to complete the guide's task. Leave out internal limitations, roadmap issues, and evaluator or runtime internals; put them in references or issues instead.
* **Use consistent, human-readable terminology.** Use established NIKA terms, including “harness” for agent harnesses, and readable names such as “LangGraph.” Link relevant research when it helps readers understand the approach.

For example, “To investigate incidents with your own agent, implement the diagnosis contract” makes the purpose clear before the instruction.

Avoid notes like this one, taken from an earlier integration guide. It refers to internal evaluator behavior, an open issue, and a full contract that the reader does not need to finish the task:

```markdown
The complete NOOA agent also records steps and tool calls for the evaluator and honors the step limit.

Inheriting from LangGraph is a current limitation; a framework-independent base class is tracked in #172. The integration reference describes the full contract.
```

## Review

Read the draft as someone trying to complete the task: is the purpose clear, is the next action easy to find, and does each paragraph or snippet earn its place? Check links, defaults, and examples against their sources. Apply the repository's verification rules when executable behavior or examples change.
