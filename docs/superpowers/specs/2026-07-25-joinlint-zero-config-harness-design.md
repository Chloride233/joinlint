# JoinLint Zero-Configuration Join Harness Design

**Date:** 2026-07-25

**Status:** Design approved in conversation; pending written spec review

**Product:** JoinLint MCP

**Positioning:** A local, read-only Join Safety MCP for AI SQL agents.

**Product promise:** Stop AI agents from guessing database joins by giving them
evidence-backed join plans and validating final SQL before another tool
executes it, without requiring users to initialize a JoinLint project, run a
scan, or review relationship candidates.

## 1. Summary

JoinLint becomes a zero-configuration, read-only MCP trust layer for data
agents. A user connects the MCP server to a trusted local project or data
source. JoinLint then discovers supported sources, extracts declared
relationships, infers additional high-confidence relationships, selects
cardinality-aware join plans, and validates the final SQL before another tool
executes it.

The normal user workflow contains no `init`, `source add`, `scan`, candidate
review, `accept`, baseline update, or generated report step. The intended flow
is:

```text
install or enable JoinLint MCP
-> ask an existing agent a multi-table question
-> JoinLint lazily discovers and profiles the relevant source
-> the agent calls get_join_plan
-> the agent writes SQL
-> the agent calls validate_sql
-> a separate database or pipeline tool may execute validated SQL
```

JoinLint itself never executes SQL, mutates a source, returns raw rows, or asks
an LLM to invent a relationship. It automatically uses only relationships
whose provenance and evidence satisfy a versioned policy. When the evidence is
insufficient or ambiguous, it returns an inconclusive result instead of a best
guess.

This design combines two layers:

- the **JoinLint MCP**, which owns deterministic discovery, inference, planning,
  and SQL validation;
- a lightweight **Join Harness**, which installs concise procedural guidance so
  supported agents reliably call the MCP before and after generating
  multi-table SQL.

No Web UI or standalone conversational agent is required.

## 2. Relationship to JoinLint v0

This design supersedes the human-review onboarding and three-tool MCP workflow
in the JoinLint v0 design for the default product experience. In particular:

- `.joinlint/model.yaml` is no longer required before MCP use;
- inferred relationships do not require `joinlint accept`;
- stale evidence is refreshed automatically when safe instead of instructing
  the user to run `joinlint scan`;
- the default MCP surface becomes `get_join_plan` and `validate_sql`;
- the product claim changes from human-confirmed semantics to automatically
  verified, provenance-bearing relationship evidence.

Existing reviewed models remain valid input. During migration, their
relationships are imported as `curated`, which is stronger provenance than an
automatically inferred relationship. Existing CLI commands may remain as
maintenance and debugging interfaces, but they are not part of onboarding.

The existing `get_data_model`, `find_join_path`, and `validate_join` tools may
remain as compatibility aliases for one minor release. New integrations and
documentation use only the two task-oriented tools.

## 3. Problem

Generic database and pipeline MCP servers can expose schemas, preview rows,
construct workflows, and execute SQL. They do not establish that a plausible
join is semantically supported, preserves the intended grain, or remains safe
when composed with other joins.

The dangerous failure is not invalid SQL. It is SQL that executes and returns
plausible but incorrect results because:

- a same-named or overlapping column was treated as a relationship;
- an identifier domain happened to be contained in another identifier domain;
- the referenced side was not unique;
- a one-to-many traversal silently multiplied rows;
- individually valid edges formed a compound fan-out path;
- previously computed evidence no longer matched the source;
- the agent called a relationship tool but did not use its edges in final SQL.

The current v0 inference rule is too weak for automatic promotion. It considers
type compatibility, referenced-side uniqueness, and value overlap, then calls a
candidate `strong` whenever it has no nulls or orphans. That policy does not use
declared foreign keys, relationship names, support size, competing candidates,
or calibrated precision. Removing human review therefore requires a new trust
model and inference engine rather than simply accepting every current strong
candidate.

