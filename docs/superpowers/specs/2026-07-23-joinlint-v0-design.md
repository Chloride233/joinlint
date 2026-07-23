# JoinLint v0 Design

**Date:** 2026-07-23

**Status:** Approved for implementation planning

**Product:** JoinLint
**Tagline:** Stop AI agents from guessing database joins.

## 1. Summary

JoinLint v0 is a local, Git-native data relationship linter for developers and
the coding or data agents they already use. It scans CSV, Parquet, and DuckDB
sources; records deterministic evidence about schemas, candidate keys,
cardinality, referential coverage, and row multiplication; proposes relationship
candidates without trusting them automatically; and preserves human-confirmed
semantics in a small reviewable YAML contract.

The same typed core powers two interfaces:

- a CLI for initialization, scanning, validation, and CI drift checks;
- a local STDIO MCP server that lets existing agents retrieve and validate
  confirmed join paths.

v0 does not ship a standalone conversational agent, an interactive Web app, or
arbitrary SQL execution through MCP. A static local report is sufficient for
inspection until user evidence justifies a richer editor.

## 2. Problem

When developers receive an unfamiliar database or a folder of related tables,
they must determine:

- which tables represent which records;
- what the grain of each table is;
- which columns can be used as keys;
- how tables are related;
- whether a join is one-to-one, one-to-many, or many-to-many;
- whether a join will multiply rows and corrupt an aggregate;
- whether a previously safe relationship has drifted.

Database tools can display schemas and ER diagrams, while LLM tools can propose
SQL. Neither is sufficient when foreign keys are absent, stale, or semantically
misleading. The dangerous failure is a plausible query that executes
successfully but returns a wrong result because its join or grain is wrong.

JoinLint's first job is therefore narrow:

> Give humans and existing agents a verified map of table relationships, and
> detect when that map becomes unsafe.

## 3. Target user and jobs

The primary user is an individual developer or data analyst working locally
with an unfamiliar multi-table dataset.

Primary jobs:

1. Inspect a new CSV, Parquet, or DuckDB data project.
2. Discover relationship candidates with inspectable evidence.
3. Confirm or reject those candidates without losing decisions on rescan.
4. Validate a proposed join before writing application or analytical SQL.
5. Give Codex, Claude, Cursor, or another MCP host confirmed data context.
6. Fail CI when a confirmed key or relationship becomes invalid.

Secondary users are maintainers reviewing AI-generated SQL and teams preserving
small data contracts in Git. Multi-user governance is outside v0.

## 4. Product promise

The user-visible promise is:

> Scan tables. Confirm relationships. Catch fan-out. Give every agent a trusted
> data map.

The intended recurring workflow is:

```text
enter an existing project
-> scan local data
-> inspect candidates and evidence
-> confirm relationships in model.yaml
-> validate joins locally or in CI
-> let an existing agent use the confirmed model through MCP
```

The durable output is not a chat transcript. It is a human-reviewed semantic
contract plus reproducible validation evidence.

## 5. Trust model

JoinLint must keep three states distinct.

### 5.1 Observed facts

Observed facts come from deterministic inspection, for example:

- physical column name and type;
- row count;
- null count;
- exact or sampled distinct count;
- uniqueness evidence;
- value inclusion and orphan counts;
- source fingerprint;
- observed row multiplication for a proposed join.

Observed facts are reproducible for the same source, scanner version, and
sampling policy. They are not business semantics.

### 5.2 Inferred candidates

Candidates are suggestions supported by observed evidence, such as a possible
foreign-key relationship. Candidate confidence expresses evidence strength,
not trust. Candidates remain in generated artifacts and are never silently
promoted into the confirmed model.

### 5.3 Confirmed semantics

Confirmed semantics are explicit user decisions stored in `model.yaml`.
Rescanning may attach new evidence or report drift, but it must never overwrite
or silently remove a confirmed decision.

## 6. v0 scope

### Included

- Python 3.12+ command-line application.
- Local CSV and Parquet directories.
- Local DuckDB files in read-only mode.
- Deterministic schema and column profiling.
- Candidate single-column key discovery.
- Candidate single-column relationship discovery.
- Relationship evidence and confidence classification.
- One-to-one, one-to-many, and many-to-many cardinality checks.
- Null-key, orphan-row, duplicate-key, grain-change, and fan-out warnings.
- Human confirmation through CLI commands or direct YAML edits.
- Static HTML relationship and validation report.
- Git-friendly confirmed model.
- Schema, key, relationship, and cardinality drift detection.
- CI-compatible exit codes and machine-readable JSON output.
- Local STDIO MCP server exposing bounded read and validation tools.
- Benchmark and regression harnesses for the v0 claims.

