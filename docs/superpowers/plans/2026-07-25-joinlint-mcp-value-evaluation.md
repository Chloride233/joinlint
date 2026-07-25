# JoinLint MCP Value Evaluation Implementation Plan

> **For agentic workers:** Execute this plan in the current session by default, task by task. Use checkbox (`- [ ]`) syntax for tracking. Use subagents only when the user explicitly requests delegation or parallel agent work.

**Goal:** Build, validate, freeze, and run an exploratory Spider pilot with 192 primary runs plus eight diagnostics that measures whether DeepSeek uses JoinLint MCP relationship context and whether that context reduces wrong joins.

**Architecture:** Keep evaluation-only code under `benchmarks/agent_join` and leave the JoinLint runtime untouched. Inspect AI runs one randomized dataset whose samples carry arm metadata; a condition-aware solver exposes no MCP, inline oracle context, oracle MCP, or blind-reviewed JoinLint MCP. Deterministic SQL, trace, execution, and reporting modules score the same append-only logs without asking another model to judge correctness.

**Tech Stack:** Python 3.12, JoinLint v0, Inspect AI 0.3.249, DeepSeek OpenAI-compatible API, MCP 1.28.1, OpenAI SDK 2.48.0, SQLGlot 30.13.0, Pydantic 2, SQLite, pytest, Ruff.

## Global Constraints

- The primary Spider pilot contains exactly 16 tasks from exactly four databases, four tasks per database, four arms, and three repetitions: 192 outcome runs. Add four zero-configuration MCP diagnostics and four local safety diagnostics for exactly 200 paid model runs; diagnostics never enter A/B/C/D outcome effects.
- Use seed `20260725` for deterministic database selection, task selection, and balanced sample ordering.
- Arm A receives an FK-ablated schema and no MCP; arm B receives the same schema plus the database-level oracle graph and validation results inline; arm C receives that oracle content through JoinLint MCP; arm D receives only the blind-reviewed JoinLint graph through MCP.
- The pilot model is `openai-api/deepseek/deepseek-v4-flash` with thinking disabled, temperature `0`, maximum output 4,096 tokens, at most four MCP calls, and a 120-second per-sample wall-clock limit.
- Read the API key only from `DEEPSEEK_API_KEY` and never write its value to source, manifests, logs, reports, or Git history. Pin the public endpoint with `DEEPSEEK_BASE_URL=https://api.deepseek.com`.
- Never give the Agent filesystem, database, shell, or SQL-execution tools. Only arms C and D receive JoinLint's three read-only MCP tools plus the structured submit tool.
- A model timeout, refusal, malformed answer, parser failure, MCP non-use, or tool application error remains in the intention-to-treat denominator and is never retried as a sample.
- Allow at most two provider retries for transient HTTP 429, 500, 502, 503, or 504 responses. Never retry a valid but incorrect model answer.
- Execute only one parsed SQLite `SELECT` statement or CTE followed by `SELECT`; reject all DDL, DML, `ATTACH`, `PRAGMA`, multi-statement, and non-query output before opening a disposable read-only snapshot.
- Eligibility may use pinned Spider metadata and gold SQL, but it must never inspect JoinLint output. A task stays selected when JoinLint misses its required edge.
- The relationship reviewer may see table and column names, JoinLint candidate evidence, and validation findings, but never questions, gold SQL, Spider foreign keys, oracle models, or Agent outputs.
- The Spider pilot is exploratory. Do not use its effect size as a marketing claim. A BIRD formal evaluation requires a separate preregistration and plan.
- Do not start the paid DeepSeek batch without explicit user approval after all local gates pass and the relationship model is frozen.
- Keep normal CI on the existing `dev` extra. Every evaluation-only test must call `pytest.importorskip("inspect_ai")` and `pytest.importorskip("sqlglot")` before importing `benchmarks.agent_join` modules that require them; the evaluation release checklist installs `dev,eval` and must show zero skips for the four evaluation test files.

---

## File Structure

### Create