## 4. Goals and non-goals

### 4.1 Goals

- Require no project initialization or relationship-review workflow.
- Start from one trusted project root or explicitly configured source boundary.
- Discover local SQLite files and CSV datasets without scanning outside that
  boundary.
- Import declared single-column, self-referential, and composite foreign keys.
- Infer additional single-column relationships with high selective precision.
- Distinguish provenance, evidence strength, and current validation state.
- Prefer an inconclusive response over an unsupported automatic relationship.
- Return a complete, deterministic join plan for two or more entities.
- Validate the joins in final read-only SQL without executing the SQL.
- Detect cardinality changes, orphan rows, grain changes, many-to-many fan-out,
  and compound fan-out.
- Refresh stale evidence automatically through read-only, content-addressed
  scans.
- Keep raw values, credentials, absolute source paths, and query results out of
  MCP responses and persistent evidence.
- Work without an LLM, provider credential, network call, Web UI, or hosted
  service.
- Make correct MCP use likely across Codex, Claude Code, and Cursor with a
  one-command Harness setup.

### 4.2 Non-goals

- Executing user SQL or proxying a database connection.
- Automatically inferring composite keys in the first release. Declared
  composite relationships are supported; inferred composite relationships are
  deferred.
- Treating statistical inclusion as proof of business meaning.
- Generating DDL, DML, or pipeline code.
- Replacing a database catalog, semantic layer, or governance system.
- Cross-source relationship inference.
- Returning best-effort joins when every candidate is uncertain.
- Guaranteeing that every MCP host obeys instructions. Harness behavior is
  measured and reported, not assumed.
- Using raw data in a remote embedding or LLM service.

## 5. User experience

The MCP host config supplies a trusted root, or one or more explicit sources:

```text
joinlint serve-mcp --auto --project <trusted-root>
```

An installer may write the equivalent project-scoped MCP configuration for a
supported host. The user does not create JoinLint configuration files.

On the first relevant tool call, JoinLint:

1. discovers supported sources within the trusted boundary;
2. reads cheap schema metadata immediately;
3. imports declared keys and relationships;
4. profiles only the columns needed for the requested join plan;
5. caches sanitized, content-addressed evidence outside the project;
6. returns a safe plan or an inconclusive reason;
7. validates the final SQL against the same evidence before execution by
   another tool.

If no supported source exists, the MCP returns `SOURCE_NOT_FOUND`. If more than
one source contains the same unqualified entity name, it returns
`AMBIGUOUS_ENTITY` with source-qualified identifiers. It never scans a parent
directory or chooses a source outside the configured boundary.

Zero configuration means no JoinLint-specific modeling workflow. It cannot
eliminate the irreducible need to identify where the data lives. The trusted
root in the MCP configuration is the default source locator and security
boundary.

## 6. Architecture

```text
MCP host and Join Harness instructions
                  |
                  v
        get_join_plan / validate_sql
                  |
                  v
        MCP policy and response adapter
                  |
       +----------+-----------+
       |                      |
       v                      v
Join graph planner       SQL join extractor
       |                      |
       +----------+-----------+
                  |
                  v
      Relationship evidence engine
       |          |           |
       v          v           v
  metadata     profiler    inference policy
  extractor    and exact   and ambiguity
               validator   resolver
       |          |           |
       +----------+-----------+
                  |
                  v
      read-only snapshot and cache layer
                  |
                  v
          SQLite / CSV sources
```

### 6.1 Source boundary and discovery

The server anchors every read to one trusted project root using the existing
descriptor-relative, no-follow path boundary. Explicit `--source` values take
precedence. With `--auto`, discovery is limited to:

- supported SQLite files at the project root or at most two levels below
  conventional `data` and `datasets` directories;
- CSV files grouped by their containing conventional data directory;
- existing JoinLint source registrations and reviewed models, when present.

