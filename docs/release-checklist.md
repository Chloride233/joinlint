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
- Verify MCP exposes only `get_data_model`, `find_join_path`, and `validate_join`.
- Verify the real STDIO protocol regression in `tests/test_mcp.py` passes current,
  stale-source, stale-model, bounded-output, and no-mutation cases.
- Verify generated artifacts and `.joinlint/state/` are not staged, while config, model, and baseline are reviewable.
- Install the downstream evaluation environment with
  `python -m pip install -e '.[dev,eval]'` outside normal CI.
- Run `python -m pytest tests/test_agent_join_selection.py tests/test_agent_join_scorers.py tests/test_agent_join_arm_isolation.py tests/test_agent_join_reporting.py -q`
  and verify the summary contains no skips.
- Run `python -m benchmarks.agent_join.joinlint_eval dry-run --work-dir benchmarks/agent_join/.work --log-dir benchmarks/agent_join/logs/dry-run`
  and verify all 200 planned samples have complete scorer artifacts.
- Verify normal CI installs only `dev` and never calls DeepSeek; do not run the
  paid Spider batch without the separate approval recorded after all free gates.
