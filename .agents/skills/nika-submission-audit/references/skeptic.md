# Skeptic prompt

Run in a fresh context, one finding at a time, with the finding JSON, the cited steps (`audit.py packet <trial> --full-step N`), and the scenario brief.

---

A reviewer claims the benchmark, its harness, or a leaderboard submission is at fault. Your job is to refute the claim if you can. Audit findings are often wrong expectations, intended behaviour, or observations from the wrong vantage point; they are also often real.

{finding}

Check, in order:

1. Does the cited excerpt say what the claim says it says?
2. Is the behaviour by design? Read the scenario brief and the design intent.
3. Is it the agent's own doing (its own command echoed back, state it changed)?
4. Would the same text appear on a fault-free run of the same scenario? If it would, it cannot reveal the fault.
5. Is a simpler known issue the real cause?

Answer with JSON:

```json
{"verdict": "upheld | refuted | by_design | undetermined", "reason": "<one paragraph>", "check_that_holds": "<the observation that settles it>"}
```

Do not refute without naming an observation that holds. Default to `undetermined`, not `refuted`, when the trace cannot settle it.
