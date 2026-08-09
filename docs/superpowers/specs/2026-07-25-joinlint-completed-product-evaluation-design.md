# JoinLint Completed-Product Formal Evaluation Design

**Date:** 2026-07-25

**Status:** Evaluation framework implemented; formal data and completed MCP runtime pending

**Product contract:** Local, read-only Join Safety MCP exposing
`get_join_plan` and `validate_sql`

## 1. Decision and claim boundary

The evaluation produces three independent decisions: relationship-evidence
release, SQL-validation release, and Agent product-effect publication. It does
not emit one blended accuracy score.

The confirmatory primary endpoint is **Join-Correct Task Completion Rate**. A
run succeeds only when it submits parseable SQL whose normalized join graph
matches a frozen equivalent graph and whose exact final SQL passes the isolated
validator. No SQL, refusal when an oracle path exists, timeout, missing edge,
extra edge, parse failure, and validation bypass remain intention-to-treat
failures.

When the oracle has no safe path, an explicit no-SQL abstention is a successful
safety outcome. When the oracle has a safe path but JoinLint is inconclusive,
the run is a coverage miss and task failure, not a dangerous acceptance.

Join-correct means only that physical endpoints, direction, current evidence,
path cardinality, and final SQL joins satisfy this contract. It does not prove
metric definitions, filters, causal meaning, or complete answer correctness.

## 2. Frozen contracts

The schema-v2 preregistration freezes exactly two returned model identities from
one provider family: DeepSeek V4 Pro as the high-capability tier and DeepSeek V4
Flash as the cost-efficient tier, both with thinking disabled. This is a
single-provider, two-tier replication and does not test cross-family
generalization. The preregistration also freezes the official CNY cache-hit,
cache-miss, and output-token prices observed on 2026-07-26; Codex CLI and Claude
Code with exact versions; JoinLint commit, Harness version, inference policy, image
reference and digest, dataset release, seed, repetitions, bootstrap draws, and publication
thresholds; and GitHub Actions plus Modal as the execution environment.

DeepSeek model names are provider aliases unless the response exposes a more
specific immutable identity. All runs record and enforce the returned identity;
an observable identity change invalidates the batch. Silent provider updates
remain a disclosed validity limitation. Formal exports retain cache-read and
cache-write token counts and recompute CNY cost from the frozen prices.

The tracked manifest contains only identifiers and hashes. Raw questions,
schema text, gold SQL, database paths, and allowed graphs live in an ignored
sealed payload. The input lock hashes every sealed file. Preflight fails on
symlinks, hash drift, cross-split variants, exact task duplicates, ambiguous
confirmatory truth, fewer than 12 databases, fewer than 60 tasks, fewer than
five tasks per database, fewer than 20 diagnostics, or a release mismatch.
Near-duplicate IDs are not trusted labels: preflight recomputes them from the
sealed schema, question, and SQL-shape content and rejects mismatches. Every
referenced source path is relative, remains inside the locked root, and is
listed in the input lock.

The minimum run plan is:

```text
60 tasks × 3 repetitions × 2 conditions × 2 models × 2 hosts = 1,440
20 diagnostics × 3 conditions × 2 models × 2 hosts = 240
```

An independent pilot estimates database-level effect variance. A two-sided
normal power calculation sets the database floor for 80 percent power to detect
a 10 percentage-point effect; the minimum remains 12 databases and five tasks
per database. The workflow does not silently expand after viewing results: it
stops before Modal dispatch when the already frozen manifest and exact run plan
do not meet the pilot requirement. Pilot, diagnostic, and confirmatory results
are never pooled.

## 3. Relationship evidence gate

`declared`, `curated`, and `verified` remain separate. Declared edges measure
extraction and current validation. Curated edges measure current support, not
permanent truth. Verified edges measure selective automatic-use precision and
coverage.

The natural corpus requires at least 24 databases across six domains: 12 with
declaration-visible and declaration-ablated variants, and 12 absent-FK
databases with two independent labels and third-person adjudication. All
variants remain in one database-level split. Counts use the database variant
group, so visible/ablated or SQLite/CSV variants cannot inflate coverage.

The adversarial corpus requires at least 240 candidates balanced across same-ID
names, reused integer domains, small-domain overlap, multiple parents,
non-unique parents, orphans, and drift. Natural and adversarial results are
reported separately.

Release requires 100 percent eligible declared extraction recall; at least 250
automatically usable verified edges across at least 20 databases; verified
precision of at least 98 percent and Wilson 95 percent lower bound of at least
95 percent; zero adversarial false promotions; and zero sampled, approximate,
or stale promotions. Coverage and abstention are always reported without a
first-release minimum.

## 4. SQL validation gate

The suite contains at least 200 supported safe controls and 200 dangerous
queries across aliases, CTEs, subqueries, `WHERE` equality, resolvable `USING`,
self joins, declared composite joins, wrong edges, grain changes, many-to-many,
compound fan-out, DDL/DML, and multiple statements.

Release requires zero dangerous queries with a decision other than blocking;
zero false blocks or inconclusive supported controls; 100 percent normalized
edge extraction; zero unsafe repair hints; zero SQL executions by
`validate_sql`; cached p95 at most 500 ms; metadata-only planning at most one
second; and the million-row fixture within 60 seconds and below 1 GiB RSS.
The strict `validate_sql` response must carry `execution_count: 0`; a missing or
nonzero attestation fails closed. This is a runtime audit contract, not
independent system-call tracing, and reports must describe it as such.

## 5. Agent conditions and failure explanation