Discovery ignores `.git`, `.joinlint`, virtual environments, package caches,
`node_modules`, `vendor`, hidden directories other than `.joinlint`, and
symlinks. Every discovered source receives a deterministic identifier derived
from its project-relative path. Separate discovered sources never produce
cross-source candidates.

Automatic discovery is conservative to avoid surprising reads. A source
outside these rules must be supplied once in the MCP launch configuration; it
does not require project initialization or relationship review.

### 6.2 Metadata extractor

The metadata extractor reads the highest-value, lowest-cost evidence first:

1. primary, unique, and foreign-key declarations;
2. column physical types, nullability, and defaults;
3. existing reviewed JoinLint relationships, if present;
4. supported local metadata such as dbt manifests or schema YAML when found
   inside the trusted root;
5. table and column names and descriptions.

For SQLite, foreign keys come from `PRAGMA foreign_key_list`, keys and
nullability come from `PRAGMA table_info`, and indexes come from
`PRAGMA index_list` and `PRAGMA index_info`. Declared self-references and
composite foreign keys are preserved as first-class edges.

Historical SQL may later contribute a bounded supporting feature, but it never
creates or verifies a relationship by itself. Query-log ingestion is excluded
from the first release because it expands privacy and source-integration scope.

### 6.3 Targeted profiler

The profiler computes evidence only for columns needed by a current candidate
or SQL validation. It does not load every table into memory before the first
response.

For every evaluated endpoint it may compute:

- exact row, null, and distinct counts;
- exact referenced-side uniqueness;
- normalized type compatibility;
- child-to-parent inclusion and orphan counts;
- matched distinct support;
- observed one-to-one or many-to-one direction;
- row multiplication for the edge and selected path.

SQLite profiling uses a consistent read-only snapshot and SQL aggregation.
CSV profiling streams a private consistent snapshot. Probabilistic sketches may
prune impossible candidates, but an edge cannot become `verified` from sampled
or approximate evidence. Exact verification is required before automatic use.

### 6.4 Relationship evidence engine

The evidence engine produces one immutable record per relationship definition
and source snapshot. It does not promote a relationship by modifying a
user-authored model.

Each record includes:

- source and relationship identifiers;
- ordered child and parent endpoint tuples;
- provenance state;
- physical types;
- key and uniqueness evidence;
- inclusion numerator and denominator;
- null and orphan counts;
- matched distinct support;
- normalized name-alignment features;
- competing-candidate count and score margin;
- cardinality and observed multiplication;
- source fingerprint, policy version, and evidence digest;
- validation state and stable findings.

Raw values and row samples are never stored in the record.

### 6.5 Join graph planner

The planner operates only on `auto_usable` edges. It accepts two to eight
source-qualified entities and an optional starting grain. It finds bounded
paths of at most four edges between required entities, composes their
cardinalities, and rejects paths with blocking findings.

Plans are ordered lexicographically by:

1. blocking-finding count;
2. provenance strength;
3. reverse one-to-many and many-to-many traversals;
4. accumulated uncertainty cost;
5. edge count;
6. stable edge identifiers.

For provenance ordering, `curated` is preferred over `declared`, and
`declared` is preferred over `verified`. Provenance never outranks a blocking
finding or an invalid current relationship.

The first zero-blocking plan is recommended. At most three additional
zero-blocking alternatives are returned. If only blocking or uncertain paths
exist, no plan is recommended.

For more than two entities, the planner deterministically grows a connected
subgraph from the requested starting entity using the best bounded path to each
remaining entity, then validates the composed subgraph as one path set. It does
not independently approve edges and ignore their compound effect.

### 6.6 SQL join extractor and validator

`validate_sql` parses one bounded read-only statement with SQLGlot. It never
sends the statement to the source. The validator:

- accepts `SELECT` or `WITH` statements only;
- resolves table aliases, CTEs, subqueries, `JOIN ... ON`, equality predicates
  in `WHERE`, and resolvable `USING` clauses;
