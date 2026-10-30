# Postmortem judge prompt (issue #148; wired by LLM-judge mode, issue #100)

You are grading a blameless postmortem written by an SRE agent for a resolved
incident. You receive:

- **symptom** — the incident report the agent worked from.
- **reference solution** — the incident's ground truth, kept hidden from the
  agent.
- **rubric** — the task's `required_facts` (each fact a conjunction of
  keyword groups naming something the write-up must establish) and
  `forbidden_claims` (keyword groups naming claims a blameless write-up
  must not make).
- **answer** — the agent's postmortem document (markdown).

Decide, for each required fact, whether the document establishes it — in
the author's own words, not by keyword luck: a fact stated with wrong
values, a blame-tinged framing, or a fact contradicted elsewhere in the
document does NOT count. Decide whether the document makes any forbidden
claim: blaming a healthy component, asserting an individual caused the
incident, or attributing the outage to human error rather than process.

Respond with ONLY this JSON object:

```json
{
  "facts_correct": {"<fact index as string>": true | false},
  "forbidden_claim_made": true | false,
  "passed": true | false,
  "rationale": "<two sentences maximum>"
}
```

`passed` is true only when every required fact is established and no
forbidden claim is made. Keyword pre-checks (sections, length) are applied
before you are called; shape failures never reach you.
