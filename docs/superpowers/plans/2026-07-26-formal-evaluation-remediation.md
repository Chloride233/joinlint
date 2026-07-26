# Formal Evaluation Evidence-Chain Remediation Implementation Plan

> **For agentic workers:** Execute this plan in the current session by default, task by task. Use checkbox (`- [ ]`) syntax for tracking. Use subagents only when the user explicitly requests delegation or parallel agent work.

**Goal:** Make every formal publication decision reproducible from one frozen evaluation lineage, enforce the pilot-derived sample size and exact run matrix, and score only strict completed-product MCP responses.

**Architecture:** Introduce a shared `EvaluationLineage` digest carried by deterministic evidence, run plans, Agent rows, and blind-review decisions. Generate relationship, SQL-validation, and performance evidence through a registered deterministic runner rather than accepting unbound verdict files. Keep statistical validation and trace parsing pure so they remain testable without network or paid execution.

**Tech Stack:** Python 3.12, Pydantic v2, Inspect AI, Inspect SWE, Modal, SQLGlot, GitHub Actions, pytest, Ruff.

## Global Constraints

- Preserve the current read-only JoinLint product boundary; evaluation database execution remains in the independent evaluation MCP.
- Formal execution remains GitHub Actions plus Modal and requires protected-environment approval.
- The current three-tool MCP must continue to fail the completed two-tool runtime gate.
- Natural, adversarial, diagnostic, pilot, and confirmatory outputs must not be pooled.
- A failed gate must still produce an observed-results report, but must never allow an improvement claim.
- Existing unrelated workspace edits must not be changed or staged.

---

### Task 1: Evaluation lineage and deterministic evidence runner

**Files:**
- Create: `benchmarks/formal_eval/lineage.py`
- Create: `benchmarks/formal_eval/deterministic.py`
- Modify: `benchmarks/formal_eval/contracts.py`
- Modify: `benchmarks/formal_eval/cli.py`
- Modify: `.github/workflows/formal-evaluation.yml`
- Test: `tests/test_formal_eval_lineage.py`

**Interfaces:**
- Consumes: schema-v2 preregistration, manifest, input-lock, target commit, image digest, policy version, raw deterministic-case bundle.
- Produces: `EvaluationLineage`, `DeterministicEvidenceBundle`, `build_lineage(...)`, `verify_lineage(...)`, and `run_deterministic_evidence(...)`.

- [x] Add failing tests proving that a result bundle with a different input-lock, commit, image, policy, or evaluation ID is rejected.
- [x] Add failing tests proving that supplied verdict rows cannot be scored unless produced by the deterministic runner and accompanied by raw-case hashes.
- [x] Implement canonical lineage digest construction and strict equality checks.
- [x] Implement a deterministic runner adapter that invokes the completed runtime for relationship and SQL cases, records measurements, and writes one bound bundle.
- [x] Change `score` and the formal workflow to consume only the generated bundle.
- [x] Run `python -m pytest tests/test_formal_eval_lineage.py tests/test_formal_eval_gates.py -q` and require all tests to pass.

### Task 2: Power, run-plan, result-set, and post-run review gates

**Files:**
- Modify: `benchmarks/formal_eval/contracts.py`
- Modify: `benchmarks/formal_eval/power.py`
- Modify: `benchmarks/formal_eval/run_plan.py`
- Modify: `benchmarks/formal_eval/export.py`
- Modify: `benchmarks/formal_eval/cli.py`
- Modify: `.github/workflows/formal-evaluation.yml`
- Modify: `.github/workflows/formal-evaluation-report.yml`
- Test: `tests/test_formal_eval_power.py`
- Test: `tests/test_formal_eval_run_plan.py`
- Test: `tests/test_formal_eval_remote.py`

**Interfaces:**
- Consumes: `PowerPlan`, `RunPlanV2`, sanitized Inspect rows, per-sample blinded review decisions.
- Produces: `validate_power_readiness(...)`, `validate_result_set(...)`, `BlindReviewBundle`, and `validate_blind_review(...)`.