- rejects DDL, DML, multiple statements, and `NATURAL JOIN`;
- normalizes every join edge as source-qualified endpoint tuples;
- matches composite equality predicates to declared composite relationships;
- checks that every final edge is `auto_usable` under current evidence;
- validates traversal direction, cardinality, grain, and path composition;
- returns repair hints from the best non-blocking plan when validation fails.

The first release infers only single-column relationships but can validate a
declared composite relationship when all component equality predicates are
present.

## 7. Automatic relationship inference

### 7.1 Provenance states

Relationships use explicit provenance rather than the old confidence labels:

| State | Meaning | Automatic use |
|---|---|---|
| `curated` | Imported from an existing human-reviewed JoinLint model | Yes, after current validation |
| `declared` | Declared by the database or supported schema metadata | Yes, with current findings attached |
| `verified` | Inferred and accepted by the versioned exact-evidence policy | Yes |
| `uncertain` | Plausible but ambiguous, weakly supported, or approximate | No |
| `rejected` | Failed a hard safety gate | No |

`curated` describes provenance, not permanent truth. A current uniqueness,
orphan, cardinality, or stale-source finding can still block its use.
`declared` similarly means the source declared the relationship; it does not
erase current data-quality findings.

### 7.2 Candidate generation

Declared and curated relationships bypass inference candidate generation but
still receive current validation.

For undeclared single-column relationships, a candidate is generated only
when:

- the endpoints are in different tables, unless a self-reference is strongly
  indicated by metadata and distinct columns;
- the types are equal or safely numeric-compatible;
- the possible parent is exactly unique in current evidence;
- the child contains at least one non-null value;
- the columns are not boolean and are not obviously low-cardinality enum or
  status fields;
- either normalized names align or the value evidence is strong enough to
  justify full evaluation.

Name normalization tokenizes snake case, camel case, separators, singular and
plural table forms, and conventional suffixes such as `id`, `key`, `code`, and
`ref`. Generic suffix similarity alone is not positive evidence: `orders.id`
does not align with every other `id` column.

The generator prunes candidates before exact comparison using metadata and
optional local sketches. Pruning may remove a candidate but can never verify
one.

### 7.3 Evidence features

The versioned policy consumes at least these features:

- declared or curated provenance;
- parent uniqueness;
- exact inclusion ratio;
- exact orphan ratio;
- null ratio;
- matched distinct support;
- child and parent row counts;
- physical-type compatibility;
- child-column to parent-table name alignment;
- child-column to parent-column name alignment;
- low-cardinality and generic-domain penalties;
- number of competing unique parent columns;
- score difference between the best and second-best parent;
- observed cardinality and multiplication.

The ambiguity features are mandatory. Perfect inclusion is insufficient when a
child identifier fits multiple unrelated parent domains.

### 7.4 Hard gates

An inferred candidate cannot become `verified` unless all of the following are
true:

- exact current evidence is available;
- referenced-side uniqueness is exactly true;
- physical types are compatible;
- at least eight distinct child values match the parent;
- inclusion is at least 99.5 percent and the orphan ratio is at most 0.5
  percent;
- neither endpoint is classified as a low-cardinality enum or boolean;
- no competing parent lies within the policy's ambiguity margin;
- the calibrated policy score exceeds the versioned auto-use threshold;
- direct-edge validation has no blocking finding.

The support, inclusion, orphan, ambiguity, and score thresholds live in a
versioned policy artifact and are recorded in evidence. The values above are
the minimum v1 hard gates; calibration may make them stricter but not looser
without a policy-version change and a new release evaluation.

Small tables with fewer than eight matched distinct values can still use
curated or declared relationships. Undeclared relationships on such tables
remain `uncertain` because perfect overlap on a tiny domain is weak evidence.

### 7.5 Calibrated policy

After the hard gates, an explainable monotonic model ranks remaining
candidates. The initial implementation may use logistic regression or a
monotonic tree model. It is trained and calibrated on database-level splits so
that rows or schemas from the same database never appear in both training and
evaluation sets.

