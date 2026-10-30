<!--
Select with ?template=submission (used by result submissions, issue #118).
All other PRs should start from the blank template.
-->
# Result submission — `<agent-name>`

**Result run:** `results/<agent-name>/` · **Model:** `<model>` · **Framework:** `<framework>`

## Submission checklist (author)

- [ ] PR adds **only** `results/<agent-name>/` (results + `agent.yaml`)
- [ ] `agent.yaml` is complete and honest (see `docs/SUBMITTING.md`)
- [ ] `python -m bench verify results/<agent-name>` exits 0 locally
- [ ] Answers are the agent's raw output — no post-editing, no hand-edited scores
- [ ] If `open-source: no`, the `command` field describes how to reproduce the run

## Reviewer checklist

- [ ] `bench verify` green in CI (re-grades every stored answer)
- [ ] Folder name matches `agent.yaml` `name`
- [ ] No other run folders touched
- [ ] Contact listed for follow-up questions

## Notes

Anything unusual about the run (partial corpus, timeouts, retries) goes here.
