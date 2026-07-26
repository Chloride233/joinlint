# JoinLint Stage 1 Proof MCP Implementation Plan

> **For agentic workers:** Execute this plan in the current session by default, task by task. Use checkbox (`- [x]`) syntax for tracking. Use subagents only when the user explicitly requests delegation or parallel agent work.

**Goal:** Deliver a SQLite-only Developer Preview whose default STDIO MCP exposes exactly `get_join_plan` and `validate_sql`, produces deterministic physical Join Proofs from current declared/curated evidence, and never executes user SQL.

**Architecture:** Add a focused `joinlint.runtime` package over the existing `SafeProject` and SQLite snapshot boundary. Keep CLI schema v1 and manual governance services intact; use separate strict MCP schema v2 models, immutable relationship/evidence/proof identities, evidence-derived authorization and proof lifecycle, an exact graph planner, and SQLGlot semantic join validation.

**Tech Stack:** Python 3.12, Pydantic v2, FastMCP STDIO, SQLGlot 30.13, RFC 8785 canonical JSON, SQLite read-only snapshots, pytest, Hypothesis, Ruff.

## Global Constraints

- Stage 1 supports SQLite only and never auto-uses inferred `strong` candidates.
- Explicit `--source` paths take precedence; bounded auto-discovery runs only when none are supplied.
- Default MCP exposes exactly `get_join_plan` and `validate_sql`; legacy CLI commands remain available.
- `ref` is request-local and never enters relationship or evidence identity.
- JoinLint validates only physical joins; every public response and document states `JoinLint proof != query correctness`.
- No user SQL execution, project writes, raw values, absolute paths, or credentials in responses or cache.
- Preserve all unrelated existing worktree changes; do not stage or commit automatically from this dirty checkout.

---

### Task 1: Strict domain models and canonical identities

**Files:**
- Create: `src/joinlint/runtime/__init__.py`
- Create: `src/joinlint/runtime/domain.py`
- Create: `tests/test_runtime_domain.py`

**Interfaces:**
- Produces: `SourceIdentity`, `SourceSnapshot`, `Endpoint`, `EntityDefinition`, `RelationshipDefinition`, `EvidenceMeasurements`, `EvidenceRecord`, `AuthorizationProjection`, `EntityRef`, `ProofEdge`, `JoinProof`, `ProofLifecycleProjection`, `NormalizedJoinEdge`, `NormalizedJoinGraph`.
- Produces: `relationship_id_for(...)`, `evidence_id_for(...)`, `plan_id_for(...)`, and stable scope constants.

- [x] Write failing tests showing relationship IDs ignore provenance, snapshot, policy, refs, and timestamps; evidence IDs change with snapshot/measurements; plan IDs change with policy/request refs/proof graph.
- [x] Add frozen strict Pydantic models with bounded identifiers, ordered endpoint tuples, SHA-256 validation, and validators for unique refs and composite endpoint arity.
- [x] Implement all three IDs with `canonical_json`, explicit hash-domain prefixes, full lowercase SHA-256, and canonical UTF-8 sorting.
- [x] Add tests proving proof lifecycle is a derived `current|stale|unverifiable` projection and proof records never mutate.
- [x] Run `python -m pytest tests/test_runtime_domain.py -q` and require all tests to pass.

### Task 2: SQLite source discovery, metadata, snapshots, and cache

**Files:**
- Create: `src/joinlint/runtime/sources.py`
- Create: `src/joinlint/runtime/cache.py`
- Create: `tests/test_runtime_sources.py`
- Create: `tests/test_runtime_cache.py`

**Interfaces:**
- Consumes: `SafeProject`, existing SQLite snapshot behavior, Task 1 domain models.
- Produces: `locate_sqlite_sources(root, explicit_sources, auto)`, `snapshot_sqlite(identity)`, `extract_sqlite_catalog(snapshot)`, and `RuntimeCache` methods `load_evidence`, `store_evidence`, `load_proof`, `store_proof`, `locked`.