- `benchmarks/agent_join/__init__.py`: package marker only.
- `benchmarks/agent_join/contracts.py`: strict manifest, task, edge, score, trace, and report contracts.
- `benchmarks/agent_join/sql_edges.py`: SQL extraction, qualification, read-only validation, and join-graph comparison.
- `benchmarks/agent_join/prepare_spider.py`: archive verification, deterministic eligibility, schema rendering, and task lock generation.
- `benchmarks/agent_join/projects.py`: isolated oracle and review project creation, candidate decision application, model freezing, and evidence regeneration.
- `benchmarks/agent_join/trace.py`: MCP message parsing and behavior metrics.
- `benchmarks/agent_join/execution.py`: safe SQLite execution and official Spider denotation equivalence adapter.
- `benchmarks/agent_join/vendor/spider_result_eq.py`: Apache-2.0 attributed copy of the fixed official equivalence functions from commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c`.
- `benchmarks/agent_join/vendor/README.md`: provenance, commit, source URL, and unchanged-function statement.
- `benchmarks/agent_join/scorers.py`: Inspect AI scorers for join outcome, trace behavior, and execution.
- `benchmarks/agent_join/joinlint_eval.py`: condition-aware Inspect solver, structured submit tool, sample construction, model configuration, and CLI.
- `benchmarks/agent_join/report.py`: sanitized row export, hierarchical paired bootstrap, gate checks, JSON, and Markdown output.
- `benchmarks/agent_join/preregistration.yaml`: frozen pilot constants and error policy.
- `benchmarks/agent_join/README.md`: acquisition, blind review, free preflight, paid run, reporting, and cleanup commands.
- `benchmarks/agent_join/prompts/base.txt`: shared SQL task instruction.
- `benchmarks/agent_join/prompts/mcp-integration.txt`: exact MCP integration instruction approved in the spec.
- `benchmarks/agent_join/tasks/spider-pilot-manifest.json`: selected IDs and public hashes, generated before the paid run.
- `benchmarks/agent_join/tasks/spider-pilot-hashes.json`: archive, source-file, database, prompt, model, and code hashes.
- `benchmarks/agent_join/models/oracle/{db_id}.yaml`: database-level oracle models.
- `benchmarks/agent_join/models/joinlint/{db_id}.yaml`: human-reviewed JoinLint models.
- `benchmarks/agent_join/results/spider-pilot-rows.json`: strict sanitized per-run records from which reports can be regenerated without raw Inspect logs.
- `benchmarks/agent_join/fixtures/mcp-safety/manifest.json`: four behavior-only safety cases.
- `tests/test_agent_join_selection.py`: deterministic Spider selection and isolation tests.
- `tests/test_agent_join_scorers.py`: SQL edge, invalid output, execution, and graph-score tests.
- `tests/test_agent_join_arm_isolation.py`: arm input, MCP availability, model configuration, and fake-model dry-run tests.
- `tests/test_agent_join_reporting.py`: trace metrics, paired bootstrap, gate, and secret-redaction tests.
- `tests/agent_join_helpers.py`: shared two-table SQLite, miniature Spider, oracle schema, frozen-input, and synthetic-result builders for evaluation tests.

### Modify

- `pyproject.toml`: add the pinned `eval` optional dependency group.
- `uv.lock`: resolve the evaluation dependency graph.
- `.gitignore`: exclude acquired data, review workspaces, Inspect logs, raw exports, and paid-run scratch state.
- `docs/benchmarking.md`: link the exploratory MCP evaluation and distinguish it from JoinSafetyBench.
- `docs/release-checklist.md`: add only free evaluation harness tests; never put paid model calls in CI.

### Do not modify

- `src/joinlint/**`: the evaluation consumes the released v0 CLI and MCP contract without product changes.
- `.github/workflows/ci.yml`: the large `eval` extra and paid API run stay outside normal CI.

---

### Task 1: Establish the evaluation-only dependency and artifact boundary

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `.gitignore`
- Create: `benchmarks/agent_join/__init__.py`
- Create: `benchmarks/agent_join/preregistration.yaml`
- Create: `benchmarks/agent_join/prompts/base.txt`
- Create: `benchmarks/agent_join/prompts/mcp-integration.txt`
- Create: `benchmarks/agent_join/README.md`

**Interfaces:**
- Consumes: the approved design at `docs/superpowers/specs/2026-07-25-joinlint-mcp-value-evaluation-design.md`.
- Produces: pinned evaluation dependencies, immutable pilot constants, exact prompts, and ignored local artifact locations used by every later task.

- [ ] **Step 1: Add the pinned optional dependency group**

Add this sibling to `[project.optional-dependencies]`; do not add these packages to the base runtime or `dev` extra:

```toml
eval = [
  "inspect-ai==0.3.249",
  "mcp==1.28.1",
  "openai==2.48.0",
  "sqlglot==30.13.0",
]
```

- [ ] **Step 2: Lock and install the combined development environment**

Run:

```bash
uv lock
uv sync --extra dev --extra eval
```

Expected: `uv.lock` changes; `uv run python -c "import inspect_ai, openai, sqlglot; print(inspect_ai.__version__, openai.__version__, sqlglot.__version__)"` prints `0.3.249 2.48.0 30.13.0`.

- [ ] **Step 3: Isolate all untracked and sensitive artifacts**

Append exactly these patterns to `.gitignore`:

```gitignore

# Agent-join evaluation data, review state, and raw logs
benchmarks/agent_join/.work/
benchmarks/agent_join/logs/
benchmarks/agent_join/results/raw/
benchmarks/agent_join/review/
```

At the top of each new `tests/test_agent_join_*.py` file, before importing evaluation modules, use:

```python
import pytest

pytest.importorskip("inspect_ai")
pytest.importorskip("sqlglot")
```

This keeps the existing `pip install -e '.[dev]'` CI green while making `uv sync --extra dev --extra eval` mandatory for the evaluation gates.

- [ ] **Step 4: Write the preregistration constants**

Create `benchmarks/agent_join/preregistration.yaml` with:

```yaml
schema_version: 1
pilot:
  dataset: spider
  split: dev
  seed: 20260725
  database_count: 4
  tasks_per_database: 4
  arms: [A, B, C, D]
  repetitions: 3
model:
  id: openai-api/deepseek/deepseek-v4-flash
  base_url: https://api.deepseek.com
  thinking: disabled
  temperature: 0
  max_output_tokens: 4096
  max_mcp_calls: 4
  time_limit_seconds: 120
  transient_retries: 2
execution:
  dialect: sqlite
  statement_deadline_seconds: 5
  max_result_rows: 10000
report:
  bootstrap_draws: 10000
  bootstrap_seed: 20260725
  manual_review_size: 40
  pilot_artifact_completion_min: 0.95
  manual_scorer_agreement_min: 0.95
  formal_wrong_join_drop_min_points: 10
  formal_execution_drop_max_points: 5
pricing:
  observed_at: 2026-07-25
  input_cache_hit_per_million_usd: 0.0028
  input_cache_miss_per_million_usd: 0.14
  output_per_million_usd: 0.28
```

- [ ] **Step 5: Freeze the two prompt files**

Create `prompts/base.txt`:

```text
You generate one SQLite query for the supplied question and schema.
Return exactly one read-only SELECT statement using submit_sql.
Do not execute SQL or inspect database contents. Use only the question, schema, and any relationship context visible in this run.
When no relationship context is available, infer join conditions cautiously from the visible schema rather than assuming hidden metadata.
If the visible information is insufficient for a safe query, submit an empty SQL string and explain why in warning.
```

Create `prompts/mcp-integration.txt`:

```text
Before generating multi-table SQL, consult JoinLint for confirmed relationships and validate the selected join path. Base join conditions only on confirmed edges returned during this run. If validation returns a blocking finding, do not silently use that path.
```

- [ ] **Step 6: Document the local-only and paid-run boundaries**

In `benchmarks/agent_join/README.md`, include the exact official Spider data URL `https://drive.google.com/file/d/1403EGqzIDoHMdQF4c9Bkyl7dZLZ5Wt6J/view?usp=sharing`, the fixed evaluator source commit `e97acc546ecbee8fa27fa8dbf025ef61493a876c`, the four arms, and the rule that no command requiring `DEEPSEEK_API_KEY` runs until the final approval task.

- [ ] **Step 7: Verify and commit**

Run:

```bash
uv run python -c "import inspect_ai, openai, sqlglot; assert inspect_ai.__version__ == '0.3.249'; assert openai.__version__ == '2.48.0'; assert sqlglot.__version__ == '30.13.0'"
uv run python -m pytest -q
uv run python -m ruff check src tests benchmarks scripts
git diff --check
```

Expected: all existing tests pass, Ruff passes, and the diff check is silent.

```bash
git add pyproject.toml uv.lock .gitignore benchmarks/agent_join
git commit -m "build: add isolated agent evaluation dependencies"
```

---

### Task 2: Implement strict contracts and deterministic SQL join scoring

**Files:**
- Create: `benchmarks/agent_join/contracts.py`
- Create: `benchmarks/agent_join/sql_edges.py`
- Create: `tests/test_agent_join_scorers.py`
- Create: `tests/agent_join_helpers.py`

**Interfaces:**
- Consumes: SQL strings, a physical SQLite schema map, and one or more allowed edge graphs.
- Produces: `Edge`, `PilotSample`, `Submission`, `JoinScore`, `canonical_edge()`, `extract_submission()`, `extract_submitted_sql()`, `extract_join_edges()`, `validate_readonly_select()`, and `score_join_graph()`.

- [ ] **Step 1: Write failing edge and graph tests**

Create these initial tests in `tests/test_agent_join_scorers.py`:

```python
from benchmarks.agent_join.sql_edges import (
    extract_join_edges,
    score_join_graph,
    validate_readonly_select,
)


SCHEMA = {
    "customers": {"id": "INTEGER", "name": "TEXT"},
    "orders": {"id": "INTEGER", "customer_id": "INTEGER"},
}


def test_extracts_alias_and_where_join_as_the_same_edge() -> None:
    on_edges = extract_join_edges(
        "SELECT * FROM orders AS o JOIN customers AS c ON o.customer_id = c.id",
        SCHEMA,
    )
    where_edges = extract_join_edges(
        "SELECT * FROM orders o, customers c WHERE c.id = o.customer_id",
        SCHEMA,
    )
    expected = frozenset({("customers.id", "orders.customer_id")})
    assert on_edges == expected
    assert where_edges == expected


def test_cte_preserves_a_physical_join_inside_the_cte() -> None:
    edges = extract_join_edges(
        "WITH x AS (SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id) SELECT * FROM x",
        SCHEMA,
    )
    assert edges == frozenset({("customers.id", "orders.customer_id")})


def test_wrong_join_requires_no_extra_and_no_missing_edges() -> None:
    allowed = [frozenset({("customers.id", "orders.customer_id")})]
    assert score_join_graph(allowed[0], allowed).wrong_join is False
    assert score_join_graph(frozenset(), allowed).wrong_join is True
    extra = frozenset({("customers.id", "orders.customer_id"), ("customers.id", "orders.id")})
    assert score_join_graph(extra, allowed).wrong_join is True


def test_readonly_validator_rejects_mutation_and_multiple_statements() -> None:
    assert validate_readonly_select("SELECT * FROM orders").sql == "SELECT * FROM orders"
    for sql in (
        "DELETE FROM orders",
        "ATTACH DATABASE '/tmp/x' AS x",
        "PRAGMA writable_schema=ON",
        "SELECT 1; SELECT 2",
    ):
        assert validate_readonly_select(sql).error_code == "UNSAFE_SQL"
```

Create `tests/agent_join_helpers.py` with `build_orders_database(root: Path) -> Path`. It creates `customers(id INTEGER PRIMARY KEY, name TEXT)` and `orders(id INTEGER PRIMARY KEY, customer_id INTEGER, total REAL)` with two customers and three orders, commits, closes, and returns `root / "orders.sqlite"`. All later evaluation tests import this builder rather than writing their own database.

- [ ] **Step 2: Run the focused test and verify the missing-module failure**

Run: `uv run python -m pytest tests/test_agent_join_scorers.py -q`

Expected: collection fails because `benchmarks.agent_join.sql_edges` does not exist.

- [ ] **Step 3: Define the strict shared contracts**

Create `contracts.py` with strict Pydantic models and these exact public fields:

```python
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Arm = Literal["A", "B", "C", "D"]
Edge = tuple[str, str]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SelectedTask(StrictModel):
    task_id: str
    db_id: str
    question: str
    schema_text: str
    schema: dict[str, dict[str, str]]
    gold_sql: str
    allowed_graphs: list[list[Edge]]
    oracle_relationships: list[dict[str, object]]
    database_path: str


class PilotSample(StrictModel):
    suite: Literal["primary", "zero_config", "safety"]
    sample_id: str
    task_id: str
    db_id: str
    arm: Arm
    repetition: int = Field(ge=0, le=2)
    question: str
    schema_text: str
    schema: dict[str, dict[str, str]]
    gold_sql: str
    allowed_graphs: list[list[Edge]]
    oracle_relationships: list[dict[str, object]]
    oracle_validations: list[dict[str, object]]
    database_path: str
    mcp_project: str | None = None

    @model_validator(mode="after")
    def require_project_only_for_mcp_arms(self) -> "PilotSample":
        if (self.arm in {"C", "D"}) != (self.mcp_project is not None):
            raise ValueError("only MCP arms require mcp_project")
        return self


class JoinScore(StrictModel):
    wrong_join: bool
    precision: float
    recall: float
    f1: float
    predicted_edges: list[Edge]
    matched_graph: list[Edge]
    error_code: str | None = None


class ValidatedSQL(StrictModel):
    sql: str | None = None
    error_code: str | None = None

    @model_validator(mode="after")
    def require_exactly_one_result(self) -> "ValidatedSQL":
        if (self.sql is None) == (self.error_code is None):
            raise ValueError("exactly one of sql or error_code is required")
        return self


class Submission(StrictModel):
    sql: str
    warning: str
```

- [ ] **Step 4: Implement SQL extraction and graph comparison**

Create `sql_edges.py` using SQLGlot's SQLite parser and qualifier. The public functions must use these signatures and rules:

```python
def canonical_edge(left: str, right: str) -> Edge:
    return tuple(sorted((left, right), key=lambda value: value.encode("utf-8")))


def extract_submission(answer: str) -> Submission:
    document = json.loads(answer)
    if (
        set(document) != {"sql", "warning"}
        or not isinstance(document["sql"], str)
        or not isinstance(document["warning"], str)
    ):
        raise ValueError("submit_sql output must contain only string sql and warning")
    return Submission(sql=document["sql"].strip(), warning=document["warning"])


def extract_submitted_sql(answer: str) -> str:
    return extract_submission(answer).sql


def validate_readonly_select(sql: str) -> ValidatedSQL:
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except sqlglot.errors.ParseError:
        return ValidatedSQL(error_code="SQL_PARSE_ERROR")
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return ValidatedSQL(error_code="UNSAFE_SQL")
    forbidden = (
        exp.Alter,
        exp.Attach,
        exp.Command,
        exp.Copy,
        exp.Create,
        exp.Delete,
        exp.Drop,
        exp.Insert,
        exp.Into,
        exp.Merge,
        exp.Pragma,
        exp.Transaction,
        exp.Update,
    )
    if any(statements[0].find(node_type) is not None for node_type in forbidden):
        return ValidatedSQL(error_code="UNSAFE_SQL")
    return ValidatedSQL(sql=statements[0].sql(dialect="sqlite"))
```

For `extract_join_edges()`, call `qualify(expression, dialect="sqlite", schema=schema, quote_identifiers=False, validate_qualify_columns=True)`, walk every `exp.EQ`, and retain only predicates whose two children are qualified `exp.Column` values from different physical tables in `schema`. For `score_join_graph()`, compare against every allowed graph, choose the graph with highest F1 and then UTF-8 lexical order, and mark `wrong_join=True` whenever the selected graph has a false positive or false negative.

- [ ] **Step 5: Run focused tests and add invalid-submit coverage**

Add tests asserting JSON-only submit parsing, empty SQL, CTE qualification failure, unqualified ambiguous columns, and multiple allowed graphs. Run:

```bash
uv run python -m pytest tests/test_agent_join_scorers.py -q
uv run python -m ruff check benchmarks/agent_join tests/test_agent_join_scorers.py
```

Expected: all focused tests and Ruff pass.

- [ ] **Step 6: Commit**

```bash
git add benchmarks/agent_join/contracts.py benchmarks/agent_join/sql_edges.py tests/agent_join_helpers.py tests/test_agent_join_scorers.py
git commit -m "feat: add deterministic agent join scoring"
```

---

### Task 3: Select and lock an eligibility-only Spider pilot

**Files:**
- Create: `benchmarks/agent_join/prepare_spider.py`
- Modify: `tests/agent_join_helpers.py`
- Create: `tests/test_agent_join_selection.py`

**Interfaces:**
- Consumes: an official Spider archive containing `dev.json`, `tables.json`, and `database/{db_id}/{db_id}.sqlite`.
- Produces: `SelectedTask`, `find_spider_root()`, `select_pilot()`, `render_schema()`, `build_oracle_graph()`, a sealed local task file, a sanitized tracked manifest, and a complete hash lock.

- [ ] **Step 1: Write failing selection and ablation tests**

Extend `tests/agent_join_helpers.py` with `build_mini_spider(root: Path) -> Path`. It creates five database directories named `mini_0` through `mini_4`, each containing the same two-table SQLite structure, a `tables.json` entry with two single primary keys and the `orders.customer_id -> customers.id` foreign key, and four unique dev questions whose gold SQL uses that edge. It also appends one ineligible record for each excluded join shape. Build this tree in `tmp_path` and assert:

```python
def test_selection_is_seeded_balanced_and_independent_of_joinlint(tmp_path: Path) -> None:
    spider = build_mini_spider(tmp_path)
    first = select_pilot(spider, seed=20260725, database_count=4, tasks_per_database=4)
    second = select_pilot(spider, seed=20260725, database_count=4, tasks_per_database=4)
    assert first == second
    assert len(first) == 16
    assert set(Counter(task.db_id for task in first).values()) == {4}


def test_rendered_schema_contains_primary_keys_but_no_foreign_keys(tmp_path: Path) -> None:
    spider = build_mini_spider(tmp_path)
    task = select_pilot(spider, seed=20260725, database_count=4, tasks_per_database=4)[0]
    assert "PRIMARY KEY" in task.schema_text
    assert "FOREIGN KEY" not in task.schema_text
    assert "REFERENCES" not in task.schema_text
```

Also add one case each for self-join, composite join, non-equality join, more than three edges, missing declared FK, a dotted table name, and a joined table without exactly one primary key; assert all are ineligible before any JoinLint code is imported.

- [ ] **Step 2: Run and verify failure**

Run: `uv run python -m pytest tests/test_agent_join_selection.py -q`

Expected: FAIL because `prepare_spider.py` and its public functions do not exist.

- [ ] **Step 3: Implement deterministic archive discovery and hashing**

`find_spider_root(archive: Path, work_dir: Path) -> Path` must:

1. require a regular `.zip` file;
2. hash the archive in 1 MiB chunks;
3. reject absolute paths, `..`, symlinks, and members larger than 1 GiB before extraction;
4. extract under `work_dir/source` only;
5. require exactly one directory containing `dev.json`, `tables.json`, and `database`;
6. return that directory without printing questions or gold SQL.

Write hashes with RFC 8785 canonical JSON through the existing `canonical_json()` helper.

- [ ] **Step 4: Implement eligibility and seeded ranking**

Use this ranking function for both databases and task IDs:

```python
def seeded_rank(seed: int, *parts: str) -> bytes:
    payload = "\0".join((str(seed), *parts)).encode("utf-8")
    return hashlib.sha256(payload).digest()
```

Eligibility must parse gold SQL through `extract_join_edges()`, require one to three edges, require every edge in the database-level single-column declared FK graph, and require every joined table to have exactly one Spider primary key. Rank eligible database IDs, retain the first four with at least four tasks, then rank and retain four tasks within each database.

- [ ] **Step 5: Separate sealed inputs from tracked public locks**

Write exactly 16 full `SelectedTask` records to ignored `benchmarks/agent_join/.work/sealed/spider-pilot.json`. `PilotSample` is reserved for the 200 arm/repetition records built in Task 6. Write only task ID, database ID, eligibility reason code, database SHA-256, question SHA-256, gold SQL SHA-256, and schema SHA-256 to `tasks/spider-pilot-manifest.json`. Write the archive and every selected source-file hash to `tasks/spider-pilot-hashes.json`.

The preparation command is:

```bash
uv run python -m benchmarks.agent_join.prepare_spider \
  --archive /tmp/spider.zip \
  --work-dir benchmarks/agent_join/.work \
  --manifest benchmarks/agent_join/tasks/spider-pilot-manifest.json \
  --hashes benchmarks/agent_join/tasks/spider-pilot-hashes.json
```

- [ ] **Step 6: Run tests and commit the selector, not downloaded data**

```bash
uv run python -m pytest tests/test_agent_join_selection.py tests/test_agent_join_scorers.py -q
uv run python -m ruff check benchmarks/agent_join tests/test_agent_join_selection.py
git diff --check
git add benchmarks/agent_join/prepare_spider.py tests/agent_join_helpers.py tests/test_agent_join_selection.py
git commit -m "feat: add frozen Spider pilot selection"
```

Expected: tests pass and no file under `.work` is staged.

---

### Task 4: Build isolated oracle and blind-review JoinLint projects

**Files:**
- Create: `benchmarks/agent_join/projects.py`
- Modify: `tests/agent_join_helpers.py`
- Expand: `tests/test_agent_join_selection.py`

**Interfaces:**
- Consumes: selected database locks, Spider table metadata, sealed task database IDs, and a human-edited decision file.
- Produces: `build_oracle_project()`, `build_review_project()`, `write_review_sheet()`, `apply_review_sheet()`, tracked oracle and JoinLint model YAML, and regenerated local MCP project evidence.

- [ ] **Step 1: Write failing project-isolation tests**

Add a two-table SQLite fixture and assert:

```python
def test_review_bundle_excludes_questions_gold_and_foreign_keys(tmp_path: Path) -> None:
    source = build_orders_database(tmp_path)
    review = build_review_project(source, tmp_path / "review")
    serialized = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in review.rglob("*")
        if path.is_file() and path.suffix in {".json", ".yaml", ".md"}
    )
    assert "How many orders" not in serialized
    assert "SELECT" not in serialized
    assert "foreign_keys" not in serialized


def test_review_decisions_create_entities_only_from_reviewed_grains(tmp_path: Path) -> None:
    review = build_review_project(build_orders_database(tmp_path), tmp_path / "review")
    sheet = load_review_sheet(review)
    sheet["entities"]["customers"]["decision"] = "id"
    sheet["entities"]["orders"]["decision"] = "id"
    accept_only_order_customer(sheet)
    apply_review_sheet(review, sheet)
    model = load_model(review)
    assert model.entities["customers"].grain.keys == ["id"]
    assert model.entities["orders"].grain.keys == ["id"]


def test_oracle_project_serves_full_database_graph_and_fresh_validation(tmp_path: Path) -> None:
    project = build_oracle_project(build_orders_database(tmp_path), oracle_schema(), tmp_path / "oracle")
    model = load_model(project)
    assert {(edge.from_, edge.to) for edge in model.relationships} == {
        ("orders.customer_id", "customers.id")
    }
    assert get_data_model(project).status == "ok"
    assert validate_cached_edges(project, [model.relationships[0].id]).status == "ok"
```

Add `oracle_schema()`, `load_review_sheet()`, and `accept_only_order_customer()` to `tests/agent_join_helpers.py`. `oracle_schema()` returns the physical tables, single primary keys, and one FK pair; `load_review_sheet()` reads the emitted JSON; `accept_only_order_customer()` selects `id` for both grains, accepts only `orders.customer_id -> customers.id`, rejects all other candidate rows, and fills fixed reviewer metadata `fixture-reviewer` plus `2026-07-25T00:00:00Z`.

- [ ] **Step 2: Run and verify failure**

Run: `uv run python -m pytest tests/test_agent_join_selection.py -q`

Expected: FAIL because the project builder functions are missing.

- [ ] **Step 3: Implement deterministic entity and oracle relationship construction**

For oracle projects only, use the physical table name as entity ID and object name and the single Spider primary-key column as the confirmed grain. Orient an FK edge toward the endpoint listed in `primary_keys`; if both endpoints are primary keys, order endpoints by UTF-8 bytes and use `one_to_one`. Otherwise use `one_to_one` when the child column is unique in JoinLint's exact profile and `many_to_one` when it is not.

Review projects must not read Spider primary keys or foreign keys. Start with an empty model, scan the database, and derive each table's reviewable grain choices only from exact `is_unique` column profiles. The reviewer must select exactly one displayed unique column or `reject_table`. A rejected table cannot participate in an accepted relationship.

Use stable relationship IDs:

```python
def relationship_id(prefix: str, from_endpoint: str, to_endpoint: str) -> str:
    digest = hashlib.sha256(
        canonical_json({"from": from_endpoint, "to": to_endpoint})
    ).hexdigest()[:16]
    return f"{prefix}_{digest}"
```

Create each isolated project with `data/database.sqlite`, `.joinlint/config.yaml`, and `.joinlint/model.yaml`, then call `scan_project()` so arms C and D always start with fresh cached validation evidence.

- [ ] **Step 4: Implement the blind review sheet lifecycle**

`write_review_sheet()` writes an `entities` object and a `relationships` list. Each entity contains its physical object, exact unique-column choices, and `decision: pending`. Each relationship contains candidate ID, endpoints, confidence, exact evidence, and `decision: pending`. The document also contains `reviewer: ""` and `reviewed_at: ""`. It must not include Spider metadata or task IDs.

`apply_review_sheet()` must reject unknown keys, duplicate IDs, missing candidates, pending values, non-unique grain choices, relationships touching rejected tables, and every relationship value except `accept` or `reject`. It requires non-empty reviewer and RFC 3339 `reviewed_at`; writes reviewed entities first; rescans and verifies candidate IDs plus evidence are unchanged; applies relationship decisions through the public candidate service; rescans again; rejects any accepted relationship whose validation has a blocking finding; and writes the final model to `models/joinlint/{db_id}.yaml`.

- [ ] **Step 5: Prove forbidden input separation**

Add a helper `audit_review_bundle(review_root: Path, forbidden_hashes: set[str]) -> None` that hashes every regular file and refuses symlinks, filenames outside the allowlist, or hashes matching sealed question, gold SQL, tables metadata, or oracle model artifacts. Test both passing and deliberately contaminated bundles.

- [ ] **Step 6: Run focused and full tests, then commit**

```bash
uv run python -m pytest tests/test_agent_join_selection.py tests/test_end_to_end.py tests/test_mcp.py -q
uv run python -m ruff check benchmarks/agent_join tests/test_agent_join_selection.py
git diff --check
git add benchmarks/agent_join/projects.py tests/agent_join_helpers.py tests/test_agent_join_selection.py
git commit -m "feat: prepare blinded JoinLint evaluation models"
```

---

### Task 5: Score MCP calls, grounding, validation, and blocking compliance

**Files:**
- Create: `benchmarks/agent_join/trace.py`
- Create: `benchmarks/agent_join/fixtures/mcp-safety/manifest.json`
- Expand: `tests/test_agent_join_reporting.py`

**Interfaces:**
- Consumes: Inspect `ChatMessageAssistant` and `ChatMessageTool` history, normalized final SQL edges, the task's required edges, and the structured submit warning.
- Produces: `TraceScore`, `parse_joinlint_trace()`, `score_trace()`, and exact Tool Call, Successful Tool Call, Correct Tool, MCP-grounded Join, Validation, Blocking Compliance, Bypass, and Tool Error values.

- [ ] **Step 1: Write failing trace tests with real Inspect message types**

Create `tests/test_agent_join_reporting.py` with synthetic calls and results:

```python
import json

from inspect_ai.model import ChatMessageAssistant, ChatMessageTool
from inspect_ai.tool import ToolCall

from benchmarks.agent_join.trace import score_trace


EDGE = ("customers.id", "orders.customer_id")


def tool_messages(*, blocking: bool = False) -> list[object]:
    call = ToolCall(id="call-1", function="JoinLint_get_data_model", arguments={})
    response = {
        "command": "get_data_model",
        "status": "ok",
        "data": {
            "entities": {},
            "relationships": [
                {
                    "id": "order_customer",
                    "from": "orders.customer_id",
                    "to": "customers.id",
                    "cardinality": "many_to_one",
                    "status": "confirmed",
                }
            ],
        },
        "findings": ([{"code": "COMPOUND_FANOUT", "severity": "blocking", "message": "COMPOUND_FANOUT"}] if blocking else []),
    }
    return [
        ChatMessageAssistant(content="", tool_calls=[call]),
        ChatMessageTool(content=json.dumps(response), tool_call_id="call-1", function=call.function),
    ]


def test_call_is_grounded_only_when_final_sql_uses_returned_edges() -> None:
    grounded = score_trace(tool_messages(), frozenset({EDGE}), frozenset({EDGE}), "")
    guessed = score_trace([], frozenset({EDGE}), frozenset({EDGE}), "")
    assert grounded.tool_called is True
    assert grounded.grounded is True
    assert guessed.tool_called is False
    assert guessed.grounded is False


def test_blocking_path_requires_avoidance_or_explicit_warning() -> None:
    silent = score_trace(tool_messages(blocking=True), frozenset({EDGE}), frozenset({EDGE}), "")
    warned = score_trace(tool_messages(blocking=True), frozenset({EDGE}), frozenset({EDGE}), "fan-out risk")
    assert silent.blocking_compliant is False
    assert warned.blocking_compliant is True
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run python -m pytest tests/test_agent_join_reporting.py -q`

Expected: FAIL because `trace.py` is missing.

- [ ] **Step 3: Define and implement trace scoring**

Add this contract to `contracts.py`:

```python
class TraceScore(StrictModel):
    tool_called: bool
    successful_tool_call: bool
    correct_tool_use: bool
    grounded: bool
    validated: bool
    blocking_compliant: bool | None
    bypassed: bool
    tool_error: bool
    returned_edges: list[Edge]
    called_tools: list[str]
```

`parse_joinlint_trace()` must recognize only the exact names `get_data_model`, `find_join_path`, and `validate_join`, either bare or preceded by the exact `JoinLint_` MCP prefix; it must not split on the last underscore because all three function names contain underscores. Pair calls and results by `tool_call_id`, require JSON object responses with known envelope fields, reject duplicate call IDs, and extract edge-ID-to-endpoint mappings only from successful `get_data_model` responses. A path or validation call counts only when its referenced relationship IDs are present in an earlier successful model response.

`score_trace()` rules are exact:

- `grounded = bool(predicted_edges) and predicted_edges <= returned_edges`;
- `bypassed = bool(predicted_edges - returned_edges) and bool(returned_edges)`;
- `validated = validate_join` successfully covered every final edge ID;
- `correct_tool_use` requires successful `get_data_model`, successful validation, and `find_join_path` when the required graph has more than one edge;
- `blocking_compliant` is `None` when no blocking finding occurred, otherwise true only when the blocking edge is absent from final SQL or the structured warning is non-empty;
- any parse error, Inspect tool error, non-`ok`/`findings` envelope, or missing call result sets `tool_error=True`.

- [ ] **Step 4: Freeze the four local MCP safety cases**

Create `fixtures/mcp-safety/manifest.json` with exactly:

```json
{
  "schema_version": 1,
  "cases": [
    {"id": "safe_direct", "recipe": "safe_many_to_one", "expected_status": "ok"},
    {"id": "cardinality_mismatch", "recipe": "cardinality_drift", "expected_code": "CARDINALITY_DRIFT"},
    {"id": "compound_fanout", "recipe": "compound_fanout", "expected_code": "COMPOUND_FANOUT"},
    {"id": "stale_evidence", "recipe": "safe_many_to_one", "mutation": "rescan_after_model_change", "expected_code": "EVIDENCE_STALE"}
  ]
}
```

Reuse `benchmarks.joinsafety.generate` recipes; do not duplicate their table-writing code.

- [ ] **Step 5: Complete error, bypass, validation, and prefix tests**

Add tests for malformed JSON, result-before-call, duplicate call IDs, MCP-prefixed names, `find_join_path`, complete and incomplete `validate_join` coverage, a returned incorrect edge that is grounded but wrong, and an unreturned correct edge that is correct but ungrounded.

- [ ] **Step 6: Verify and commit**

```bash
uv run python -m pytest tests/test_agent_join_reporting.py -q
uv run python -m ruff check benchmarks/agent_join tests/test_agent_join_reporting.py
git add benchmarks/agent_join/contracts.py benchmarks/agent_join/trace.py benchmarks/agent_join/fixtures tests/test_agent_join_reporting.py
git commit -m "feat: score JoinLint MCP grounding behavior"
```

---

### Task 6: Run all four arms through one condition-aware Inspect task

**Files:**
- Create: `benchmarks/agent_join/joinlint_eval.py`
- Modify: `tests/agent_join_helpers.py`
- Create: `tests/test_agent_join_arm_isolation.py`

**Interfaces:**
- Consumes: sealed selected tasks, frozen prompts, oracle and JoinLint MCP projects, four safety projects, and preregistration settings.
- Produces: `submit_sql()`, `condition_solver()`, `build_samples()`, `spider_pilot_task()`, `run_eval()`, and append-only Inspect `.eval` logs for 192 primary plus eight diagnostic runs.

- [ ] **Step 1: Write failing arm-isolation tests**

Test the 200-sample suite and the exact 192-run primary matrix:

```python
def test_sample_matrix_is_complete_balanced_and_deterministic(frozen_inputs: Path) -> None:
    samples = build_samples(frozen_inputs)
    primary = [sample for sample in samples if sample.metadata["suite"] == "primary"]
    assert len(samples) == 200
    assert len(primary) == 192
    assert Counter(sample.metadata["arm"] for sample in primary) == {"A": 48, "B": 48, "C": 48, "D": 48}
    assert Counter(sample.metadata["repetition"] for sample in primary) == {0: 64, 1: 64, 2: 64}
    assert sum(sample.metadata["suite"] == "zero_config" for sample in samples) == 4
    assert sum(sample.metadata["suite"] == "safety" for sample in samples) == 4
    assert [sample.id for sample in samples] == [sample.id for sample in build_samples(frozen_inputs)]


def test_only_mcp_arms_receive_a_project_or_integration_instruction(frozen_inputs: Path) -> None:
    by_key = {
        (s.metadata["task_id"], s.metadata["repetition"], s.metadata["arm"]): s
        for s in build_samples(frozen_inputs)
        if s.metadata["suite"] == "primary"
    }
    task_id = next(iter({key[0] for key in by_key}))
    for repetition in range(3):
        a = by_key[(task_id, repetition, "A")]
        b = by_key[(task_id, repetition, "B")]
        c = by_key[(task_id, repetition, "C")]
        d = by_key[(task_id, repetition, "D")]
        assert a.metadata["mcp_project"] is None
        assert b.metadata["mcp_project"] is None
        assert c.metadata["mcp_project"].endswith("/oracle/" + c.metadata["db_id"])
        assert d.metadata["mcp_project"].endswith("/joinlint/" + d.metadata["db_id"])
        assert "confirmed relationships" in b.input
        assert "consult JoinLint" in c.input and "consult JoinLint" in d.input
        assert "consult JoinLint" not in a.input and "consult JoinLint" not in b.input


def test_arm_content_is_isolated_and_oracle_delivery_is_equivalent(frozen_inputs: Path) -> None:
    a, b, c, d = one_primary_quad(build_samples(frozen_inputs))
    for sample in (b, c, d):
        assert sample.metadata["question"] == a.metadata["question"]
        assert sample.metadata["schema_text"] == a.metadata["schema_text"]
        assert sample.metadata["schema"] == a.metadata["schema"]
        assert sample.metadata["gold_sql"] == a.metadata["gold_sql"]
        assert sample.metadata["allowed_graphs"] == a.metadata["allowed_graphs"]
    assert canonical_oracle_payload_from_input(b.input) == canonical_oracle_payload_from_project(
        Path(c.metadata["mcp_project"])
    )
    assert "oracle_relationships" not in c.input and "oracle_relationships" not in d.input
    assert c.input == d.input
    assert agent_visible_prompt(c) == agent_visible_prompt(d)
    assert {
        canonical_json(frozen_model_config(sample))
        for sample in (a, b, c, d)
    } == {canonical_json(frozen_model_config(a))}


def test_primary_order_is_balanced_within_each_database(frozen_inputs: Path) -> None:
    groups = primary_arm_order_groups(build_samples(frozen_inputs))
    for db_groups in groups.values():
        assert len(db_groups) == 12
        for position in range(4):
            assert Counter(group[position] for group in db_groups) == {
                "A": 3,
                "B": 3,
                "C": 3,
                "D": 3,
            }
```

Add `frozen_inputs(tmp_path: Path) -> Path`, `one_primary_quad()`, `canonical_oracle_payload_from_input()`, `canonical_oracle_payload_from_project()`, `agent_visible_prompt()`, `frozen_model_config()`, and `primary_arm_order_groups()` helpers. The fixture runs the miniature selector, builds four oracle and four reviewed JoinLint projects, builds the four safety projects, and returns the containing evaluation root. It must never read environment credentials.

- [ ] **Step 2: Run and verify failure**

Run: `uv run python -m pytest tests/test_agent_join_arm_isolation.py -q`

Expected: FAIL because `joinlint_eval.py` does not exist.

- [ ] **Step 3: Implement the structured submission tool**

Use an Inspect tool so every successful answer has one machine-readable shape:

```python
@tool(name="submit_sql")
def submit_sql() -> Tool:
    async def execute(sql: str, warning: str) -> str:
        """Submit one SQLite SELECT and any safety warning.

        Args:
            sql: One read-only SQLite SELECT, or an empty string when no safe query exists.
            warning: Empty when safe; otherwise the reason no safe query is available.
        """
        return json.dumps({"sql": sql, "warning": warning}, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    return execute
```

Pass it through `AgentSubmit(tool=submit_sql(), name="submit_sql")`; do not use JSON mode in parallel with tool calling.

- [ ] **Step 4: Implement one metadata-dispatched solver**

Use one Inspect `Task` so randomization occurs across arms rather than as four sequential task batches:

```python
@solver
def condition_solver() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sample = PilotSample.model_validate(state.metadata)
        tools: list[ToolSource] = []
        prompt = BASE_PROMPT
        if sample.arm in {"C", "D"}:
            if sample.suite != "zero_config":
                prompt += "\n\n" + MCP_PROMPT
            tools.append(
                mcp_server_stdio(
                    name="JoinLint",
                    command=sys.executable,
                    args=["-m", "joinlint", "serve-mcp", "--project", sample.mcp_project or ""],
                    cwd=REPOSITORY_ROOT,
                    env=sanitized_mcp_environment(),
                )
            )
        react_solver = as_solver(
            react(prompt=prompt, tools=tools, attempts=1, submit=AgentSubmit(tool=submit_sql(), name="submit_sql"))
        )
        return await react_solver(state, generate)

    return solve
```

`sanitized_mcp_environment()` must construct a new allowlisted mapping containing only `HOME`, `PATH`, `LANG`, `LC_ALL`, and `TMPDIR` when present. It must reject any allowlisted value containing the exact DeepSeek key value and must never include `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN`, or arbitrary inherited variables. Test this with sentinel values before starting any MCP process. JoinLint requires no provider credential.

- [ ] **Step 5: Build and deterministically order explicit repetitions**

Create all 192 primary `Sample` objects explicitly, with IDs `primary:{task_id}:{arm}:{repetition}`. Arm B input includes the canonical JSON oracle graph and validation results inline under the sentence `The following confirmed relationships and validation results are authoritative; base join conditions on them.` C and D inputs contain neither oracle payload nor that sentence.

Add four `zero_config` samples: choose the first seeded primary task from each database, use arm D and repetition zero, expose the JoinLint MCP project, and omit `mcp-integration.txt`. Add four `safety` samples from the frozen safety manifest, use arm D and repetition zero, and include the integration instruction. Neither suite enters A/B/C/D outcome effects.

For the 192 primary runs, do not use a single global hash sort because it does not guarantee the preregistered database-level balance. Within each database, rank the 12 `(task_id, repetition)` units with `seeded_rank(20260725, db_id, task_id, str(repetition))`. Derive one seeded base permutation of `[A, B, C, D]`, then assign cyclic rotations by ranked-unit index modulo four. This gives each arm exactly three appearances in every ordinal position in each database while still randomizing arm order. Rank the 48 four-run unit groups plus the eight one-run diagnostic groups by seeded group ID, preserving the balanced four-arm order inside each primary group. The arm-isolation test must prove the position counts, exact 200 IDs, and deterministic repeatability.

- [ ] **Step 6: Freeze DeepSeek and Inspect runtime parameters**

`run_eval()` must call:

```python
eval(
    spider_pilot_task(inputs_root),
    model="openai-api/deepseek/deepseek-v4-flash",
    model_args={"strict_tools": False},
    log_dir=str(log_dir),
    log_format="eval",
    fail_on_error=False,
    score_on_error=True,
    max_samples=4,
    max_retries=2,
    timeout=120,
    time_limit=120,
    turn_limit=5,
    max_tokens=4096,
    temperature=0,
    parallel_tool_calls=False,
    cache=False,
    extra_body={"thinking": {"type": "disabled"}},
)
```

Before the call, require `DEEPSEEK_BASE_URL` to equal `https://api.deepseek.com`; require but never print `DEEPSEEK_API_KEY`. Configure the `openai-api` provider from those two values without placing the key in `model_args`, task metadata, or logs: temporarily map them to the provider's `OPENAI_BASE_URL` and `OPENAI_API_KEY` environment variables only for the duration of `eval()`, then restore the prior environment in `finally`. The explicit MCP subprocess environment above prevents either provider variable from crossing into JoinLint. For `--model mockllm/model`, skip both environment checks and provider-variable mapping so local dry runs remain free.

`parallel_tool_calls=False` limits each model turn to one call. With `turn_limit=5` and a required final `submit_sql` call, a sample can make at most four MCP calls. A turn-limit stop is a completed failed sample and is not retried.

- [ ] **Step 7: Add a mock-model dry run that exercises submit and MCP**

Use Inspect's `mockllm/model` with a callable output sequence that first calls `JoinLint_get_data_model` for an MCP arm and then calls `submit_sql`. Assert the log contains one sample, both tool calls, a structured completion, and scorer metadata. Add an MCP fixture tool that returns its received environment variable names and assert no name containing `DEEPSEEK`, `OPENAI_API_KEY`, or `GITHUB_TOKEN` is present. The fake model must never import or read DeepSeek environment variables.

- [ ] **Step 8: Verify and commit**

```bash
uv run python -m pytest tests/test_agent_join_arm_isolation.py tests/test_mcp.py -q
uv run python -m ruff check benchmarks/agent_join tests/test_agent_join_arm_isolation.py
git diff --check
git add benchmarks/agent_join/joinlint_eval.py tests/agent_join_helpers.py tests/test_agent_join_arm_isolation.py
git commit -m "feat: add four-arm Inspect MCP runner"
```

---

### Task 7: Add safe execution with official Spider denotation equivalence

**Files:**
- Create: `benchmarks/agent_join/execution.py`
- Create: `benchmarks/agent_join/vendor/spider_result_eq.py`
- Create: `benchmarks/agent_join/vendor/README.md`
- Expand: `tests/test_agent_join_scorers.py`

**Interfaces:**
- Consumes: a validated predicted `SELECT`, gold SQL, one locked SQLite file, a five-second deadline, and a 10,000-row cap.
- Produces: `ExecutionResult`, `execute_readonly()`, and `execution_matches()` using the official `result_eq()` equivalence rule.

- [ ] **Step 1: Write failing security and denotation tests**

Add tests that create a disposable SQLite database and assert:

```python
def test_readonly_execution_matches_equivalent_column_order(tmp_path: Path) -> None:
    database = build_orders_database(tmp_path)
    result = execution_matches(
        database,
        "fixture-task",
        "SELECT customer_id, id FROM orders ORDER BY id",
        "SELECT id, customer_id FROM orders ORDER BY id",
        deadline_seconds=5,
        max_rows=10000,
    )
    assert result.executed is True
    assert result.equivalent is True


def test_execution_never_runs_mutation_attach_or_multiple_statements(tmp_path: Path) -> None:
    database = build_orders_database(tmp_path)
    original = database.read_bytes()
    for sql in ("DELETE FROM orders", "ATTACH DATABASE '/tmp/x' AS x", "SELECT 1; SELECT 2"):
        result = execute_readonly(database, sql, deadline_seconds=5, max_rows=10000)
        assert result.error_code == "UNSAFE_SQL"
    assert database.read_bytes() == original
```

Add deadline and row-cap cases and assert they return `EXECUTION_TIMEOUT` and `RESULT_LIMIT_EXCEEDED` without truncating a result into a false match.

- [ ] **Step 2: Run and verify failure**

Run: `uv run python -m pytest tests/test_agent_join_scorers.py -q`

Expected: FAIL because the execution module is missing.

- [ ] **Step 3: Vendor only the fixed official equivalence functions**

Copy `permute_tuple`, `unorder_row`, `quick_rej`, `multiset_eq`, `get_constraint_permutation`, and `result_eq` without semantic changes from:

```text
https://github.com/taoyds/test-suite-sql-eval/blob/e97acc546ecbee8fa27fa8dbf025ef61493a876c/exec_eval.py
```

Add the upstream Apache-2.0 copyright and source URL at the top of `spider_result_eq.py`. In `vendor/README.md`, record the source repository, commit, source path, license, copied function names, and the fact that JoinLint supplies its own safe read-only executor around the unchanged equivalence logic.

- [ ] **Step 4: Implement a bounded read-only SQLite executor**

Define the result contract in `execution.py` before the executor:

```python
@dataclass(frozen=True)
class ExecutionResult:
    executed: bool
    equivalent: bool | None = None
    rows: tuple[tuple[object, ...], ...] | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.executed == (self.error_code is not None):
            raise ValueError("executed results cannot carry an error code")
        if self.rows is not None and self.equivalent is not None:
            raise ValueError("a result contains rows or equivalence, never both")
```

`execute_readonly()` must first call `validate_readonly_select()`, copy the database to a temporary directory, open it with `file:<quoted-path>?mode=ro&immutable=1`, set a progress handler that aborts after `time.monotonic() + deadline_seconds`, and install a SQLite authorizer that allows only read/select/function opcodes while explicitly denying `load_extension` and every attach, pragma, schema-write, transaction, DDL, and DML opcode. It then executes exactly once and fetches at most `max_rows + 1`. It must close the cursor and connection in `finally` and return typed error codes rather than exception text. Add tests for a writable CTE when the parser accepts it and `SELECT load_extension(...)`; neither may reach an authorized side effect.

Use the exact public signature `execution_matches(database: Path, task_id: str, gold_sql: str, predicted_sql: str, *, deadline_seconds: float, max_rows: int) -> ExecutionResult`. It executes gold and prediction separately, treats gold failure as infrastructure corruption, derives `order_matters` from the parsed gold `ORDER BY`, and calls the unchanged `result_eq()`. Because the vendored helper uses module-global `random`, guard save-state, `random.seed(sha256(task_id).digest())`, `result_eq()`, and state restoration with one process-wide lock so four concurrent samples cannot alter one another or the caller's random state.

- [ ] **Step 5: Run security and equivalence tests**

```bash
uv run python -m pytest tests/test_agent_join_scorers.py -q
uv run python -m ruff check benchmarks/agent_join tests/test_agent_join_scorers.py
```

Expected: all cases pass and the source database hashes remain unchanged.

- [ ] **Step 6: Commit**

```bash
git add benchmarks/agent_join/execution.py benchmarks/agent_join/vendor tests/test_agent_join_scorers.py
git commit -m "feat: add safe Spider execution scoring"
```

---

### Task 8: Integrate deterministic Inspect scorers

**Files:**
- Create: `benchmarks/agent_join/scorers.py`
- Modify: `benchmarks/agent_join/joinlint_eval.py`
- Expand: `tests/test_agent_join_scorers.py`
- Expand: `tests/test_agent_join_reporting.py`

**Interfaces:**
- Consumes: `TaskState.output.completion`, `TaskState.messages`, `PilotSample` metadata, SQL edge functions, trace scoring, and safe execution.
- Produces: registered `join_outcome_scorer()`, `mcp_trace_scorer()`, and `execution_scorer()` with numeric values and complete metadata.

- [ ] **Step 1: Write failing scorer-contract tests**

Construct `TaskState` objects directly and assert each scorer returns one `Score` with stable names and metadata. Include valid SQL, malformed submit JSON, missing edges, ungrounded correct SQL, grounded wrong SQL, execution failure, and arm A where MCP metrics are explicitly not applicable.

- [ ] **Step 2: Run and verify failure**

Run: `uv run python -m pytest tests/test_agent_join_scorers.py tests/test_agent_join_reporting.py -q`

Expected: FAIL because the registered scorer factories are missing.

- [ ] **Step 3: Implement the join outcome scorer**

Use Inspect's decorator and numeric error value:

```python
@scorer(metrics=[mean()])
def join_outcome_scorer() -> Scorer:
    async def score(state: TaskState, target: Target) -> Score:
        sample = PilotSample.model_validate(state.metadata)
        try:
            submission = extract_submission(state.output.completion)
            predicted = extract_join_edges(submission.sql, sample.schema)
            outcome = score_join_graph(predicted, sample.allowed_graphs)
        except (ValueError, sqlglot.errors.SqlglotError) as error:
            outcome = JoinScore(
                wrong_join=True,
                precision=0.0,
                recall=0.0,
                f1=0.0,
                predicted_edges=[],
                matched_graph=sample.allowed_graphs[0],
                error_code="INVALID_MODEL_OUTPUT",
            )
        return Score(value=0 if outcome.wrong_join else 1, answer=state.output.completion, metadata=outcome.model_dump(mode="json"))

    return score
```

Store precision, recall, F1, predicted edges, matched graph, error code, SQL, and `submission.warning` in raw score metadata. Do not hide parser or submission errors. The report module must remove SQL and warning text while preserving their categorical effects.

- [ ] **Step 4: Implement MCP trace and execution scorers**

`mcp_trace_scorer()` returns `Score.unscored()` for arms A and B. For C and D it returns `1` only when grounded and stores every supporting trace metric. `execution_scorer()` returns `1` for equivalent, `0` for non-equivalent or invalid model SQL, and `Score.unscored()` only for a gold-query infrastructure failure that must stop the batch.

- [ ] **Step 5: Wire all scorers into the Inspect task**

Set:

```python
scorer=[join_outcome_scorer(), mcp_trace_scorer(), execution_scorer()]
```

Use `fail_on_error=False` and `score_on_error=True`; verify the fake-model log contains all three scorer keys for every completed sample.

- [ ] **Step 6: Run focused tests and commit**

```bash
uv run python -m pytest tests/test_agent_join_scorers.py tests/test_agent_join_reporting.py tests/test_agent_join_arm_isolation.py -q
uv run python -m ruff check benchmarks/agent_join tests/test_agent_join_scorers.py tests/test_agent_join_reporting.py
git add benchmarks/agent_join/scorers.py benchmarks/agent_join/joinlint_eval.py tests/test_agent_join_scorers.py tests/test_agent_join_reporting.py
git commit -m "feat: integrate MCP and SQL outcome scorers"
```

---

### Task 9: Generate sanitized paired reports and hierarchical bootstrap intervals

**Files:**
- Create: `benchmarks/agent_join/report.py`
- Expand: `tests/test_agent_join_reporting.py`

**Interfaces:**
- Consumes: one complete Inspect `.eval` log plus the frozen preregistration and hash lock.
- Produces: `ResultRow`, `load_result_rows()`, `write_sanitized_rows()`, `read_sanitized_rows()`, `paired_effects()`, `cluster_bootstrap()`, `evaluate_pilot_gates()`, sanitized per-run JSON, aggregate JSON, and Markdown.

- [ ] **Step 1: Write failing paired-effect and bootstrap tests**

Create synthetic rows covering two databases, two tasks per database, four arms, and three repetitions. Assert:

```python
def test_effects_use_baseline_minus_treatment_for_error_rates() -> None:
    rows = synthetic_rows(a_wrong=[1, 1, 0], d_wrong=[0, 0, 0])
    effects = paired_effects(rows)
    assert effects["wrong_join_rate_points"]["A_vs_D"] > 0


def test_cluster_bootstrap_is_seeded_and_resamples_databases_first() -> None:
    rows = clustered_rows()
    first = cluster_bootstrap(rows, draws=1000, seed=20260725)
    second = cluster_bootstrap(rows, draws=1000, seed=20260725)
    assert first == second
    assert first.draw_count == 1000


def test_report_never_contains_prompt_gold_sql_or_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "forbidden-secret")
    document = build_report(synthetic_rows(), frozen_metadata())
    serialized = json.dumps(document, sort_keys=True)
    assert "forbidden-secret" not in serialized
    assert "gold_sql" not in serialized
    assert "prompt" not in serialized
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run python -m pytest tests/test_agent_join_reporting.py -q`

Expected: FAIL because `report.py` is missing.

- [ ] **Step 3: Flatten Inspect logs into a strict sanitized row type**

Add this model to `contracts.py`:

```python
class ResultRow(StrictModel):
    sample_id: str
    task_id: str
    db_id: str
    arm: Arm
    suite: Literal["primary", "zero_config", "safety"]
    repetition: int
    artifact_complete: bool
    wrong_join: bool
    join_precision: float
    join_recall: float
    join_f1: float
    execution_correct: bool | None
    grounded: bool | None
    tool_called: bool | None
    validated: bool | None
    blocking_compliant: bool | None
    input_tokens: int
    input_cache_read_tokens: int
    output_tokens: int
    total_cost_usd: float
    total_time_seconds: float
    error_code: str | None
```

`load_result_rows()` must use `inspect_ai.log.read_eval_log()`, join the log against all 200 planned sample IDs, reject duplicates and mixed requested or returned model identities, and synthesize a row when a planned sample or required scorer record is absent. The synthesized values are `artifact_complete=False`, `wrong_join=True`, join precision/recall/F1 `0.0`, `execution_correct=False`, MCP booleans `False` for C/D and `None` for A/B, token/cost/time values `0`, and `error_code="MISSING_ARTIFACT"`. It must copy no messages, SQL, prompt, tool content, question, gold text, absolute path, or provider credential into `ResultRow`. Compute cost from the frozen cache-hit input, cache-miss input, and output rates in `preregistration.yaml`; do not rely on a mutable provider price lookup. Paired product effects use only the 192 rows whose suite is `primary`; diagnostic rows appear only in MCP behavior sections.

`write_sanitized_rows()` writes an object containing `schema_version`, the requested and returned model identities, the frozen input/log hashes, and exactly 200 UTF-8-sorted `ResultRow` objects. `read_sanitized_rows()` rejects unknown fields, duplicate or missing sample IDs, non-relative identifiers, and hash/model metadata that differs from the hash lock. Reporting functions accept only this validated sanitized document, never raw Inspect messages. Add a test that deletes the raw log after export and regenerates byte-identical aggregate JSON and Markdown from the sanitized rows.

- [ ] **Step 4: Implement paired summaries and database-clustered bootstrap**

For descriptive pilot effects, pair rows by `(db_id, task_id, repetition)` and report A versus B, B versus C, C versus D, and A versus D. For error rates, calculate baseline minus treatment so positive values mean improvement.

`cluster_bootstrap()` performs 10,000 deterministic draws. For each draw, sample database IDs with replacement; within each sampled database sample task IDs with replacement; within each sampled task sample repetition IDs with replacement; then retain all four paired arm rows for that sampled unit. Return the 2.5th and 97.5th percentile by the nearest-rank method and record the seed and draw count.

Add `summarize_relationship_review()` to compare each database's frozen JoinLint graph with its full single-column oracle graph and report candidate count, confirmed count, graph precision/recall/F1, accepted/rejected entity count, accepted/rejected relationship count, and elapsed human review seconds. These are database-level discovery diagnostics, not per-task samples, and must remain separate from Agent outcome effects.

- [ ] **Step 5: Implement protocol gates without product-effect gating**

`evaluate_pilot_gates()` checks artifact completion at least 0.95, scorer agreement at least 0.95, exact arm/repetition balance, one model identity, all required hash matches, and zero leaked forbidden fields. It must not require JoinLint to improve any outcome.

- [ ] **Step 6: Write deterministic JSON and Markdown outputs**

The JSON contains counts, rates, absolute percentage-point effects, exploratory intervals, MCP behavior metrics, errors by code and database, tokens, latency, model metadata, hashes, and protocol gates. The Markdown renders only values from that JSON. Both files sort keys and databases by UTF-8 bytes and include this heading:

```markdown
> Exploratory Spider pilot. These results validate the evaluation protocol and are not a general product or marketing claim.
```

- [ ] **Step 7: Verify and commit**

```bash
uv run python -m pytest tests/test_agent_join_reporting.py -q
uv run python -m ruff check benchmarks/agent_join tests/test_agent_join_reporting.py
git diff --check
git add benchmarks/agent_join/contracts.py benchmarks/agent_join/report.py tests/test_agent_join_reporting.py
git commit -m "feat: report paired MCP evaluation results"
```

---

### Task 10: Complete all free local gates and freeze the real pilot inputs

**Files:**
- Generate: `benchmarks/agent_join/tasks/spider-pilot-manifest.json`
- Generate: `benchmarks/agent_join/tasks/spider-pilot-hashes.json`
- Generate: `benchmarks/agent_join/models/oracle/{db_id}.yaml`
- Generate after human review: `benchmarks/agent_join/models/joinlint/{db_id}.yaml`
- Modify: `benchmarks/agent_join/preregistration.yaml`
- Modify: `docs/benchmarking.md`
- Modify: `docs/release-checklist.md`

**Interfaces:**
- Consumes: the official Spider archive, completed code, one human review decision for every visible candidate, and the independent process audit.
- Produces: frozen tracked manifests and models, locally regenerated MCP projects, a complete free preflight record, and the final paid-run approval request.

- [ ] **Step 1: Acquire the exact official archive without adding it to Git**

Download the Spider data file from the exact official URL documented in Task 1 and save it as `/tmp/spider.zip`. Do not use an unofficial mirror. Record the HTTP download timestamp and computed archive SHA-256 in `spider-pilot-hashes.json`; do not claim the digest was published by Spider.

- [ ] **Step 2: Select and lock the 16 pilot tasks**

Run:

```bash
uv run python -m benchmarks.agent_join.prepare_spider \
  --archive /tmp/spider.zip \
  --work-dir benchmarks/agent_join/.work \
  --manifest benchmarks/agent_join/tasks/spider-pilot-manifest.json \
  --hashes benchmarks/agent_join/tasks/spider-pilot-hashes.json
```

Expected: exactly four database IDs, exactly 16 task IDs, no questions or SQL in tracked manifests, and a sealed ignored task file.

- [ ] **Step 3: Build oracle projects and blind review bundles before inspecting sealed tasks**

Run:

```bash
uv run python -m benchmarks.agent_join.projects prepare \
  --work-dir benchmarks/agent_join/.work \
  --hashes benchmarks/agent_join/tasks/spider-pilot-hashes.json \
  --oracle-models benchmarks/agent_join/models/oracle \
  --review-dir benchmarks/agent_join/review
```

Expected: four oracle models, four local oracle MCP projects with fresh validation, four candidate decision sheets containing only allowed review fields, and a passing review-bundle audit.

- [ ] **Step 4: Pause for the required human semantic review**

Ask the user to review every file in `benchmarks/agent_join/review/{db_id}-decisions.json`. For each entity, replace `pending` with exactly one displayed unique grain column or `reject_table`. For each relationship, replace `pending` with `accept` or `reject`. Fill `reviewer` plus RFC 3339 `reviewed_at`. Do not expose `sealed`, `tables.json`, oracle models, task IDs, questions, or gold SQL during this review.

This is the only mandatory manual product-semantic step. An independent Agent may audit the bundle and decision completeness but must not choose accept or reject.

After the user finishes, launch the explicitly requested independent audit with no inherited conversation context and give it only `benchmarks/agent_join/review`, the allowlisted review schema, and the output of `audit_review_bundle()`. Require it to report pending decisions, forbidden content, duplicate IDs, and unexplained file additions without recommending any semantic accept/reject decision. Save its text report under the ignored review directory.

- [ ] **Step 5: Apply, validate, audit, and freeze the JoinLint models**

Run:

```bash
uv run python -m benchmarks.agent_join.projects apply \
  --work-dir benchmarks/agent_join/.work \
  --review-dir benchmarks/agent_join/review \
  --joinlint-models benchmarks/agent_join/models/joinlint \
  --hashes benchmarks/agent_join/tasks/spider-pilot-hashes.json
```

Expected: no pending decision, no blocking accepted relationship, four frozen JoinLint models, fresh D-arm MCP evidence, and model digests added to the hash lock.

- [ ] **Step 6: Run the complete free verification suite**

```bash
uv run python -m pytest tests/test_agent_join_selection.py tests/test_agent_join_scorers.py tests/test_agent_join_arm_isolation.py tests/test_agent_join_reporting.py -q
uv run python -m pytest -q
uv run python -m ruff check src tests benchmarks scripts
uv run python -m benchmarks.agent_join.joinlint_eval dry-run \
  --work-dir benchmarks/agent_join/.work \
  --log-dir benchmarks/agent_join/logs/dry-run
git diff --check
```

Expected: all tests pass, the fake-model dry run covers all four arms and four safety cases without a provider key, and no paid API request occurs.

- [ ] **Step 7: Run a bounded secret and artifact scan**

Run:

```bash
rg -n --hidden --glob '!uv.lock' --glob '!benchmarks/agent_join/.work/**' --glob '!benchmarks/agent_join/logs/**' "DEEPSEEK_API_KEY=|sk-[A-Za-z0-9]|api[_-]?key[\"' ]*:" .
git status --short
```

Expected: no credential value is found; no ignored data, review file, `.env`, or raw log is staged.

- [ ] **Step 8: Document the free harness and commit frozen public inputs**

Update `docs/benchmarking.md` to label this as an exploratory downstream MCP evaluation distinct from JoinSafetyBench. Add `python -m pip install -e '.[dev,eval]'`, the four free focused test files, an assertion that their pytest summary contains no skips, and the fake-model dry-run to `docs/release-checklist.md`; explicitly state that normal CI installs only `dev` and never calls DeepSeek.

```bash
git add benchmarks/agent_join/preregistration.yaml benchmarks/agent_join/tasks benchmarks/agent_join/models docs/benchmarking.md docs/release-checklist.md
git commit -m "test: freeze Spider MCP pilot inputs"
```

- [ ] **Step 9: Stop and request approval for the paid batch**

Report selected database/task counts, entity and relationship accept/reject counts, frozen hashes, free gate results, the 192 primary plus eight diagnostic runs, the exact 200-run total, and the current DeepSeek pricing link. Do not proceed until the user explicitly approves spending API credit.

---

### Task 11: Execute the paid DeepSeek pilot and diagnostics as one immutable batch

**Files:**
- Generate ignored: `benchmarks/agent_join/logs/{run-id}/*.eval`
- Generate ignored: `benchmarks/agent_join/results/raw/{run-id}/`
- Modify only after completion: `benchmarks/agent_join/tasks/spider-pilot-hashes.json` with run-log hashes and returned model identity.

**Interfaces:**
- Consumes: explicit paid-run approval, `DEEPSEEK_API_KEY`, exact base URL, frozen code and inputs, and passing Task 10 gates.
- Produces: one complete append-only Inspect log batch or one invalidated batch; never a partial merged result.

- [ ] **Step 1: Verify authority and environment without printing the key**

Confirm the user approved the paid run in the current task. Run:

```bash
uv run python -c "import os; assert os.environ.get('DEEPSEEK_API_KEY'); assert os.environ.get('DEEPSEEK_BASE_URL') == 'https://api.deepseek.com'"
```

Expected: silent success. If it fails, stop and ask the user to set the missing environment variable locally; never request the key value in chat.

- [ ] **Step 2: Recheck every frozen hash immediately before spending**

Run:

```bash
uv run python -m benchmarks.agent_join.prepare_spider verify \
  --work-dir benchmarks/agent_join/.work \
  --hashes benchmarks/agent_join/tasks/spider-pilot-hashes.json
```

Expected: all dataset, prompt, model, candidate-policy, scorer, runner, and preregistration hashes match.

- [ ] **Step 3: Start one 200-run batch**

Run:

```bash
uv run python -m benchmarks.agent_join.joinlint_eval run \
  --work-dir benchmarks/agent_join/.work \
  --log-dir benchmarks/agent_join/logs/pilot-20260725
```

Expected: one Inspect task with 192 primary and eight diagnostic samples, at most four concurrent samples, exact model ID, disabled thinking, no fallback model, and append-only `.eval` output.

- [ ] **Step 4: Apply the frozen batch-stop rule**

If hashes change, the returned model identity changes, a scorer crashes, gold SQL fails, or infrastructure artifacts corrupt, stop the batch before examining arm outcomes. Preserve the invalid batch under its run ID, record the reason, fix only the infrastructure cause, choose a new run ID, and rerun all 200 samples. Never combine batches.

- [ ] **Step 5: Hash and seal the completed raw log**

Run:

```bash
uv run python -m benchmarks.agent_join.report seal \
  --log-dir benchmarks/agent_join/logs/pilot-20260725 \
  --hashes benchmarks/agent_join/tasks/spider-pilot-hashes.json
```

Expected: exactly 200 unique planned sample artifacts are indexed, including explicit error artifacts; log hashes and requested/returned model identities are recorded; no outcome table is printed yet.

---

### Task 12: Blind-review scorer ambiguities and publish the exploratory report

**Files:**
- Generate ignored during review: `benchmarks/agent_join/review/scorer-sample.json`
- Create after log sealing: `benchmarks/agent_join/results/spider-pilot-rows.json`
- Create after review: `benchmarks/agent_join/results/spider-pilot.json`
- Create after review: `benchmarks/agent_join/results/spider-pilot.md`
- Modify: `docs/benchmarking.md`
- Modify: `benchmarks/agent_join/README.md`

**Interfaces:**
- Consumes: the sealed log, a stratified arm-hidden manual review sample, review decisions, and frozen report code.
- Produces: scorer agreement, protocol gates, complete exploratory results, reproduction documentation, and the go/no-go decision for a separate BIRD spec.

- [ ] **Step 1: Export a stratified arm-blinded scorer sample**

Run:

```bash
uv run python -m benchmarks.agent_join.report review-sample \
  --log-dir benchmarks/agent_join/logs/pilot-20260725 \
  --output benchmarks/agent_join/review/scorer-sample.json \
  --seed 20260725
```

The export contains exactly 40 seeded runs. It stratifies across valid, wrong, parse-error, MCP-used, MCP-bypassed, and tool-error categories when those categories exist, then fills any remaining slots by seeded rank. It removes arm labels, MCP project paths, provider metadata, prompts, and tool logs while retaining generated SQL, normalized predicted edges, allowed graphs, and automatic decisions needed for review.

- [ ] **Step 2: Complete blinded manual scorer review**

Ask the reviewer to mark each automatic edge and wrong-join decision `agree` or `disagree` with a reason. The reviewer cannot alter generated SQL or automatic output. Save decisions in the ignored review file and calculate agreement over all reviewed decisions.

- [ ] **Step 3: Export the strict sanitized per-run record**

Run:

```bash
uv run python -m benchmarks.agent_join.report sanitize \
  --log-dir benchmarks/agent_join/logs/pilot-20260725 \
  --preregistration benchmarks/agent_join/preregistration.yaml \
  --hashes benchmarks/agent_join/tasks/spider-pilot-hashes.json \
  --output benchmarks/agent_join/results/spider-pilot-rows.json
```

Expected: exactly 200 strict rows, including synthesized missing-artifact rows where necessary; no prompt, question, SQL, warning text, tool response, absolute path, or credential is present.

- [ ] **Step 4: Generate sanitized JSON and Markdown reports**

Run:

```bash
uv run python -m benchmarks.agent_join.report build \
  --rows benchmarks/agent_join/results/spider-pilot-rows.json \
  --review benchmarks/agent_join/review/scorer-sample.json \
  --preregistration benchmarks/agent_join/preregistration.yaml \
  --hashes benchmarks/agent_join/tasks/spider-pilot-hashes.json \
  --json-output benchmarks/agent_join/results/spider-pilot.json \
  --markdown-output benchmarks/agent_join/results/spider-pilot.md
```

Expected: reports account for all 200 planned runs or explicitly fail the 95% artifact gate; calculate A/B, B/C, C/D, and A/D comparisons only from the 192 primary runs; report the eight diagnostics separately; separate grounding from correctness; show all failure counts; label intervals exploratory; and contain no prompt, question, gold SQL, raw tool response, local absolute path, or credential. Move the raw log directory aside and repeat the build from `--rows`; output hashes must remain identical.

- [ ] **Step 5: Verify report determinism and security**

Run the same build command a second time and compare SHA-256 outputs. Then run:

```bash
uv run python -m pytest -q
uv run python -m ruff check src tests benchmarks scripts
git diff --check
rg -n 'DEEPSEEK_API_KEY|api.deepseek.com/v1/.+key|gold_sql|raw_tool_response' benchmarks/agent_join/results
```

Expected: regenerated hashes match, tests and Ruff pass, diff check is silent, and the bounded leak scan finds no forbidden content.

- [ ] **Step 6: Record the protocol decision without changing product code**

Update `docs/benchmarking.md` with the report links, protocol-gate result, sample/model/date limitation, and explicit statement that pilot effects are not marketing claims. If protocol gates fail, state which gate failed and stop; do not tune JoinLint against hidden pilot results. If they pass, recommend a separate BIRD formal-evaluation brainstorming/spec cycle.

- [ ] **Step 7: Commit the complete exploratory result**

```bash
git add benchmarks/agent_join/results/spider-pilot-rows.json benchmarks/agent_join/results/spider-pilot.json benchmarks/agent_join/results/spider-pilot.md benchmarks/agent_join/tasks/spider-pilot-hashes.json benchmarks/agent_join/README.md docs/benchmarking.md
git commit -m "test: record exploratory MCP value pilot"
```

Expected: raw logs, acquired data, review sheets, absolute paths, and secrets remain ignored and unstaged.

---

## Final Verification

After Task 12, run:

```bash
uv run python -m pytest -q
uv run python -m ruff check src tests benchmarks scripts
uv run python benchmarks/joinsafety/generate.py --output /tmp/joinlint-benchmark --seed 20260723
uv run python -m pytest tests/test_benchmark.py tests/test_scanner.py tests/test_end_to_end.py -q
git diff --check
git status --short
```

Expected:

- the full existing suite and all new evaluation tests pass;
- JoinSafetyBench still meets its frozen gates;
- Ruff and whitespace checks pass;
- tracked pilot results are deterministic and sanitized;
- only explicitly intended tracked result/documentation files are changed;
- no paid command is added to CI;
- the next action is either fix a failed protocol gate without inspecting product effects or begin a separate BIRD formal-evaluation design, never write the marketing claim from this pilot.