The policy optimizes selective precision, not raw recall. The auto-use
threshold is the lowest threshold that satisfies the release precision gate on
the frozen validation set. The exposed value is called a probability only if
calibration error passes the release gate; otherwise MCP responses expose
`evidence_score` and provenance without probabilistic wording.

All weights, preprocessing rules, thresholds, benchmark hashes, and training
code are versioned. Runtime inference remains deterministic and requires no
network call.

### 7.6 Optional semantic reranking

A future optional reranker may inspect only table names, column names,
descriptions, and the user's requested entity set. It may rerank candidates
that already passed type, uniqueness, support, and inclusion gates. It may not:

- see raw values;
- create a new candidate;
- override a blocking finding;
- promote an edge that failed a hard gate;
- silently resolve an ambiguity below the required score margin.

The first zero-configuration release does not require this component. The core
must meet its release gates without a provider credential or remote model.

## 8. Freshness and cache behavior

### 8.1 Lazy, just-in-time validation

Startup performs source discovery and cheap metadata extraction. Expensive
value profiling is deferred until a requested plan or SQL references an edge.

For a declared relationship, `get_join_plan` may return the edge immediately
with `validation_state: pending`. Before `validate_sql` can return a passing
result, JoinLint obtains exact current evidence for every final edge and the
composed path. An inferred relationship is never `verified` from pending or
approximate evidence.

### 8.2 Content-addressed cache

Sanitized evidence is stored atomically in a per-user operating-system cache,
not in the source project. The cache directory is mode `0700` where supported.
Cache keys include:

- framed source fingerprint;
- normalized relationship definition;
- scanner version;
- inference policy version;
- SQL-normalization version.

Cache records contain schemas, counts, hashes, evidence features, and findings,
but no raw rows, samples, credentials, SQL text, or query results.

### 8.3 Automatic refresh

Every tool call cheaply verifies current source identity. When the source or
policy fingerprint changes, JoinLint invalidates only affected records and
recomputes the requested evidence. The user is never told to run a separate
scan command.

If a consistent snapshot cannot be obtained, a resource limit is exceeded, or
the source changes during validation, the tool returns `inconclusive` with a
stable code. It does not reuse stale passing evidence.

## 9. MCP contract

The default server exposes exactly two read-only tools.

### 9.1 `get_join_plan`

Input:

```json
{
  "entities": ["sales.order_items", "sales.orders", "sales.customers"],
  "start_entity": "sales.order_items",
  "max_depth": 4
}
```

Rules:

- `entities` contains two to eight unique source-qualified identifiers;
- `start_entity`, when supplied, must be in `entities`;
- `max_depth` is between one and four;
- unqualified names are accepted only when they resolve uniquely;
- the tool returns no more than one recommended plan and three alternatives;
- uncertain edges never appear in the recommended plan.

Successful output includes:

```json
{
  "schema_version": 2,
  "command": "get_join_plan",
  "status": "ok",
  "data": {
    "plan_id": "plan_<digest>",
    "source_fingerprint": "<digest>",
    "policy_version": "inference-v1",
    "start_entity": "sales.order_items",
    "edges": [
      {
        "id": "edge_<digest>",
        "from": {
          "entity": "sales.order_items",
          "columns": ["order_id"]
        },
        "to": {
          "entity": "sales.orders",
          "columns": ["id"]
        },
        "predicates": ["order_items.order_id = orders.id"],
        "traversal": "forward",
        "cardinality": "many_to_one",
        "provenance": "verified",
        "validation_state": "current"
      }
    ],
    "findings": [],
    "alternatives": []
  },
  "findings": [],
  "error": null
}
```

When no safe plan exists, the response is `inconclusive` and contains a stable
error code such as `NO_VERIFIED_PATH`, `AMBIGUOUS_RELATIONSHIP`,
`SOURCE_CHANGED_DURING_SCAN`, or `RESOURCE_LIMIT_EXCEEDED`. It may return
bounded uncertain alternatives as diagnostic data, but marks them
`auto_usable: false` and never supplies them as the recommended plan.

