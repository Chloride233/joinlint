# JoinLint

**Stop AI agents from guessing database joins.**

JoinLint Stage 1 is a local, read-only **AI SQL Join Safety MCP Developer
Preview**. It produces evidence-backed physical Join Proofs and validates the
join graph in final SQL before a separate database tool executes it.

> **JoinLint proof != query correctness.**

JoinLint validates physical join endpoints, exact relationship evidence,
cardinality, grain, fan-out, and—when supplied—the binding between SQL and one
Join Proof. It does not validate selected columns, filters, aggregations,
metric definitions, business semantics, or answer correctness.

## Stage 1 scope

The default MCP runtime:

- supports SQLite only;
- uses current, exact `declared` foreign keys and matching read-only `curated`
  relationships from an existing JoinLint model;
- never auto-uses inferred or legacy `strong` candidates;
- exposes exactly `get_join_plan` and `validate_sql` over local STDIO;
- never provides schema lookup or SQL execution;
- never sends user SQL to SQLite for execution;
- writes no project files and stores only sanitized evidence/proofs in a
  private user cache.

Parquet, DuckDB, remote databases, cross-source relationships, arbitrary SQL
execution, inferred relationships, metric semantics, and answer validation are
not supported by the Stage 1 MCP.

## Quick start

Install the Developer Preview:

```bash
python -m pip install -e '.[dev]'
```

Start it against one trusted project-relative SQLite source. No `joinlint init`
or scan is required for declared foreign keys:

```bash
joinlint serve-mcp --project /trusted/project --source data/app.sqlite
```

When `--source` is omitted, JoinLint performs bounded discovery at the project
root and under `data/` or `datasets/`. An explicit source always overrides
auto-discovery. Use `--no-auto` to require explicit sources.

JoinLint is designed to run beside a third-party database MCP:

```text
database MCP: inspect schema
JoinLint: get_join_plan(entity_refs, start_ref, expected_grain_ref)
agent: generate final SQL from the proof
JoinLint: validate_sql(sql, plan_id)
database MCP: execute only after validation passes
```

`ref` values such as `orders` and `manager` are request-local instance names.
They are not entity identity and are not reused across requests.

### MCP configuration and Harness

Copy the relevant template and replace the project/source paths:

- [Codex configuration](examples/mcp/codex-config.toml)
- [Claude Code configuration](examples/mcp/claude-code.json)
- [Cursor compatibility smoke configuration](examples/mcp/cursor.json)
- [Agent Harness instructions](examples/harness/joinlint-agent-instructions.md)

Cursor is compatibility-smoke-only for Stage 1. Codex CLI and Claude Code are
the supported formal-evaluation hosts. Configure your database MCP separately;
the examples deliberately do not install or authorize one.

## MCP contract

The response envelope is MCP schema v3. Join Proof objects remain immutable
schema v2 objects, so this usability revision does not change relationship,
evidence, or plan identity.

`get_join_plan` accepts 2–8 request-local entity instances and returns at most
four hops of current, exact, authorized physical relationships. If no safe
path exists it returns `inconclusive`, never uncertain predicates. It
distinguishes a missing authorized path (`NO_VERIFIED_PATH`) from a request
that contains references outside its dominant connected component
(`UNCONNECTED_ENTITY_REF`), an unsafe requested grain
(`GRAIN_INCOMPATIBLE`), and compound fan-out (`COMPOUND_FANOUT`) so an Agent
can stop instead of retrying equivalent plans. `UNCONNECTED_ENTITY_REF` is
retryable only after the Agent confirms the affected reference is not needed
and sends one changed request; it never authorizes an unchanged retry.
`GRAIN_INCOMPATIBLE` is corrective only: after checking the intended
pre-aggregation row grain, an Agent may send one request changing only
`expected_grain_ref`.

`validate_sql` parses one SQLite `SELECT`/`WITH`, normalizes aliases, CTEs,
subqueries, self joins, composite predicates, INNER/LEFT joins, and WHERE
equalities, then checks the complete graph. A supplied stale proof returns
`PROOF_STALE`; it is never silently downgraded to independent lint. Repair
never rewrites SQL. A proof-bound validation uses the immutable grain stored in
the proof; an explicit different `expected_grain_ref` is rejected. A planning
`GRAIN_INCOMPATIBLE` may therefore change the grain through one new plan, while
a validation `GRAIN_INCOMPATIBLE` requires revising the SQL against the same
proof.

Every response carries explicit `validated_scope` and `not_validated_scope`.
Every SQL validation attests `execution_count: 0`.

Every non-OK error and blocking finding also carries bounded recovery guidance:

- `retryable` means the tool may be called again only after applying
  `next_action`; it never authorizes retrying an unchanged request;
- `next_action` is one stable action such as `replan`, `fix_entity_refs`,
  `revise_sql`, `specify_source`, `change_expected_grain`, `reduce_request`,
  or `stop`;
- `affected_refs` and `blocking_relationship_ids` contain only sanitized,
  bounded identifiers;
- `stop` means the Agent must not execute the SQL or retry the same operation.

## Manual governance CLI

The existing human-governed CLI remains available for CSV and SQLite project
workflows. These commands do not add legacy tools to the default MCP server:

```bash
joinlint init --project .
joinlint source add sales data --project .
joinlint scan --project .
joinlint candidates --project . --json
joinlint accept <candidate-id> --project .
joinlint validate --project .
joinlint baseline update --project .
joinlint check --project .
```

## Evidence and development

Run the implementation and strict contract checks with:

```bash
python -m pytest -q
python -m ruff check src tests benchmarks scripts
```

The determinism acceptance gate starts two fresh server processes, compares
cold and warm caches over ten repetitions, permutes entity-ref order and
equivalent SQL aliases/equalities, and permits only documented observation
timestamps to differ.

See the [benchmarking guide](docs/benchmarking.md) for the separation between
deterministic capability gates and formal Agent product-effect evidence. In the
completed 20-pair opaque-relationship stress experiment, control completed
6/20 tasks and the JoinLint treatment completed 20/20 (absolute improvement
+0.70; exact two-sided McNemar p=0.0001220703125). This supports only the frozen
synthetic stress scope; it does not estimate natural SQL error rates, prove
business meaning, or show relationship discovery. See the
[effect results](docs/opaque-relationship-effect-results.md).
The [evaluation and failure ledger](docs/evaluation-ledger.md) keeps the full
positive, negative, incomplete, and local experiment history together with the
recurring CI and harness root causes; it is the starting point before any retry.

The formal workflow also has a pinned infrastructure-resume mode. It accepts
only the exact failed source run and server-reported artifact digest, preserves
every completed intention-to-treat row, and runs only the missing sample IDs.
The completed query-contract resume was capped at CNY 6.35 under the existing
CNY 70 public campaign; it is not a second draw from the frozen corpus. The
paid workflow was disabled after the completed run.

## License

MIT
