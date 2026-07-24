# JoinLint

**Stop AI agents from guessing database joins.**

JoinLint is a local, Git-native data relationship linter. It scans local CSV
directories and SQLite files, proposes deterministic single-column join
candidates, validates confirmed relationship cardinality and fan-out risk, and
keeps reviewed semantics in `.joinlint/model.yaml`.

## Quick start

```bash
python -m pip install -e '.[dev]'
joinlint init --project .
joinlint source add sales data --project .
joinlint scan --project .
joinlint candidates --project . --json
```

Confirm a candidate with `joinlint accept <candidate-id> --project .` after
reviewing or editing `model.yaml`, then run:

```bash
joinlint validate --project .
joinlint baseline update --project .
joinlint check --project .
```

`joinlint serve-mcp --project .` starts a local STDIO MCP server with three
read-only tools: `get_data_model`, `find_join_path`, and `validate_join`.

## Bundled SQLite smoke test

The repository includes a pinned MIT-licensed Chinook SQLite fixture. To scan
it through the public adapter:

```bash
work=$(mktemp -d)
mkdir "$work/data"
cp tests/fixtures/chinook/chinook.sqlite "$work/data/chinook.sqlite"
joinlint init --project "$work"
joinlint source add chinook data/chinook.sqlite --project "$work"
joinlint scan --project "$work"
```

`python -m pytest tests/test_end_to_end.py -q` also exercises the complete
workflow: scan, explicit model review, candidate confirmation, validation,
baseline update, clean-checkout `check`, and static report generation.

## Scope

- Local CSV directories and SQLite files on macOS and Linux.
- Deterministic exact scans with no LLM or network requirement.
- Git-tracked configuration, confirmed model, and sanitized baseline.
- Candidate evidence, confirmed join validation, drift checks, static reports,
  and bounded local MCP context.

Parquet, DuckDB, remote databases, arbitrary SQL execution, automatic
relationship confirmation, composite-key inference, Web UI, and remote MCP are
not supported in v0.

See the [v0 design](docs/superpowers/specs/2026-07-23-joinlint-v0-design.md),
[benchmarking guide](docs/benchmarking.md), and
[release checklist](docs/release-checklist.md).

## License

MIT