- [x] Write failing source tests for explicit-source precedence, root-level and `data|datasets` discovery at depth <=2, ignored hidden/vendor/cache directories, symlink refusal, unsupported extension, and multi-source stable IDs.
- [x] Implement source identity as a digest of the descriptor-resolved trusted boundary, source kind, and project-relative locator; never serialize the absolute root.
- [x] Reuse or adapt the existing consistent SQLite snapshot logic so `snapshot_id` covers schema plus committed main/WAL state and detects source changes during copy.
- [x] Extract tables, columns, PK/unique constraints, declared single/self/composite FKs into deterministic catalog models without reading table rows.
- [x] Write failing cache tests for mode `0700`, atomic replacement, digest validation, corrupted-record removal, no absolute paths/raw values, and per-key cross-process locking.
- [x] Implement `RuntimeCache` under the platform user cache root with schema-v2 JSON and content-addressed evidence/proof records.
- [x] Run the focused source/cache tests and require all tests to pass.

### Task 3: Declared/curated evidence runtime and authorization

**Files:**
- Create: `src/joinlint/runtime/evidence.py`
- Create: `tests/test_runtime_evidence.py`

**Interfaces:**
- Consumes: source catalog/snapshot, optional legacy `ModelV1`, `RuntimeCache`.
- Produces: `relationship_definitions(catalog, legacy_model)`, `verify_relationship(snapshot, catalog, definition)`, `authorize(record, current_snapshot_id, policy_version)`, `build_authorized_graph(...)`.

- [x] Write failing tests for declared FK, self FK, ordered composite FK, current legacy curated relationship import, and rejection of stale/cross-source/missing legacy endpoints.
- [x] Implement stable definitions where relationship identity excludes provenance and curated/declared sources may support the same physical definition.
- [x] Implement exact aggregate verification for parent uniqueness, child inclusion/orphans, null counts, observed cardinality, and maximum multiplicity using internally generated quoted SQLite queries only.
- [x] Emit stable blocking findings for non-unique parent, orphan, cardinality drift, many-to-many, and source-change/resource failures.
- [x] Derive authorization from evidence mode, snapshot equality, policy version, and blocking findings; do not persist `current/stale/usable` on relationship definitions.
- [x] Add a query-spy test proving the exact user SQL string is never passed into the SQLite evidence executor.
- [x] Run `python -m pytest tests/test_runtime_evidence.py -q` and require all tests to pass.

### Task 4: Deterministic planner, grain compatibility, and proof lifecycle

**Files:**
- Create: `src/joinlint/runtime/planner.py`
- Create: `tests/test_runtime_planner.py`

**Interfaces:**
- Consumes: authorized relationship/evidence graph and `EntityRef` request nodes.
- Produces: `plan_join(entity_refs, start_ref, expected_grain_ref, max_depth, include_alternatives, graph) -> JoinProof`, `proof_lifecycle(proof, runtime) -> ProofLifecycleProjection`, and `grain_compatible(...)`.

- [x] Write failing planner tests for normal, self, composite, multi-hop, disconnected, ambiguous, max-depth, deterministic path ordering, and default-hidden alternatives.
- [x] Implement graph search over request-local refs while resolving physical entity definitions separately; never treat a ref as entity identity.
- [x] Compose edge multiplicities and reject many-to-many or compound fan-out before producing a proof.
- [x] Implement grain-key propagation: expected grain must remain guaranteed non-null and unique; LEFT compatibility is derived from preserved side, uniqueness, and observed multiplicity rather than traversal direction.
- [x] Implement lifecycle checks for source snapshot, policy version, evidence availability and authorization; missing proof returns `PROOF_NOT_AVAILABLE`, not a fabricated lifecycle.
- [x] Canonicalize proof edges and compute `plan_id` only after the proof is complete; cache the immutable proof.
- [x] Run `python -m pytest tests/test_runtime_planner.py -q` and require all tests to pass.

### Task 5: Semantic SQL graph validation

