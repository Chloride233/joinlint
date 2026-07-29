# JoinLint v0 Release Checklist

- Run `python -m pytest -q`.
- Run `python -m ruff check src tests benchmarks scripts`.
- Run `python benchmarks/joinsafety/generate.py --output /tmp/joinlint-benchmark --seed 20260723`.
- Run `python -m pytest tests/test_benchmark.py tests/test_scanner.py tests/test_end_to_end.py -q`.
- Verify `shasum -a 256 tests/fixtures/chinook/chinook.sqlite` matches the fixture README.
- Run `python scripts/run_benchmark.py --rows 1000000 --output performance.json` on Linux.
- Verify `performance.json` reports at most 60 seconds and less than 1 GiB peak RSS.
- Verify the README contains no unmeasured generalization or improvement claim.
- Verify `joinlint scan`, `candidates`, `validate`, `baseline update`, and `check` on the bundled smoke fixture.
- Verify the default MCP exposes only `get_join_plan` and `validate_sql`.
- Verify the real STDIO protocol regression in `tests/test_mcp.py` passes the
  two-call proof flow, stale-proof refusal, sanitized errors, credential
  scrubbing, `execution_count: 0`, and no-project-write cases.
- Verify `tests/test_runtime_determinism.py` passes across two fresh processes,
  cold/warm caches, ten repeats, request-ref permutations, and equivalent SQL.
- Verify README and tool descriptions state `JoinLint proof != query correctness`
  and label Stage 1 as a Developer Preview.
- Verify generated artifacts and `.joinlint/state/` are not staged, while config, model, and baseline are reviewable.
- Install the downstream evaluation environment with
  `python -m pip install -e '.[dev,eval]'` outside normal CI.
- Run `python -m pytest tests/test_agent_join_selection.py tests/test_agent_join_scorers.py tests/test_agent_join_arm_isolation.py tests/test_agent_join_reporting.py -q`
  and verify the summary contains no skips.
- Run `python -m benchmarks.agent_join.joinlint_eval dry-run --work-dir benchmarks/agent_join/.work --log-dir benchmarks/agent_join/logs/dry-run`
  and verify all 200 planned samples have complete scorer artifacts.
- Verify normal CI installs only `dev` and never calls DeepSeek; do not run the
  paid Spider batch without the separate approval recorded after all free gates.

## Completed-product formal evaluation v2

- Install `.[dev,eval]` and run `python -m pytest tests/test_formal_eval_*.py -q`
  with no failures.
- Run `python -m benchmarks.formal_eval.cli fake-model --output /tmp/joinlint-formal-smoke`
  and verify both Markdown and machine-readable JSON reports are rebuildable.
- Run `python -m benchmarks.formal_eval.cli inspect-smoke --project tests/fixtures/chinook --output /tmp/joinlint-inspect-smoke`
  and require `passed: true` while retaining its synthetic, non-evidentiary label.
- Verify fake-model output is labelled synthetic and is not used in any product
  or marketing claim.
- Freeze DeepSeek V4 Pro and V4 Flash returned identities, non-thinking mode,
  official CNY cache-hit/cache-miss/output prices, pinned Codex CLI and Claude
  Code versions, JoinLint commit, Harness and policy versions, image digest,
  seed, sample size, and dataset release in schema-v2 preregistration. Disclose
  that this is one provider family with two tiers, not cross-family replication.
- Verify the input lock covers every sealed corpus, SQL label, task payload,
  database, independent pilot row, and raw deterministic performance fixture.
- Verify preflight reports at least 12 confirmatory databases, 60 natural tasks,
  20 diagnostic tasks, no ambiguous confirmatory truth, no exact duplicate, and
  no database variant crossing a split.
- Verify relationship evidence counts at least 12 declared visible/ablated
  variant pairs and 12 independently annotated absent-FK databases without
  double-counting physical variants; adversarial trap counts must be balanced.
- Verify the Stage 1 MCP exposes exactly `get_join_plan` and `validate_sql` and
  the runtime-contract gate rejects any additional tool.
- Confirm the independent pilot power plan is frozen before any confirmatory
  output is viewed; stop before dispatch unless the frozen manifest and exact
  run plan meet the pilot requirement and the 1,440-run minimum.
- Verify the deterministic evidence bundle matches the raw-suite digest and the
  evaluation lineage; do not accept hand-authored relationship, SQL, or
  performance verdict files.
- Verify every strict `validate_sql` result carries the runtime audit attestation
  `execution_count: 0`; missing or nonzero values must fail the SQL gate.
- Run the protected `formal-evaluation` GitHub Actions workflow only after its
  explicit paid-run approval; do not use a local machine as a runner.
- Verify the diagnostic canary remains operational evidence only. Before a full
  Pilot, require the bound four-cell calibration attestation with the exact
  Pilot limits; neither diagnostic output enters the confirmatory report.
- Verify sanitized exports reject unexpected returned model IDs, duplicate run
  identities, missing scorer artifacts, raw questions, schema text, gold SQL,
  database paths, transcripts, and common credential prefixes.
- Blind-review the exact run-plan sample of at least 80 runs or 10 percent of
  all runs, whichever is larger. Require arm-removal attestation, per-output
  digests, reviewer digests, and at least 95 percent scorer agreement.
- Verify the report includes absolute counts, the full failure funnel, all
  failed and no-improvement databases, model/host cells, bootstrap intervals,
  token use, latency, cost, and all failed publication gates.
- Publish an improvement claim only when every preregistered deterministic,
  effect, non-inferiority, per-cell harm, compliance, bypass, and tool-error gate
  passes; otherwise publish observed results without improvement language.
