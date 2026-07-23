# JoinLint v0 Design

**Date:** 2026-07-23

**Status:** Revised after independent audit; pending re-audit

**Product:** JoinLint
**Tagline:** Stop AI agents from guessing database joins.

## 1. Summary

JoinLint v0 is a local, Git-native data relationship linter for developers and
the coding or data agents they already use. It scans CSV, Parquet, DuckDB, and
SQLite sources; records deterministic evidence about schemas, candidate keys,
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
- exact distinct count;
- uniqueness evidence;
- value inclusion and orphan counts;
- source fingerprint;
- observed row multiplication for a proposed join.

Observed facts are reproducible for the same source, scanner version, and scan
policy. v0 validation is exact: sampling is not supported and no finding may be
reported as safe from partial evidence. Resource-limit failures return an
inconclusive error rather than a passing result. Observed facts are not business
semantics.

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
- Local DuckDB and SQLite files in read-only mode.
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
- A self-contained join-safety benchmark and regression harness for the core
  v0 claims.

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
- BIRD, Spider, or model-based downstream evaluation; these require a separate
  v0.1 evaluation milestone.

## 7. Architecture

```text
CSV / Parquet / DuckDB / SQLite
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

Loads source registration from `config.yaml`, resolves project-relative paths,
identifies supported formats, and prevents access outside approved roots. It
owns no semantic logic and never edits configuration during a scan.

#### Scanner

Reads source metadata and computes exact observed facts. It is deterministic
and does not call an LLM. Every generated record includes the source
fingerprint, scanner version, policy version, and timestamp. A v0 fingerprint
uses SHA-256 over canonical source bytes: a single file hashes its bytes; a
directory hashes the sorted POSIX-relative file paths and each supported file's
SHA-256. Generated evidence identifiers additionally include the scanner and
policy versions. Directory order, timestamps, and absolute paths do not affect
the digest.

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

Compares a fresh exact scan with the version-controlled sanitized baseline and
the constraints in `model.yaml`. It classifies additive changes, warnings, and
breaking changes with stable codes. It never depends on a prior generated cache,
so a clean CI checkout behaves like a local checkout.

#### Report generator

Creates `.joinlint/generated/report.html` from the model and generated evidence
as part of the same atomic generated-state transaction. The report is read-only,
contains no JavaScript, remote assets, or analytics, and contextually escapes
every identifier and value.

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
|-- config.yaml                # version-controlled source configuration
|-- model.yaml                 # version-controlled confirmed semantics
|-- baseline.json              # version-controlled sanitized CI baseline
|-- state/                     # durable local user state, ignored by Git
|   `-- rejections.json
|-- analyses/                  # reserved for later sanitized artifacts
`-- generated/                 # reproducible cache, ignored by Git
    |-- catalog.json
    |-- relationship-candidates.json
    |-- profile.json
    `-- report.html
```

Rules:

- `config.yaml` contains source identifiers, kinds, project-relative paths,
  limits, and policy selection. `joinlint source add` is the only v0 command
  that edits it.
- `model.yaml` contains only explicit confirmed semantics that refer to source
  identifiers from `config.yaml`.
- `baseline.json` contains a schema version, source fingerprints, normalized
  object and column schemas, confirmed-key results, and confirmed-relationship
  cardinality results. It contains no raw values, samples, credentials, or
  absolute paths. Updating it is an explicit user action after exact validation.
- `state/rejections.json` is local durable user state, not a reproducible cache.
  A rejection key hashes the candidate policy version, endpoints, types, and
  confidence band. It is invalidated when any of those inputs changes.
- Generated files record fingerprints, scanner version, policy version, and
  timestamp.
- Generated evidence never overwrites confirmed semantics.
- Raw rows, credentials, secrets, and unrestricted query results are never
  written to version-controlled artifacts.
- `analyses/` is reserved for a later explicitly saved, sanitized artifact
  workflow; v0 creates the directory contract but does not populate it.
- A rescan is idempotent for unchanged sources and configuration except for the
  recorded scan timestamp.
- Scan output, including `report.html`, is written to a sibling temporary
  directory and atomically replaces `generated/` only after every artifact is
  complete.

## 9. Confirmed model shape

The v0 configuration and model are intentionally small. They do not attempt to
compete with dbt's metric or semantic layers. Source configuration is separated
from human-confirmed semantics.

```yaml
# .joinlint/config.yaml
version: 1
sources:
  sales:
    kind: directory
    path: data
    limits:
      max_source_bytes: 1073741824
      max_scan_seconds: 60