**Files:**
- Create: `src/joinlint/runtime/sql.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `tests/test_runtime_sql.py`

**Interfaces:**
- Produces: `normalize_sql_graph(sql, catalog, source_id) -> NormalizedJoinGraph` and `validate_sql_graph(graph, authorized_graph, proof=None, expected_grain_ref=None) -> SQLValidationOutcome`.

- [x] Move `sqlglot==30.13.0` from eval-only use into the base runtime dependency and refresh the lockfile.
- [x] Write failing tests for aliases, CTEs, subqueries, self joins, composite joins, INNER reordering, equality-side reversal, WHERE equality, and equivalent semantic graphs.
- [x] Parse exactly one bounded SQLite SELECT/WITH and reject parse failures as errors; represent DDL/DML, multiple statements, RIGHT/FULL/CROSS/NATURAL and non-equality joins as blocking findings.
- [x] Resolve every table instance to a request-local ref and every equality to physical endpoint pairs; group complete composite predicates as one relationship.
- [x] Require exact graph equivalence when a proof is supplied; never downgrade stale/unavailable proof validation to independent lint.
- [x] Validate independent graphs against only current authorized edges, full-path fan-out, and grain compatibility; return repair proof references rather than rewritten SQL.
- [x] Run `python -m pytest tests/test_runtime_sql.py -q` and require all tests to pass.

### Task 6: Strict two-tool MCP, CLI migration, formal evaluation, Harness, and release docs

**Files:**
- Create: `src/joinlint/mcp_contracts.py`
- Modify: `src/joinlint/mcp_server.py`
- Modify: `src/joinlint/cli.py`
- Modify: `benchmarks/formal_eval/trace.py`
- Modify: `benchmarks/formal_eval/oracle_mcp.py`
- Modify: `README.md`
- Test: `tests/test_mcp.py`, `tests/test_security_regressions.py`, `tests/test_formal_eval_*.py`

**Interfaces:**
- Exposes exactly `get_join_plan(entity_refs, start_ref, expected_grain_ref, max_depth, include_alternatives)` and `validate_sql(sql, dialect, source_id, plan_id, expected_grain_ref)`.
- CLI becomes `joinlint serve-mcp --project ROOT [--source RELATIVE.sqlite ...] [--auto/--no-auto]` with auto enabled only when no explicit source is supplied.

- [x] Define strict schema-v2 request/response models, stable error/finding codes, proof lifecycle, evidence freshness, `validated_scope`, `not_validated_scope`, and `execution_count: 0`.
- [x] Replace the default three-tool server with the two-tool runtime while leaving manual CLI service functions untouched and unexposed through MCP.
- [x] Add concise server/tool instructions requiring the two-call Harness and stating `JoinLint proof != query correctness`.
- [x] Update protocol tests to require exactly two tools, response size <=1 MiB, unknown-field rejection, sanitized errors, no project writes, and no inherited provider credentials.
- [x] Update formal-eval trace/oracle models to the final proof schema, semantic graph fields, lifecycle, scopes, and execution attestation.
- [x] Add Codex/Claude project configuration examples and Cursor compatibility smoke documentation without installing or executing a database MCP.
- [x] Add the Developer Preview claim boundary, SQLite-only support, inference-disabled status, and third-party database MCP workflow to README/release docs.
- [x] Implement determinism acceptance tests: two fresh processes, cold/warm caches, 10 repeats, entity-ref permutations, alias/join/equality permutations; compare all stable fields and mask only documented timestamps/performance.
- [x] Run `python -m pytest tests/test_runtime_*.py tests/test_mcp.py tests/test_security_regressions.py tests/test_formal_eval_*.py -q`.
- [x] Run the full suite, Ruff, workflow YAML parse, fake-model pipeline, runtime two-tool check, and `git diff --check`; require every check to pass.

## Completion Audit

- [x] Default runtime exposes exactly two tools and succeeds against a fresh SQLite root with no `.joinlint` files.
- [x] Declared and valid curated relationships produce current exact proofs; no inferred candidate can be auto-used.
- [x] Proof IDs, lifecycle, SQL semantic matching, and grain compatibility meet the frozen contract.
- [x] Project contents remain unchanged across MCP calls; cache and responses contain no forbidden data.
- [x] Public documentation and reports state that proof is not query correctness and label the release Developer Preview.
- [x] All targeted and full verification commands pass with no skipped Stage 1 acceptance test.
