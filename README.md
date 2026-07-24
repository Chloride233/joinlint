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