```

```yaml
# .joinlint/model.yaml
version: 1
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

Schema v1 requires exactly one item in every `grain.keys` list. Composite keys
remain syntactically reserved but invalid until a later schema version defines
their validation semantics.

Cardinality is directed relative to `from -> to` and uses exactly one of:

- `one_to_one`: each `from` row matches at most one `to` row and vice versa;
- `one_to_many`: a `from` row may match many `to` rows; each `to` row matches at
  most one `from` row;
- `many_to_one`: each `from` row matches at most one `to` row; a `to` row may
  match many `from` rows;
- `many_to_many`: both sides may match multiple rows.

Validation reports `from_rows_per_to` and `to_rows_per_from` separately. It
never describes multiplication only as ambiguous "left" or "right" fan-out.

## 10. CLI contract

### `joinlint init`

Creates `.joinlint/`, minimal `config.yaml` and `model.yaml` files, local-state
and generated directories, and ignore rules without scanning data. It refuses
to overwrite existing files unless the user supplies an explicit force option.

### `joinlint source add <source-id> <path>`

Validates and atomically registers one supported project-relative source in
`config.yaml`. It rejects duplicate identifiers, paths outside the project root,
symlinks, unsupported formats, and configuration races.

### `joinlint scan [source-id]`

Exactly scans all registered sources by default or one registered source and
writes generated catalog, profile, candidate, and report artifacts. It never
edits `config.yaml`, `model.yaml`, or `baseline.json` and does not confirm
relationships. If exact scanning exceeds a configured limit, the command fails
as inconclusive and preserves the previous generated transaction.

### `joinlint candidates`

Lists relationship candidates with stable identifiers, evidence summaries,
and confidence labels.

### `joinlint accept <candidate-id>`

Copies a currently valid candidate into `model.yaml` as a confirmed user
decision. It fails if the evidence is stale or the model changed concurrently.

### `joinlint reject <candidate-id>`

Records a rejection in `.joinlint/state/rejections.json` so repeated scans do
not keep presenting the same unchanged candidate. The rejection remains local,
survives generated-cache replacement, and is invalidated when its deterministic
candidate key changes.

### `joinlint validate [relationship-or-path]`

Validates all confirmed relationships by default or one specified relationship
or path. It prints grain changes, cardinality, orphan counts, null counts, and
fan-out findings.

### `joinlint check`

Runs a fresh exact scan in memory, validates current data against `model.yaml`,
and compares sanitized current structure and validation evidence with the
version-controlled `baseline.json`. It works without generated cache, writes no
project artifact, and returns CI-suitable findings.

### `joinlint baseline update`

Requires a successful exact scan and validation, then atomically writes the
sanitized version-controlled baseline. It is an explicit user action and is
never run implicitly by `scan`, `validate`, `check`, or MCP.

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

### Project-root resolution

All commands accept `--project <path>`. Without it, commands other than `init`
walk from the resolved current directory upward and select the nearest directory
containing `.joinlint/config.yaml`; absence is an error. `init` uses the resolved
current directory unless `--project` is provided. The resolved project root and
approved source paths are immutable for a process lifetime. v0 rejects any
source path containing a symlink. Every file open repeats canonical containment
checks to prevent path replacement after startup.

### Machine-readable output

With `--json`, stdout contains exactly one UTF-8 JSON object and human logs go
to stderr. The versioned envelope is:

