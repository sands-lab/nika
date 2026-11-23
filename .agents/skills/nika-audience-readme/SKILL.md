---
name: nika-audience-readme
description: Write a README or guide about a NIKA artifact (a dataset, a tool, a result) for readers who are not network or systems engineers, such as data scientists or industry partners, with figures, a verified worked example, and an independent review. Use with nika-docs when the audience is outside the NIKA developer community.
---

# README for a non-engineering audience

Use this skill when the reader wants to *use* something NIKA produced, not to develop NIKA: for example a dataset for model training, a dashboard, or a benchmark result. It complements the [NIKA documentation skill](../nika-docs/SKILL.md), whose writing principles still apply. The reference result is [`scripts/offline_recording/README.md`](../../../scripts/offline_recording/README.md).

## 1. Fix the reader and the question

Before writing, write down in one line each:

- **Who reads it** and what they already know (for example: "data scientists; know pandas and ML evaluation; do not know routing protocols").
- **The question the page answers.** For an artifact this is usually: how was it built, what can I do with it, and how?
- **What the reader does next** after reading (load the data, run the tool, score a model).

Every section must serve that question. Move everything else to a final section for producers or maintainers, after a `---` divider.

## 2. Use the reader's words

- Name things the way the audience does: "dataset", "task", "labels", "columns"; not internal names such as "bundle", "trial", "session" or module names. Keep NIKA glossary terms to the essential few (task, network, fault) and define each at first use.
- Explain domain content at the level of a practitioner of that domain, not an implementer. For network data, describe evidence as an IP network engineer would ("routing table", "ARP table", "failed pings"), never as the code that collects it.
- Prefer plain descriptions with the technology in parentheses: "switches programmed by a central controller (OpenFlow, ONOS)".
- Never let identifiers, file names or column values reveal answers (for example, task IDs that contain the fault name).

## 3. Structure

The order that worked:

1. **Promise** (one paragraph): what the artifact lets the reader do, and what one unit of it is.
2. **What the reader predicts or gets**, as a short numbered list.
3. **Overview figure**: source → what is captured → what the reader receives.
4. **What is in it**: one table of the kinds of content, with concrete examples per row, plus one sentence on how the parts relate.
5. **How it was produced**: a time diagram, followed by at most three bullets about what the reader must know (for example which parts are model input, and what each timestamp means).
6. **Layout**: a short folder tree with one-line comments; say what must stay out of model input.
7. **Use it**: one tested code snippet, the column meanings, the label convention for the negative or healthy class, and one `[!IMPORTANT]` callout for the main pitfall (for example size versus context windows).
8. **Worked example**: one real item, with a figure and two or three short excerpts that show the answer from different sides.
9. **Catalog**: a table of the items in the sample, plain descriptions first.
10. `---` then **Produce more**: numbered steps with exact commands.

## 4. Figures

- Draw concept figures as slides with `python-pptx` and render them with LibreOffice (`soffice --headless --convert-to pdf`, then `pdftoppm -png -r 150`). Commit both the `.pptx` source and the PNG, plus the script that draws them, so others can edit them. See [`make_figures.py`](../../../scripts/offline_recording/figures/make_figures.py) for the helpers and palette.
- Use one palette across figures: green for healthy, red for fault, amber for alerts and tests, blue for state, violet for monitoring. Use a 16:9 slide with a large title, a one-line subtitle, and short labels.
- Generate per-item figures (such as network diagrams) with a script that has PEP 723 inline dependencies, so it needs no system packages. Derive the layout from the data's structure, not from item names, and never highlight the answer.
- Look at every rendered image before using it. Fix clipped text, overlaps and stray shadows; then look again.
- Write descriptive alt text. Do not repeat in prose what the figure already shows.

## 5. Verify every claim

- Run the code snippet against the real data.
- Check every number, time and file name in the text against the data, and give ranges as measured. Do not round them into a stronger claim.
- Check the worked example's evidence one source at a time. If a claim mixes sources, for example web checks described as monitoring, fix the claim.
- Avoid absolute claims ("everything an operations team could see", "nothing wrong") that the data does not prove; say what was done ("without an injected fault").

## 6. Independent review

When the draft is complete, ask a reviewer that did not write it, for example the NIKA-dev Letta agent, for a prioritized list of concrete edits. Send:

- the reader and question from step 1 and the owner's constraints (terminology, level of detail, visual quality);
- a short description of every figure, since the reviewer may not see images;
- the full text, with code blocks and tables intact. Condensed text produces false findings about formatting.

Apply substantive findings: accuracy, overclaims, missing conventions, and the evaluation semantics a data scientist would get wrong. Re-check the edited claims against the data. Update the figures when wording changes there too.

## Checklist

- [ ] Reader, question and next action written down
- [ ] No internal terms or answer-revealing identifiers in reader-facing text
- [ ] Overview figure, time diagram, and per-item figures rendered and inspected
- [ ] Snippet executed; numbers and excerpts verified against data
- [ ] Negative or healthy-class label convention and main pitfall stated
- [ ] Producer instructions after a divider, with exact commands
- [ ] Independent review done and findings applied