- [x] Add a failing test where pilot variance requires more databases than the manifest contains.
- [x] Add failing tests for missing, extra, duplicated, and cross-lineage Agent rows.
- [x] Add a failing test showing that aggregate `144/144` review counts without sample decisions cannot unlock publication.
- [x] Enforce manifest counts against the pilot plan before Modal dispatch.
- [x] Enforce exact equality between frozen run-plan sample IDs and exported result IDs.
- [x] Replace aggregate blind-review counts with unique per-sample decisions bound to the formal result digest and require the exact preregistered review sample.
- [x] Run the focused power, run-plan, and remote tests and require all to pass.

### Task 3: Strict MCP response and corpus coverage contracts

**Files:**
- Modify: `benchmarks/formal_eval/contracts.py`
- Modify: `benchmarks/formal_eval/trace.py`
- Modify: `benchmarks/formal_eval/gates.py`
- Modify: `benchmarks/formal_eval/fake.py`
- Test: `tests/test_formal_eval_trace.py`
- Test: `tests/test_formal_eval_gates.py`

**Interfaces:**
- Consumes: completed-product `get_join_plan` and `validate_sql` response envelopes, deterministic-case taxonomy.
- Produces: strict response models, compound-edge normalization, and coverage-gate reports.

- [x] Add failing tests for wrong command/version, blocking plan findings, non-current or non-auto-usable edges, mismatched validation edges, and compound predicates.
- [x] Implement strict response-envelope parsing and compound predicate expansion.
- [x] Add adversarial subtype, reviewer/adjudication, SQL-shape, and SQL-danger taxonomy fields.
- [x] Enforce every preregistered corpus category and report curated relationships separately.
- [x] Require two distinct model families and bind returned provider snapshot IDs rather than aliases where the log exposes them.
- [x] Run trace, contract, and gate tests and require all tests to pass.

### Task 4: Exact matrix validation and shared-cluster bootstrap

**Files:**
- Modify: `benchmarks/formal_eval/gates.py`
- Test: `tests/test_formal_eval_gates.py`

**Interfaces:**
- Consumes: exact paired result rows and a frozen `RunPlanV2`.
- Produces: exact matrix readiness and database-shared hierarchical bootstrap intervals.

- [x] Add a failing test where one model-host-task cell is absent despite global model and host counts remaining two.
- [x] Add a deterministic bootstrap test proving that every draw applies the same sampled database/task/repetition indices to all four cells.
- [x] Replace the existing minimum-shape test with exact run-plan validation.
- [x] Resample databases globally, then tasks and repetitions, and only then aggregate the four cells equally.
- [x] Run `python -m pytest tests/test_formal_eval_gates.py -q` and require all tests to pass.

### Task 5: Workflow safety, documentation, and final verification

**Files:**
- Modify: `.github/workflows/formal-evaluation.yml`
- Modify: `.github/workflows/formal-evaluation-report.yml`
- Modify: `benchmarks/formal_eval/README.md`
- Modify: `docs/benchmarking.md`
- Modify: `docs/release-checklist.md`
- Modify: `docs/superpowers/specs/2026-07-25-joinlint-completed-product-evaluation-design.md`
- Test: `tests/test_formal_eval_remote.py`

**Interfaces:**
- Consumes: all preceding gates.
- Produces: an approval-gated workflow whose commands use environment variables for dispatch inputs and whose final report proves one complete lineage.

- [x] Add workflow tests rejecting direct shell interpolation of dispatch inputs in `run` commands.
- [x] Move workflow inputs into step environment variables and quote shell variables.
- [x] Update documentation to describe deterministic evidence generation, lineage, post-run blind review, and the power/run-plan stop conditions.
- [x] Run `python -m pytest tests/test_formal_eval_*.py -q`.
- [x] Run `python -m pytest -q`.
- [x] Run `python -m ruff check src tests benchmarks scripts` and `git diff --check`.
- [x] Confirm fake-model remains explicitly non-evidentiary and the current three-tool MCP still fails the completed runtime gate.