```json
{
  "schema_version": 1,
  "command": "check",
  "status": "ok",
  "data": {},
  "findings": [],
  "error": null
}
```

`status` is `ok`, `findings`, `inconclusive`, or `error`. Every finding and error
has a stable code, severity, bounded message, and source identifier without an
absolute path. JSON commands never truncate correctness-critical findings; they
fail with `OUTPUT_LIMIT_EXCEEDED` if the configured 1 MiB output limit would be
exceeded.

## 11. MCP contract

v0 uses local STDIO only and inherits the immutable project and source
boundaries established by `joinlint serve-mcp --project <path>`. It exposes
three bounded tools:

### `get_data_model`

Returns the version, confirmed entities, grains, keys, and relationships. It
does not return credentials, raw samples, or generated candidate values.

### `find_join_path`

Accepts source and target entity identifiers and returns confirmed candidate
paths with relationship identifiers, direction, cardinality, and known grain
changes. It never invents an unconfirmed edge.

### `validate_join`

Accepts confirmed relationship identifiers or a path returned by
`find_join_path`. MCP never scans sources or writes cache state. Before returning
validation evidence, it recomputes the canonical source SHA-256 fingerprints and
requires an exact generated profile with matching fingerprints, scanner
version, and policy version. Missing or stale evidence returns
`EVIDENCE_STALE` with the instruction to run CLI `joinlint scan`. Otherwise it
returns deterministic evidence and stable warning codes. It does not execute
user-provided SQL.

All tool responses use schema version 1 and the same finding and error envelopes
as CLI JSON. Responses are limited to 1 MiB. `find_join_path` accepts a maximum
depth of four, returns at most twenty paths ordered by relationship identifiers,
and fails rather than silently truncating. `validate_join` accepts at most
sixteen edges. Database strings are untrusted data and are never interpreted as
MCP or model instructions. v0 exposes no mutating MCP tool.

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

Every accepted candidate and every passing validation requires exact evidence.
If a configured source cannot be scanned exactly, candidate generation may
report evidence already computed but confirmation, validation, baseline update,
and CI success remain blocked with an `INCONCLUSIVE_SCAN` result.

### Join safety findings

The validator must detect and assign stable codes to:

- referenced key not unique;
- child key contains nulls;
- child rows have no referenced parent;
- relationship cardinality differs from the confirmed contract;
- join changes the `from` or `to` grain according to the directed cardinality;
- many-to-many join can multiply both sides;
- a join path compounds row multiplication;
- observed evidence is stale for the current source fingerprint.

The validator reports facts and risks. It does not claim that a statistically
valid relationship is semantically correct.

## 13. Security and privacy

Security requirements are part of v0, not deferred polish.

- Processing is local by default and requires no model provider.
- Only sources registered in `config.yaml` and contained within the immutable
  project root are readable.
- Source paths containing symlinks are rejected in v0. Canonical containment is
  rechecked at every file open to resist path replacement after startup.
- DuckDB and SQLite sources are opened read-only; arbitrary SQL is not accepted.
- Extension installation, external access, database attachment, and network
  access are not exposed by JoinLint commands or MCP tools.
- Scans have configurable file-size, memory, execution-time, and output limits.
- MCP responses exclude raw sample rows by default and remain size bounded.
- Column names and values are untrusted data and cannot alter tool behavior.
- Credentials are not supported in v0 configuration because remote databases
  are out of scope.
- Static reports contain no JavaScript or remote assets, apply contextual HTML
  and JSON escaping, never use unsafe `innerHTML`, and include a restrictive
  Content Security Policy: `default-src 'none'; style-src 'unsafe-inline'`.
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
  successful generated state while returning `INCONCLUSIVE_SCAN`.
- MCP starts only in a trusted project containing a valid JoinLint model.
- MCP returns `EVIDENCE_STALE` instead of scanning when exact generated evidence
  is missing or its source, scanner, or policy fingerprint differs.
- `joinlint check` fails with `BASELINE_MISSING` until the user explicitly
  creates the tracked baseline.