Schema version 2 permits bounded diagnostic `data` on an `inconclusive`
response. Consumers must treat `status`, not the presence of data, as the
authorization decision. Schema version 1 compatibility responses retain the
old `data: null` behavior.

### 9.2 `validate_sql`

Input:

```json
{
  "sql": "SELECT ...",
  "dialect": "sqlite",
  "expected_grain": "sales.order_items"
}
```

Rules:

- `sql` is at most 64 KiB of UTF-8 text;
- exactly one `SELECT` or `WITH` statement is accepted;
- `dialect` defaults to the discovered source dialect;
- `expected_grain` is optional and must resolve to one entity;
- parsing never executes the statement;
- response size remains limited to 1 MiB.

Successful output includes normalized final edges, matched relationship IDs,
current evidence digests, resulting grain, and findings. A pass requires every
final join edge to map to a current `auto_usable` relationship and the complete
path to have no blocking finding.

Failures return stable, actionable codes including:

- `SQL_PARSE_ERROR`;
- `UNSUPPORTED_SQL_STATEMENT`;
- `UNKNOWN_ENTITY`;
- `UNSUPPORTED_JOIN_EDGE`;
- `AMBIGUOUS_RELATIONSHIP`;
- `REFERENCED_KEY_NOT_UNIQUE`;
- `ORPHAN_CHILD_ROW`;
- `CARDINALITY_DRIFT`;
- `GRAIN_CHANGE`;
- `MANY_TO_MANY_FANOUT`;
- `COMPOUND_FANOUT`;
- `SOURCE_CHANGED_DURING_SCAN`;
- `RESOURCE_LIMIT_EXCEEDED`.

When a safe alternative exists, the response includes a bounded repair object
containing relationship IDs and normalized predicates. It does not rewrite or
execute SQL inside the MCP server.

### 9.3 Response envelope

Both tools use one strict envelope:

- `schema_version`;
- `command`;
- `status`: `ok`, `findings`, `inconclusive`, or `error`;
- `data`;
- stable structured `findings`;
- sanitized `error`.

Unknown fields are rejected. Internal exceptions never reveal paths, SQL text,
raw values, credentials, or tracebacks.

### 9.4 Server instructions

The MCP server advertises concise instructions:

> Before generating multi-table SQL, call `get_join_plan` with every intended
> entity and use only predicates from the recommended plan. After generating
> SQL, call `validate_sql`. Do not submit or execute SQL when validation is
> inconclusive or contains a blocking finding.

Tool descriptions repeat only the tool-specific preconditions. Procedural
guidance belongs in server instructions and the Harness skill, not in verbose
tool schemas.

## 10. Lightweight Join Harness

MCP alone cannot force an agent to call a tool. The Join Harness packages the
smallest host-specific layer needed to improve correct use.

### 10.1 Components

- one project-scoped MCP configuration;
- one concise Codex, Claude Code, or Cursor instruction file;
- the canonical two-call procedure;
- installation and health-check commands;
- no UI, agent runtime, provider key, or persistent service beyond the MCP
  process.

### 10.2 Canonical agent procedure

For every task that produces multi-table SQL:

1. identify all intended entities from the user request and available schema;
2. call `get_join_plan` once with the complete entity set, with at most one
   guidance-authorized changed replan;
3. use only predicates in the recommended plan;
4. generate the SQL;
5. call `validate_sql` with the exact final SQL and immutable proof-bound grain;
6. make at most one changed SQL-only repair when guidance is retryable with
   `next_action: revise_sql`, then revalidate with the same proof;
7. stop and explain for every other inconclusive, error, or blocking result;
8. execute only through a separately authorized database or pipeline tool.

The Harness does not tell the agent that a tool call alone establishes safety.
The final SQL must be grounded in returned edges and pass final validation.

### 10.3 Installation

