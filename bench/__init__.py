"""Azure SRE Bench: an open benchmark for AI SRE agents on Azure/AKS incidents.

Versioning surfaces (issue #95):

- `GRADER_VERSION` — bumped when grading semantics change (matching rules,
  length caps, distractor handling, judge mode). Every result record carries
  the version that produced its `score`/`passed`.
- `RESULT_SCHEMA_VERSION` — bumped when the result-record shape changes
  incompatibly. The schema (version 1) is documented in `docs/SUBMITTING.md`.

The result record, as written by `bench run` and the manual protocol:

    task, schema_version, grader_version, rubric_sha256, agent   identity
    score, passed, detail                                        grading
    elapsed_s, answer, stderr_tail, timed_out, returncode        run context
    commands, efficiency (optional)                              sim-CLI trace
    usage (optional)                                             token/cost telemetry
    judge (optional)                                             reserved for LLM-judge grading (issue #100)
    mode ("manual", manual protocol only)

Renderers rank only records whose `grader_version` matches the current grader
and whose `rubric_sha256` matches the current rubric; every other record
renders `stale` (graded under a different version or an older rubric) or
`legacy` (written before these fields existed).
"""

__version__ = "0.1.0"

#: Grading-semantics version (issue #95). Bump on any change to how answers
#: are scored: matching rules, length caps, distractor handling, judge mode.
GRADER_VERSION = 1

#: Result-record schema version (issue #95). Bump on an incompatible change to
#: the record shape `bench run` and the manual protocol write.
RESULT_SCHEMA_VERSION = 1