The naturalistic study uses a frozen BIRD SQLite subset selected without
reading JoinLint output. Tasks require one to four cross-table equality joins,
deterministic SQLGlot parsing, and independently reviewed equivalent graphs.

Control receives the question, schema, and independent read-only database MCP.
Treatment receives identical inputs plus the official Harness and real
JoinLint two-tool MCP. Twenty sealed semantic-join-failure tasks run
oracle-inline, oracle-through-MCP, and JoinLint-without-Harness diagnostics.

Every treatment trace becomes this funnel:

```text
eligible → plan called → plan usable → protocol compliant → SQL grounded → final SQL validated
→ validation passed → join correct → execution correct
```

Failures use only: `TOOL_NOT_CALLED`, `ENTITY_SET_INCOMPLETE`,
`PLAN_INCONCLUSIVE`, `NO_VERIFIED_PATH`, `WRONG_PLAN`, `SQL_NOT_GROUNDED`,
`VALIDATION_NOT_CALLED`, `FINAL_SQL_NOT_VALIDATED`, `VALIDATION_BLOCKED`, `AGENT_BYPASS`,
`SQL_PARSE_FAILED`, `EXECUTION_FAILED`, `MODEL_TIMEOUT`, `MODEL_LIMIT`, `MCP_TOOL_ERROR`,
`GROUND_TRUTH_AMBIGUOUS`, and `INFRASTRUCTURE_FAILURE`.

## 6. Statistics and publication gates

Control and treatment pair on model, host, database, task, and repetition.
Model calls are not independent samples. The primary interval resamples
databases, then tasks, then repetitions while keeping four model/host cells
equally weighted. Cell claims require Holm correction. Missing artifacts stay
in the denominator. There is no interim inspection, sequential stopping, or
result-driven expansion.

A public improvement claim requires completion improvement of at least 10
points with database-clustered 95 percent lower bound above zero; dangerous
submission no worse; execution lower bound at least -5 points; no cell more
than 5 points worse; planning, validation, and grounding each at least 90
percent; blocking compliance at least 95 percent; bypass at most 5 percent;
and tool errors at most 2 percent.

Negative and null effects still produce the complete report but cannot use
improvement language.

## 7. Remote execution and artifacts

The protected `formal-evaluation` GitHub Environment requires explicit paid-run
approval, but approval alone is not a budget boundary. Its paid job is currently
fail-closed until an atomic campaign reservation and access-controlled frozen
input store are wired; free tests and synthetic smoke remain available. When
re-enabled, formal preflight verifies
inputs, rejects any MCP surface other than exactly `get_join_plan` and
`validate_sql`, creates a content-addressed evaluation lineage, creates run and
power plans, enforces power readiness, and only then starts remote work.

The available campaign-ledger primitive reserves an exact micro-CNY full upper
bound by append-only GitHub ref compare-and-swap and treats exact replays as
non-authorizing. It intentionally has no automatic release or settlement path.
Formal evaluation still lacks a frozen per-run upper bound, authoritative
genesis and opening balance, and protected ledger ref. The workflow-owned
admission wrapper deliberately has no formal-evaluation policy; its fixed
readiness, calibration, and Pilot policies plus exact-head receipt verification
cannot authorize this workflow. Its evaluated-commit field is only
caller-attested until a clean workflow-owned checkout and frozen-input verifier
are wired. Formal therefore remains hard-blocked.

Relationship, SQL-validation, and performance rows are generated by the frozen
deterministic runner from the locked raw suite. The resulting bundle records the
suite digest, runner identity, and evaluation lineage; report generation rejects
any mismatch. JoinLint MCP subprocesses receive an allowlisted environment and
never inherit model-provider, Modal, or GitHub credentials.

Inspect AI runs fixed Codex CLI and Claude Code versions through Inspect SWE.
Each sample receives a fresh Modal sandbox from the digest-pinned registry image
and private source. Both conditions
use the same separate `EvaluationDatabase.execute_sql` MCP. JoinLint never
executes SQL or receives model credentials. Oracle diagnostics use a separate
two-tool server backed only by frozen graphs.

Raw Inspect logs remain only on the ephemeral Actions runner and are never
uploaded. Formal evidentiary and paid sanitized artifacts are retained for 90
days only after a strict verifier from a separate, commit-pinned workflow
checkout accepts their profile-specific inventory, typed schema, canonical
duplicate-free finite JSON, private-content boundary, and cross-file
consistency. Markdown must rebuild byte-for-byte from its verified JSON.
Unknown paths, symbolic links, special files, questions, schema text, gold SQL,
source paths, raw rows, keys, and transcripts fail closed. The non-evidentiary
deterministic smoke artifact is outside this stronger contract. This content
gate does not replace source-run metadata attestation or atomic campaign
reservation. The checkout pins validator code provenance but shares the job
runner, so it is not an adversarial process sandbox; re-enablement also requires
an approved target commit or a clean verifier runner.

The Agent export must equal the exact frozen run-plan ID set. The independent
post-run review uses the exact preregistered sample and binds each decision to
the output digest, reviewer digest, Agent-result digest, run-plan digest, and an
arm-label-removal attestation.

## 8. Current readiness

The v2 contracts, lineage-bound deterministic generation, trace grounding,
power and exact-matrix gates, database and oracle MCPs, Inspect SWE task, Modal
workflow, sanitization, blinded-review binding, and reporting are implemented.

The formal batch is intentionally blocked because JoinLint v0.1 still exposes
the legacy three tools. The natural and adversarial corpora, SQL suite, BIRD
payload, independent pilot, frozen models and host binaries, image digest, and
external credentials also remain pending evidence inputs. Synthetic smoke is
not product evidence.