A one-command installer may configure one named host or every detected
supported host. It writes only documented project-scoped MCP and instruction
files, shows every target before writing, and never copies provider credentials.

The MCP package must also remain usable without the installer through a normal
STDIO configuration. Host-specific setup improves tool-use success but is not a
runtime dependency.

## 11. Safety and privacy

- Sources are opened read-only through the trusted path boundary.
- SQLite uses a consistent private snapshot that includes committed WAL state.
- CSV files are copied and verified through the existing no-follow snapshot
  boundary.
- The MCP exposes no mutation, execution, shell, file-write, or arbitrary query
  tool.
- `validate_sql` parses input but never connects it to a database executor.
- MCP responses and caches contain no raw rows or raw distinct values.
- Source strings are untrusted data and are never interpreted as instructions.
- Errors are stable codes with sanitized messages.
- Cache writes are atomic and confined to a per-user cache root.
- Resource-limit or freshness failures are inconclusive, never passing.
- A semantic reranker, if later enabled, receives metadata only and cannot
  bypass deterministic gates.

## 12. Performance and resource behavior

The system uses progressive work:

1. metadata extraction;
2. cheap candidate pruning;
3. exact profiling of requested edges;
4. path validation;
5. content-addressed reuse.

Declared metadata lookup should complete without a full-table scan. Exact
verification is targeted to the edges needed by the current plan or SQL.

Release performance gates are:

- cached `get_join_plan` and `validate_sql` complete within 500 ms at p95 on the
  committed local fixture suite;
- metadata-only declared-edge planning completes within one second at p95;
- the existing one-million-row exact validation fixture completes within 60
  seconds and under 1 GiB peak RSS;
- a tool exceeding its configured time or memory budget returns
  `RESOURCE_LIMIT_EXCEEDED` rather than partial passing evidence;
- MCP responses remain below 1 MiB.

Hardware, Python version, operating system, row counts, and cache state are
recorded with every published performance result.

## 13. Evaluation strategy

Inference quality and Agent behavior are evaluated separately.

### 13.1 Relationship inference evaluation

The frozen suite contains:

- SQLite databases with declared foreign keys, evaluated both with declarations
  visible and with declarations ablated;
- CSV equivalents of selected databases;
- self-referential and declared composite relationships;
- small-domain collision cases;
- reused integer-ID domains across unrelated tables;
- generic `id`, `code`, `name`, and `status` columns;
- multiple plausible parent tables;
- orphan, null, duplicate-parent, and cardinality-drift cases;
- paths where safe edges compose into compound fan-out.

Database-level train, calibration, and test splits prevent schema leakage. FK
declarations form one objective label source, but declared-FK recovery is not
presented as general business-relationship accuracy. A separately frozen,
blind-reviewed absent-FK subset measures inferred business relationships.

Reported metrics include:

- declared relationship extraction recall;
- candidate edge precision, recall, and F1;
- `verified` selective precision and coverage;
- precision-versus-coverage curves;
- calibration error when probabilities are exposed;
- path precision and safe-path coverage;
- ambiguity false-promotion rate;
- findings by stable code;
- runtime and peak memory.

Initial release gates are:

- 100 percent extraction of eligible declared relationships;
- at least 98 percent precision among automatically usable inferred edges on
  the frozen held-out set;
- zero automatically usable false edges in the frozen ambiguity and
  small-domain suites;
- no sampled or approximate evidence promoted to `verified`;
- every false positive, false negative, inconclusive case, and unsupported
  source remains in the denominator and report.

Recall and coverage have no minimum release claim until the held-out suite is
large enough to support one. The product may be useful with selective coverage;
it is not useful with silently poor precision.

### 13.2 SQL validation evaluation

The SQL suite covers aliases, CTEs, subqueries, `WHERE` equality joins,
resolvable `USING`, self-joins, declared composite joins, unsupported edges,
grain changes, fan-out, multiple statements, and DDL/DML rejection.

For supported SQL shapes, release requires:

