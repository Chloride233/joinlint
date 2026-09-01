# JoinLint project rules

## Purpose

JoinLint is a local, read-only Join Safety MCP for AI SQL agents. It supplies
evidence-backed relationships and join-risk findings; it never executes SQL or
claims that a supported join proves business meaning.

## Run and verify

- Install development dependencies with `python -m pip install -e '.[dev]'`.
- Use a separate local evaluation environment with `.[dev,eval]` only. Keep
  `remote` dependencies in the workflow image or another environment; do not
  add them to the local mock-evaluation environment.
- Run tests with `python -m pytest -q`.
- Run lint with `python -m ruff check src tests benchmarks scripts`.
- Start the current STDIO server with
  `joinlint serve-mcp --project <root> --source <relative.sqlite>`.

## Stack and layout

- Python 3.12, Pydantic, Typer, FastMCP, PyYAML, and RFC 8785.
- Runtime code lives in `src/joinlint/`; tests live in `tests/`.
- Product and evaluation designs live in `docs/superpowers/specs/`.
- Deterministic regression and Agent evaluation code lives in `benchmarks/`.

## Current contract

- The strict Developer Preview MCP exposes exactly `get_join_plan` and
  `validate_sql`.
- SQLite is the current evaluated strict runtime. Sources may be explicit or
  found by bounded project discovery; the runtime uses declared foreign keys
  and reviewed curated relationships, never inferred candidates.
- The manual v0.1 governance CLI remains separate from the strict MCP runtime.
  Semantic intent inference, SQL execution, and answer validation are out of
  scope.
- Keep runtime claims separate from pending roadmap and evaluation claims.
- Preserve read-only source handling, sanitized evidence, deterministic output,
  and fail-closed behavior.

## Evaluation failure discipline

- Read `docs/evaluation-ledger.md` before retrying an experiment. Classify a
  result as experiment outcome, infrastructure failure, admission block, or
  tooling/UI failure.
- A recurring failure keeps one `.learnings/ERRORS.md` `Pattern-Key`, reopens
  on recurrence, and requires a deterministic regression guard before retry.
- Build frozen Pilot assets with `benchmarks.formal_eval.release_archive`, then
  verify the downloaded Draft Release asset before dispatch.
- Define reservation CLI admission in the campaign policy table; the workflow
  parity test must pass for every enabled mode.
- Keep raw private logs ignored. Commit or upload only sanitized artifacts that
  pass the pinned public-artifact verifier.