- Internal exceptions return a stable error envelope without leaking raw rows
  or local paths outside the approved project root.

## 15. Benchmark strategy

Core v0 and external evaluation have separate completion gates. Core v0 has no
network or third-party dataset dependency.

### Core v0 smoke

A small MIT-licensed Chinook SQLite fixture is pinned with its upstream version
and SHA-256 for installation, CLI, MCP, and known-relationship smoke tests. It
uses the same SQLite source adapter as users and is not evidence of
generalization.

### Core v0 JoinSafetyBench

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
evaluation seeds are separate and fixed before implementation tuning. The core
gate requires detection of every seeded blocking safety and drift case with no
incorrect `PASS` result. On GitHub Actions `ubuntu-latest`, the pinned one-million
row performance fixture must complete exact scan and validation within 60
seconds and below 1 GiB peak RSS; the workflow records both values.

### v0 demonstration

AdventureWorks supplies a recognizable multi-table business example after a
pinned, checksummed conversion into a supported local format. It is a demo, not
core acceptance and not evidence of generalization.

### v0.1 external discovery evaluation

BIRD Mini-Dev is deferred to a separately specified v0.1 milestone after its
distribution terms and conversion path are verified. The evaluation is called
`declared-FK recovery`, not general relationship discovery. Its specification
must define eligible compatible-type cross-table column pairs, exclusions,
manual audit of alleged false positives, and database-level development and
untouched evaluation splits before candidate-policy tuning. The core v0 release
does not depend on BIRD availability or scores.

### v0.1 downstream usefulness

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
  mutating, scanning, or arbitrary-query tools, including stale-evidence
  behavior.
- Golden static-report tests without embedding source data, plus malicious
  table and column identifiers that exercise escaping and CSP.
- Drift tests for additive, warning, and breaking mutations.
- End-to-end smoke from `init` through source registration, scan, confirmation,
  validation, baseline update, clean-checkout `check`, report, and MCP lookup.
- Benchmark tests that pin dataset version, transformation, and expected
  metrics.

## 17. v0 acceptance criteria

v0 is complete only when all of the following are reproducible:

1. A new user can initialize and scan the bundled smoke dataset with documented
   commands through the public SQLite adapter.
2. The scanner produces facts and candidates without an LLM or network access.
3. Candidates show evidence and remain untrusted until explicit confirmation.
4. Confirmed corrections and unchanged local rejections survive rescans without
   mixing user state into generated cache.
5. All seeded fan-out, duplicate-key, orphan, null-key, grain, and drift cases
   produce their expected stable findings, with zero incorrect `PASS` results.
6. The fixed held-out single-column relationship fixture reaches at least 0.85
   precision and 0.80 recall under the declared eligible-pair protocol. These
   thresholds validate the implementation but do not support a generalization
   claim.
7. On GitHub Actions `ubuntu-latest`, the pinned one-million-row fixture
   completes exact scan and validation within 60 seconds and below 1 GiB peak
   RSS.
8. From a clean checkout with no generated cache, `joinlint check` compares a
   fresh exact scan with tracked `baseline.json`, returns CI-appropriate exit
   codes, and changes no project file.
9. The static report shows sources, confirmed relationships, candidates,
   evidence, and risks without remote assets or raw rows.
10. A local MCP host can retrieve the confirmed model, find a confirmed path,
    and validate matching exact evidence through the three v0 tools; stale
    evidence deterministically returns `EVIDENCE_STALE`.
11. CLI JSON and MCP tests enforce the versioned envelopes, numeric limits,
    deterministic ordering, stdout/stderr policy, and stable errors.
12. MCP and CLI cannot execute arbitrary SQL, mutate confirmed semantics during
    read operations, follow symlinks, read outside approved roots, or return raw
    sample rows.
13. The checked-in core harness reports relationship-fixture, join-safety, drift,
    runtime, and memory results with pinned versions and commands.
14. Documentation states measured limits and does not present demo performance
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