### Excluded

- Native LLM or model-provider dependency.
- Standalone chat interface or autonomous Agent loop.
- Interactive relationship graph editor.
- Chart or dashboard generation.
- PostgreSQL, MySQL, BigQuery, Snowflake, or remote warehouses.
- Metric definitions or a general semantic-layer framework.
- Composite-key inference or arbitrary relationship expressions.
- Arbitrary SQL generation or execution through MCP.
- DDL, DML, or source modification.
- Remote MCP, OAuth, hosted service, or tenant management.
- Automatic promotion of candidates into confirmed semantics.
- Raw query-result persistence.
- Full BIRD or Spider benchmark integration.

## 7. Architecture

```text
CSV / Parquet / DuckDB
        |
        v
Source boundary and deterministic scanner
        |
        v
Catalog, profile, keys, fingerprints
        |
        v
Relationship candidate engine
        |
        +----> generated static report
        |
        v
Human-confirmed model.yaml
        |
        +----> CLI validate/check and CI
        |
        +----> local STDIO MCP adapter
```

### 7.1 Core packages

The implementation plan should preserve these conceptual boundaries even if
the first code layout combines very small modules.

#### Source boundary

Resolves explicitly provided project-relative paths, identifies supported
formats, and prevents access outside approved roots. It owns no semantic logic.

#### Scanner

Reads source metadata and computes observed facts. It is deterministic and
does not call an LLM. Every generated record includes the source fingerprint,
scanner version, scan mode, and timestamp.

#### Candidate engine

Uses compatible types, normalized column names, key uniqueness, inclusion
coverage, null behavior, and value overlap to propose relationships. It emits
evidence and confidence but never edits the confirmed model.

#### Validator

Validates candidate or confirmed relationships against source data. It reports
key uniqueness, orphan rows, cardinality, null keys, and row multiplication.
It also validates multi-edge join paths for accumulated grain changes and
fan-out risk.

#### Model store

Loads and validates the versioned human-confirmed YAML contract. It preserves
user decisions across scans and rejects unknown schema versions or malformed
references. It never stores credentials or raw row samples.

#### Drift checker

Compares confirmed semantics and prior fingerprints with the current scan. It
classifies additive changes, warnings, and breaking changes with stable codes.

#### Report generator

Creates a self-contained static HTML report from the model and generated
evidence. The report is read-only and contains no remote scripts or analytics.

#### CLI adapter

Parses user intent, calls application services, renders concise terminal
output, writes JSON when requested, and maps findings to stable exit codes. It
contains no scanning or validation rules.

#### MCP adapter

Provides a small local STDIO surface over the same application services and
policy checks used by the CLI. It holds no separate model or cache and performs
no autonomous planning.

## 8. Repository and artifact lifecycle

The user's project receives:

```text
.joinlint/
|-- model.yaml                 # confirmed, version-controlled semantics
|-- analyses/                  # explicit sanitized user-owned artifacts
`-- generated/                 # reproducible cache, ignored by Git
    |-- catalog.json
    |-- relationship-candidates.json
    `-- profile.json
```

Rules:

- `model.yaml` contains only explicit confirmed semantics and stable source
  identifiers.
- Generated files record fingerprints, scanner version, scan mode, and
  timestamp.
- Generated evidence never overwrites confirmed semantics.
- Raw rows, credentials, secrets, and unrestricted query results are never
  written to version-controlled artifacts.
- `analyses/` is reserved for a later explicitly saved, sanitized artifact
  workflow; v0 creates the directory contract but does not populate it.
- A rescan is idempotent for unchanged sources and configuration except for the
  recorded scan timestamp.

## 9. Confirmed model shape

The v0 YAML model is intentionally small. It does not attempt to compete with
dbt's metric or semantic layers.

```yaml
version: 1
sources:
  sales:
    kind: directory
    path: data

entities:
  orders:
    source: sales
    object: orders.csv
    grain:
      keys: [order_id]
      status: confirmed

  order_items:
    source: sales
    object: order_items.parquet
    grain:
      keys: [order_item_id]
      status: confirmed

relationships:
  - id: order_items_to_orders
    from: order_items.order_id
    to: orders.order_id
    cardinality: many_to_one
    status: confirmed
```