- every normalized final edge is extracted;
- every unsupported edge is blocking;
- every current safe edge is accepted;
- compound path findings match the deterministic validator;
- SQL is never executed during validation;
- repair hints never introduce an unverified edge.

### 13.3 Harness and downstream evaluation

The existing four-arm MCP evaluation is extended with a Harness condition. It
measures:

- tool discovery and call rate;
- successful tool call rate;
- complete-entity planning rate;
- final-SQL validation rate;
- MCP-grounded join rate;
- blocking compliance and bypass rate;
- query-level wrong join rate;
- valid SQL and execution accuracy through an isolated evaluator;
- latency, tokens, and cost.

MCP availability, an MCP call, and final SQL correctness remain separate
metrics. A returned incorrect edge can be grounded but wrong; a guessed correct
edge can be correct but ungrounded.

No cross-model or general product claim is made from the exploratory Spider
pilot. Publication wording names the model, dataset, date, Harness version,
inference policy version, and MCP host.

## 14. Testing

Unit tests cover:

- source discovery boundaries and exclusions;
- declared PK, FK, unique, self, and composite extraction;
- name normalization and generic-name penalties;
- every hard inference gate and boundary value;
- competing-candidate ambiguity;
- evidence and cache canonicalization;
- automatic stale refresh and source-change failure;
- deterministic graph ordering and multi-entity composition;
- SQL extraction and statement rejection;
- strict MCP schemas, response limits, and error sanitization;
- absence of raw rows, paths, credentials, and SQL in persistent evidence.

Integration tests start the real STDIO server against fresh SQLite and CSV
fixtures, call both tools, mutate the source between calls, and verify that the
server refreshes affected evidence without mutating the project.

Property tests generate schemas and value domains to check invariants:

- an inferred edge is never `verified` when the parent is non-unique;
- approximate evidence never passes;
- adding a competing equally supported parent cannot increase assurance;
- source or policy changes cannot reuse old passing evidence;
- every passing SQL edge exists in the current auto-usable graph;
- graph and response ordering are deterministic.

## 15. Migration and delivery sequence

The design is delivered in bounded stages:

1. **Metadata-first graph:** extract declared SQLite keys and relationships,
   including self and composite relationships, and import legacy curated models.
2. **Inference policy:** add normalized names, ambiguity features, exact hard
   gates, frozen evaluation data, and versioned calibration artifacts.
3. **Automatic evidence runtime:** add constrained source discovery, targeted
   profiling, external content-addressed cache, and automatic refresh.
4. **Task-oriented MCP:** implement `get_join_plan` and parse-only
   `validate_sql`, retaining compatibility aliases temporarily.
5. **Lightweight Harness:** package server instructions, host-specific guidance,
   one-command setup, and health checks.
6. **Evaluation:** run free inference, SQL, protocol, safety, performance, and
   mock-agent gates before any paid or public downstream evaluation.

Each stage must preserve the read-only source boundary and may ship only when
its targeted tests pass. The semantic reranker, remote databases, inferred
composite keys, query-log evidence, Web UI, and execution proxy remain separate
future decisions.

## 16. Completion criteria

The zero-configuration Join Harness is complete when:

1. a fresh supported project can start the MCP without JoinLint project files;
2. eligible declared relationships are available without a value scan;
3. inferred relationships become auto-usable only under the versioned exact
   policy;
4. ambiguous candidates remain unavailable to automatic planning;
5. `get_join_plan` returns deterministic cardinality-aware plans for supported
   entity sets;
6. `validate_sql` parses but never executes final SQL and blocks every
   unsupported final edge;
7. stale evidence refreshes automatically or fails inconclusively;
8. caches and MCP responses contain no raw data or credentials;
9. the one-command Harness setup makes both tools discoverable in supported
   hosts;
10. all inference, SQL, MCP, safety, privacy, and performance release gates
    pass with complete reports;
11. documentation no longer requires `init`, candidate review, acceptance,
    baseline update, or manual scan for normal MCP use.
