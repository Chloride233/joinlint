# Learnings

Corrections, insights, and knowledge gaps captured during development.

**Categories**: correction | insight | knowledge_gap | best_practice

---

## [LRN-20260830-003] best_practice

**Logged**: 2026-08-30T00:00:00+08:00
**Priority**: high
**Status**: promoted
**Area**: tests

### Summary

Keep local mock evaluation and remote-host execution dependencies in separate
virtual environments.

### Details

Inspect loads installed extension entry points at evaluation startup. Mixing
the `remote` extra into the local `dev,eval` environment caused macOS to load
unused sandbox and provider packages before a mock task, making a six-second
test appear hung for roughly 70 seconds. A clean `dev,eval` environment ran the
full suite in about 20 seconds without disabling Inspect tests.

### Suggested Action

Use `dev,eval` for local smoke and diagnostic work. Install `remote` only in the
formal workflow image or a separate environment.

### Metadata

- Source: simplify-and-harden
- Related Files: `pyproject.toml`, `AGENTS.md`, `benchmarks/formal_eval/README.md`
- Tags: inspect, environment, evaluation, macOS
- Pattern-Key: eval.separate_local_and_remote_dependencies
- Recurrence-Count: 6
- First-Seen: 2026-08-29
- Last-Seen: 2026-08-30

### Resolution

- **Promoted**: 2026-08-30T00:00:00+08:00
- **Notes**: The environment boundary is documented in project rules,
  benchmarking guidance, and the release checklist.

---

## [LRN-20260830-002] best_practice

**Logged**: 2026-08-30T00:00:00+08:00
**Priority**: high
**Status**: promoted
**Area**: evaluation

### Summary

An evaluation failure is not resolved by a successful retry; it is resolved
when the root cause has one stable identity and a deterministic guard.

### Details

The audit found repeated archive pollution, reservation-mode drift, duplicate
CI notifications, and secondary artifact-scan errors. Per-run notes made the
history look larger without preventing recurrence. Grouping incidents by
`Pattern-Key` exposes recurrence and keeps product outcomes separate from
infrastructure, admission, and operator failures.

### Suggested Action

Before retrying, update `docs/evaluation-ledger.md`, reuse or create the root
cause's `Pattern-Key`, and add the smallest regression guard that would have
stopped the repeated operation.

### Metadata

- Source: incident_audit
- Related Files: `docs/evaluation-ledger.md`, `.learnings/ERRORS.md`, `AGENTS.md`
- Tags: evaluation, CI, root-cause, regression
- Pattern-Key: eval.guard_before_retry
- Recurrence-Count: 4
- First-Seen: 2026-08-29
- Last-Seen: 2026-08-30

### Resolution

- **Promoted**: 2026-08-30T00:00:00+08:00
- **Notes**: The retry discipline is now in project `AGENTS.md` and the release
  checklist.

---

## [LRN-20260830-001] correction

**Logged**: 2026-08-30T11:19:14+08:00
**Priority**: high
**Status**: resolved
**Area**: architecture

### Summary

Strengthen the product value of JoinLint's focused two-tool MCP without turning it into an in-product semantic platform.

### Details

The user corrected that the goal is not minimalism for its own sake: it is to make the current product value stronger without adding a QueryContract framework, semantic registry, SQL compiler, execution orchestration, or receipt subsystem. Reuse the current `entity_refs`, `expected_grain_ref`, scope disclaimers, and fail-closed behavior, then improve proof, positioning, and user-visible usefulness around them.

### Suggested Action

Prioritize evidence and product-value improvements around the current contract: clearer before/after scenarios, stronger harness acceptance, and concise positioning. Add a subsystem only after a concrete integration repeatedly proves that the existing two-tool contract is insufficient.

### Metadata

- Source: user_feedback
- Related Files: README.md, examples/harness/joinlint-agent-instructions.md
- Tags: scope, MCP, YAGNI
- Pattern-Key: simplify.joinlint_product_boundary
- Recurrence-Count: 2
- First-Seen: 2026-08-30
- Last-Seen: 2026-08-30

### Resolution

- **Resolved**: 2026-08-30T11:24:03+08:00
- **Notes**: Roadmap synthesis now centers on proving and amplifying existing product value within the two-tool boundary; no product code changed.

---