Candidate confidence and changing profile statistics belong in generated
evidence, not in the confirmed file.

## 10. CLI contract

### `joinlint init`

Creates `.joinlint/`, a minimal `model.yaml`, and ignore rules without scanning
data. It refuses to overwrite existing files unless the user supplies an
explicit force option.

### `joinlint scan <path>`

Registers an approved project-relative source, scans it, and writes generated
catalog, profile, candidate, and report artifacts. It does not confirm
relationships.

### `joinlint candidates`

Lists relationship candidates with stable identifiers, evidence summaries,
and confidence labels.

### `joinlint accept <candidate-id>`

Copies a currently valid candidate into `model.yaml` as a confirmed user
decision. It fails if the evidence is stale or the model changed concurrently.

### `joinlint reject <candidate-id>`

Records a local rejection in generated state so repeated scans do not keep
presenting the same unchanged candidate. Rejections are not business semantics
and remain outside `model.yaml`.

### `joinlint validate [relationship-or-path]`

Validates all confirmed relationships by default or one specified relationship
or path. It prints grain changes, cardinality, orphan counts, null counts, and
fan-out findings.

### `joinlint check`

Runs scan freshness and drift checks suitable for CI. It does not mutate the
confirmed model.

### `joinlint report`

Regenerates the static HTML report from current artifacts.

### `joinlint serve-mcp`

Starts the local STDIO MCP server bound to the current trusted project.

### Exit codes

- `0`: command completed and no configured blocking finding exists;
- `1`: validation or drift finding should fail CI;
- `2`: invalid arguments, malformed configuration, unsupported input, or
  internal processing error.

Findings also have stable machine-readable codes so CI does not parse prose.

## 11. MCP contract

v0 uses local STDIO only and inherits the current project's source boundaries.
It exposes three bounded tools:

### `get_data_model`

Returns the version, confirmed entities, grains, keys, and relationships. It
does not return credentials, raw samples, or generated candidate values.

### `find_join_path`

Accepts source and target entity identifiers and returns confirmed candidate
paths with relationship identifiers, direction, cardinality, and known grain
changes. It never invents an unconfirmed edge.

### `validate_join`

Accepts confirmed relationship identifiers or a path returned by
`find_join_path`. It returns current deterministic validation evidence and
stable warning codes. It does not execute user-provided SQL.

All tool responses have row, byte, and item limits. Database strings are
untrusted data and are never interpreted as MCP or model instructions. v0
exposes no mutating MCP tool.

## 12. Relationship evidence and validation

### Candidate evidence

Single-column candidates may use:

- normalized-name compatibility;
- physical-type compatibility;
- uniqueness of the referenced column;
- inclusion coverage of non-null child values;
- null rate;
- orphan count and ratio;
- number of matched distinct values.

Confidence bands are deterministic, versioned policy output. They are not
probabilities and must be described as evidence strength.

### Join safety findings

The validator must detect and assign stable codes to:

- referenced key not unique;
- child key contains nulls;
- child rows have no referenced parent;
- relationship cardinality differs from the confirmed contract;
- one-to-many join changes the left or right grain;
- many-to-many join can multiply both sides;
- a join path compounds row multiplication;
- observed evidence is stale for the current source fingerprint.

The validator reports facts and risks. It does not claim that a statistically
valid relationship is semantically correct.

## 13. Security and privacy

Security requirements are part of v0, not deferred polish.

- Processing is local by default and requires no model provider.
- Only explicit project-relative sources are readable.
- Symlink and path traversal must not escape approved roots.
- DuckDB sources are opened read-only; arbitrary SQL is not accepted.
- Extension installation, external access, database attachment, and network
  access are not exposed by JoinLint commands or MCP tools.
- Scans have configurable file-size, memory, execution-time, and output limits.
- MCP responses exclude raw sample rows by default and remain size bounded.
- Column names and values are untrusted data and cannot alter tool behavior.
- Credentials are not supported in v0 configuration because remote databases
  are out of scope.
- Static reports contain no remote JavaScript, tracking, or embedded secrets.
- Generated profiles are ignored by Git because metadata can still be
  sensitive.

## 14. Error handling

- Unsupported files produce a stable input error and do not partially update
  confirmed state.
- A partial scan writes to a temporary location and replaces generated state
  only after success.
- Stale candidates cannot be accepted.
- Malformed `model.yaml` fails closed with a line-aware validation message.
- Missing source objects make confirmed relationships invalid rather than
  silently removing them.
