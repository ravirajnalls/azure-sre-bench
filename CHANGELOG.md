# Changelog

All notable changes to Azure SRE Bench are documented here. The format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
are cut as git tags (`v<major>.<minor>`) with a frozen release manifest in
`releases/` (issue #116).

## [0.1.0] - 2026-10-30

First versioned release: the phase-2 corpus and integrity work, frozen as
`releases/v0.1.json` (40 tasks, `grader_version` 1, result schema 1).

### Added

- **Corpus — 40 tasks.** 32 diagnostic tasks across aks-networking,
  aks-identity, aks-ops, aks-workloads, app-service, monitoring, key-vault,
  identity, cost, networking, and ado-pipelines, all rubric-audited
  (specific decisive values, distractor groups, no generic short tokens);
  plus 8 hard tasks (#102–#109) with multi-fixture causal chains, red
  herrings, and arithmetically consistent numeric fixtures.
- **Scoring integrity.** Volume caps on answers (600-char root-cause/fix,
  8×300 evidence, per-task override); evidence demoted out of root-cause
  matching; distractor groups failing with `named_distractor`; a global
  safety-rules engine with negation guard; three adversarial probe agents
  (`keyword_soup`, `fixture_dump`, `minimal_dump`) gated in CI with an empty
  `tests/known_gameable.txt` (#92–#94, #113).
- **Grader/rubric versioning.** `GRADER_VERSION`, `RESULT_SCHEMA_VERSION`,
  and `rubric_sha256` on every result record; renderers mark mismatched
  records `stale` or `legacy` (#95).
- **Difficulty.** Evidence-count rubric defined in AGENTS.md;
  `solution.json` carries `evidence_files`; validator warns on
  difficulty/evidence disagreement (#101).
- **Remediation track.** `kind: remediation` tasks grade `patched_files`
  against per-check assertions (`equals`/`exists`/`absent`/`gte`/`lte`) in
  isolated fixture copies (#110).
- **Simulated CLIs.** `kubectl`/`az` shims backed by per-task
  `_commands.yaml` canned output, placed first on the agent's PATH; command
  traces feed efficiency metrics (#111).
- **Efficiency and cost metrics.** Files read, commands run, wall time, and
  optional `usage` (input/output tokens, cost) on results; leaderboard and
  site columns (#112).
- **Community platform.** Canary string enforced by the validator; Apache-2.0
  code / CC-BY-4.0 data licensing; held-out split mechanics with age-encrypted
  hidden files; JSONL dataset export with dataset card; `bench verify`
  submission flow with CI enforcement; leaderboard agent metadata and
  filters; structured `bench new` contribution scaffolding (#114–#120).
- **Release infrastructure.** `BENCH_VERSION`, frozen release manifests,
  `bench run --release v0.1`, and a tag-push release workflow building the
  tarball (#116).
- **Agent ecosystem.** Azure AI Foundry agent, LangChain adapter, MCP server
  + client runner, closed-agent manual protocol, and the baseline-sweep
  harness (#121–#125; credential-dependent real runs remain documented gaps).
- **Live mode (spike).** ADR and a kind-based faulted scenario exercised by
  CI where docker exists (#126).

[0.1.0]: https://github.com/ravirajnalls/azure-sre-bench/releases/tag/v0.1