- Ambiguous candidates remain candidates and require human selection.
- Resource-limit failures identify the affected source and preserve the last
  successful generated state.
- MCP starts only in a trusted project containing a valid JoinLint model.
- Internal exceptions return a stable error envelope without leaking raw rows
  or local paths outside the approved project root.

## 15. Benchmark strategy

Each benchmark measures a separate claim.

### Fast smoke

Chinook SQLite is used for installation, CLI, MCP, and known-relationship smoke
tests. It is not evidence of generalization.

### Flagship demonstration

AdventureWorks supplies a recognizable multi-table business example and seeded
aggregate traps. It is a demo dataset, not the headline accuracy benchmark.

### Relationship discovery

A fixed subset of BIRD Mini-Dev SQLite databases is evaluated with declared
foreign-key metadata hidden from the candidate engine. The runner measures
relationship precision, recall, and F1 against the held-out declarations.
BIRD data is downloaded on demand and is not redistributed until its
distribution terms are explicitly verified.

### Join safety

JoinLint maintains a deterministic generated corpus covering:

- one-to-one and one-to-many joins;
- many-to-many fan-out;
- duplicate parent keys;
- null child keys;
- orphan child rows;
- grain mismatch;
- multi-edge multiplication;
- controlled schema and cardinality drift.

Generation specifications and expected findings are public. Development and
evaluation seeds are separate and fixed before implementation tuning.

### Downstream usefulness

An optional BYOK evaluation compares an agent given only raw schema with the
same model given JoinLint's confirmed context. It measures join selection and
execution-result correctness. Model name, version, prompt version, temperature,
cost, latency, and repeated-run variance are recorded.

No improvement percentage may appear in the README until it is reproduced by
the checked-in harness. Reports distinguish percentage points from relative
percentage change and include failures.

## 16. Testing strategy

- Unit tests for path boundaries, fingerprints, schema inference, key evidence,
  relationship scoring, cardinality, and finding codes.
- Property-oriented tests for join multiplication and null/duplicate cases.
- Contract tests for YAML schema evolution and generated artifact versions.
- CLI integration tests for every command and exit code.
- MCP protocol tests for tool schemas, limits, error envelopes, and absence of
  mutating or arbitrary-query tools.
- Golden static-report tests without embedding source data.
- Drift tests for additive, warning, and breaking mutations.
- End-to-end smoke from `init` through scan, confirmation, validation, check,
  report, and MCP lookup.
- Benchmark tests that pin dataset version, transformation, and expected
  metrics.

## 17. v0 acceptance criteria

v0 is complete only when all of the following are reproducible:

1. A new user can initialize and scan the bundled smoke dataset with documented
   commands.
2. The scanner produces facts and candidates without an LLM or network access.
3. Candidates show evidence and remain untrusted until explicit confirmation.
4. Confirmed corrections survive rescans unchanged.
5. All seeded fan-out, duplicate-key, orphan, null-key, grain, and drift cases
   produce their expected stable findings.
6. `joinlint check` returns CI-appropriate exit codes and never changes
   `model.yaml`.
7. The static report shows sources, confirmed relationships, candidates,
   evidence, and risks without remote assets or raw rows.
8. A local MCP host can retrieve the confirmed model, find a confirmed path,
   and validate it through the three v0 tools.
9. MCP cannot execute arbitrary SQL, mutate the model, read outside approved
   roots, or return raw sample rows.
10. The checked-in evaluation harness reports relationship discovery and join
    safety metrics with pinned versions and commands.
11. Documentation states measured limits and does not present demo performance
    as generalization evidence.

The product decision gate after v0 is usage evidence. A conversational analysis
loop, richer data-source support, or an interactive Web editor starts only when
users demonstrate that the verified context layer is useful and identify the
next workflow bottleneck.

## 18. Deferred evolution

Potential follow-ups, in order of evidence rather than commitment:

1. Import dbt manifest and catalog metadata.
2. Composite keys and explicit user-defined relationship expressions.
3. Python API for Notebook and Polars workflows.
4. Sandboxed read-only query execution after an explicit security design.
5. Interactive local relationship editor if YAML correction is a measured
   adoption bottleneck.
6. Agentic multi-step analysis using the confirmed model.
7. PostgreSQL and other remote sources.
8. Remote MCP and multi-user governance.

These items are not part of v0 and must not appear as delivered behavior.
