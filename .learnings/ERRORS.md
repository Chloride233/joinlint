# Errors

Command failures and integration errors.

---

## [ERR-20260830-010] github_mermaid_iframe_locator_deadline

**Logged**: 2026-08-30T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: docs

### Summary

Immediately after reloading the GitHub Markdown preview, a direct locator
evaluation of the rendered Mermaid iframe exceeded the browser's short selector
deadline even though both frames were already present.

### Error

```text
Timed out evaluating selector article iframe >> nth=0
```

### Context

- The preview had already reported two Mermaid output frames and both expected
  section headings.
- Repeating the same locator evaluation was not useful.

### Suggested Fix

After a GitHub Markdown reload, collect frame rectangles with one read-only
page evaluation, scroll the target frame into view, and then capture the visible
page instead of retrying the short locator deadline.

### Metadata

- Reproducible: no
- Related Files: `docs/current-architecture.md`
- Pattern-Key: browser.github_mermaid_iframe_deadline
- Recurrence-Count: 1
- Last-Seen: 2026-08-30

### Resolution

- **Resolved**: 2026-08-30T00:00:00+08:00
- **Notes**: Both diagrams rendered and were visually inspected after using a
  single page-level rectangle read and normal browser scrolling.

---

## [ERR-20260830-009] markdown_backticks_in_shell_validation

**Logged**: 2026-08-30T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: docs

### Summary

A documentation-validation command placed Markdown fence backticks inside a
double-quoted shell command, so zsh treated them as command substitution before
the intended checks ran.

### Error

```text
zsh: parse error near `|'
zsh: parse error in command substitution
```

### Context

- The failure occurred before the architecture document or test suite was
  evaluated.
- The Markdown content itself was unchanged.

### Suggested Fix

Keep literal Markdown fences out of double-quoted shell arguments. Use a
single-quoted search pattern or inspect the file with a command that does not
interpret backticks.

### Metadata

- Reproducible: yes
- Related Files: `docs/current-architecture.md`
- Pattern-Key: shell.markdown_backtick_substitution
- Recurrence-Count: 1
- Last-Seen: 2026-08-30

### Resolution

- **Resolved**: 2026-08-30T00:00:00+08:00
- **Notes**: Replaced the unsafe validation invocation with fixed, quoted
  searches before retrying the document checks.

---

## [ERR-20260830-008] artifact_scan_cascaded_missing_root

**Logged**: 2026-08-30T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: ci

### Summary

Formal workflows used `always()` for sanitized-artifact scanning, so an
upstream failure that created no artifact directory could produce a second,
misleading scanner error.

### Suggested Fix

Run the scanner only when the verifier checkout succeeded and the expected
artifact tree exists.

### Metadata

- Reproducible: yes
- Related Files: `.github/workflows/formal-evaluation.yml`,
  `.github/workflows/formal-pilot.yml`,
  `.github/workflows/formal-pilot-canary.yml`
- Pattern-Key: eval.artifact_scan.missing_root
- Recurrence-Count: 2
- Last-Seen: 2026-08-30

### Resolution

- **Resolved**: 2026-08-30T00:00:00+08:00
- **Notes**: All public formal artifact scans now require a matching tree; the
  workflow contract test enforces the condition.

---

## [ERR-20260830-007] duplicate_ci_push_pr_notifications

**Logged**: 2026-08-30T00:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: ci

### Summary

Ordinary CI ran once for a feature-branch push and again for the corresponding
pull request update, duplicating both work and red failure notifications.

### Context

- The audit found 256 CI runs for 136 commits: 120 duplicate runs.
- Twenty-eight red runs represented 17 unique failed commits; 11 failed
  commits emitted both push and pull-request failures.

### Suggested Fix

Run push CI only on `main`, use pull-request CI for feature branches, and cancel
superseded runs on the same ref.

### Metadata

- Reproducible: yes
- Related Files: `.github/workflows/ci.yml`,
  `tests/test_workflow_pinning.py`
- Pattern-Key: ci.duplicate_push_pr
- Recurrence-Count: 11
- Last-Seen: 2026-08-30

### Resolution

- **Resolved**: 2026-08-30T00:00:00+08:00
- **Notes**: CI trigger and concurrency contracts now have a regression test.

---

## [ERR-20260830-006] phoenix_container_name_survived_stop

**Logged**: 2026-08-30T15:44:00+08:00
**Priority**: low
**Status**: resolved
**Area**: eval

### Summary

The existing Phoenix container was not configured for auto-removal, so stopping
it did not release its name before the sanitized review database restart.

### Error

```text
Conflict. The container name "/joinlint-phoenix" is already in use
```

### Suggested Fix

Inspect the stopped container identity and `AutoRemove` flag, remove only that
container shell after its data bind is separated, then restart with `--rm`.

### Metadata

- Reproducible: yes
- Related Files: `benchmarks/formal_eval/README.md`

### Resolution

- **Resolved**: 2026-08-30T15:45:00+08:00
- **Notes**: Verified the exact stopped Phoenix 20.2.1 container, removed it, and restarted the loopback-only service with `--rm`.

---

## [ERR-20260830-005] in_app_browser_edge_button_click

**Logged**: 2026-08-30T15:48:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

The in-app browser located Phoenix's right-edge annotation button but could not
dispatch a mouse event to its reported coordinates.

### Error

```text
Unable to translate Input.dispatchMouseEvent: No element found at point
```

### Context

- The button remained visible and enabled in the DOM snapshot.
- The failure affected only the optional annotation-form interaction check.

### Suggested Fix

Use keyboard activation for right-edge controls after locating them, and keep
the DOM snapshot as the acceptance source.

### Metadata

- Reproducible: yes
- Related Files: `benchmarks/formal_eval/phoenix_review.py`

### Resolution

- **Resolved**: 2026-08-30T15:48:00+08:00
- **Notes**: Retried the control through keyboard activation without creating an annotation.

---

## [ERR-20260830-004] in_app_browser_visibility_docs_shape

**Logged**: 2026-08-30T15:46:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

The visibility capability exposes its usage note through `info.description`,
not a callable `docs()` method.

### Error

```text
(intermediate value).docs is not a function
```

### Suggested Fix

Read the capability object before invoking `set(true)`.

### Metadata

- Reproducible: yes
- Related Files: `benchmarks/formal_eval/phoenix_review.py`

### Resolution

- **Resolved**: 2026-08-30T15:46:00+08:00
- **Notes**: Read `info.description` and retained background visibility until final delivery.

---

## [ERR-20260830-003] in_app_browser_networkidle

**Logged**: 2026-08-30T15:36:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

The in-app browser rejected the documented `networkidle` load-state option during local UI QA.

### Error

```text
playwright_wait_for_load_state does not support networkidle
```

### Context

- The Phoenix page navigation itself succeeded.
- The failure was limited to the requested wait condition.

### Suggested Fix

Use the supported `domcontentloaded` state followed by a fresh DOM snapshot.

### Metadata

- Reproducible: yes
- Related Files: `benchmarks/formal_eval/phoenix_review.py`

### Resolution

- **Resolved**: 2026-08-30T15:36:00+08:00
- **Notes**: Switched the UI acceptance check to `domcontentloaded` plus DOM inspection.

---

## [ERR-20260830-001] local_codex_exec_sandbox_state

**Logged**: 2026-08-30T11:36:42+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary

A local Codex CLI evaluation probe could not start inside the managed filesystem sandbox because the CLI needs writable state under the existing Codex home.

### Error

```text
failed to open state_5.sqlite: attempt to write a readonly database
failed to initialize in-process app-server client: Operation not permitted
```

### Context

- The probe used `codex exec --ephemeral` with a read-only task sandbox.
- The outer managed sandbox still made the Codex state database read-only.
- No credential or token value was read or logged.

### Suggested Fix

Run the bounded `codex exec` process through the normal sandbox-escalation path while keeping the nested task sandbox read-only and the session ephemeral.

### Metadata

- Reproducible: yes
- Related Files: local Codex state database

### Resolution

- **Resolved**: 2026-08-30T11:36:42+08:00
- **Notes**: The identical sandbox-external probe returned `LOCAL_CODEX_OK` using the existing local login state.

---

## [ERR-20260830-002] local_codex_mcp_approval_cancellation

**Logged**: 2026-08-30T11:51:32+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary

Direct local Codex evaluation runs cancelled fixed MCP calls even with `approval_policy="never"` until the nested CLI approval boundary was explicitly bypassed.

### Error

```text
user cancelled MCP tool call
```

### Context

- The first treatment calibration exposed only the evaluation database and JoinLint MCP servers.
- Shell, apps, plugins, computer use, workspace dependencies, and multi-agent features were disabled.
- A separate startup failure occurred until the evaluation-only validation marker was pre-created; the final unguarded local design does not use that marker.

### Suggested Fix

For this bounded runner, use an ephemeral Codex process with all unrelated tools disabled, an isolated temporary working directory, and the CLI approval bypass inside the separately controlled outer sandbox escalation.

### Metadata

- Reproducible: yes
- Related Files: `benchmarks/formal_eval/database_mcp.py`, `benchmarks/formal_eval/recording_joinlint_mcp.py`

### Resolution

- **Resolved**: 2026-08-30T11:51:32+08:00
- **Notes**: A treatment calibration completed `get_join_plan`, `validate_sql`, one read-only execution, and one accepted submission using the restricted tool surface.

---

## [ERR-20260829-009] project_python_not_selected

**Logged**: 2026-08-29T20:34:31+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

The repository verification first used a missing `python` alias, then the
dependency-free system `python3` instead of the project's virtual environment.

### Error

```text
zsh:1: command not found: python
ModuleNotFoundError: No module named 'pydantic'
```

### Context

- Both failed commands were read-only public-artifact validations.
- System `python3` is Python 3.9; `.venv/bin/python` is the configured Python
  3.12 environment with the repository dependencies.
- No artifact or repository state was changed by either failed command.

### Suggested Fix

Use `.venv/bin/python` for local repository verification.

### Metadata

- Reproducible: yes
- Related Files: `pyproject.toml`
- Pattern-Key: local.project_python_not_selected
- Recurrence-Count: 2
- Last-Seen: 2026-08-30

### Resolution

- **Resolved**: 2026-08-29T20:34:31+08:00
- **Notes**: Continued with `.venv/bin/python`; the failure recurred during the
  2026-08-30 ledger audit and the same correction was applied.

---

## [ERR-20260829-010] ripgrep_short_option_misread

**Logged**: 2026-08-29T20:49:34+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

`rg -L` was assumed to mean files without matches, but this installed ripgrep
uses it as the short form of `--follow`.

### Error

```text
ERROR: file or directory not found: tests/test_agent_join_scorers.py:pytest.importorskip(inspect_ai)
```

### Context

- The command was selecting non-Inspect test files for local verification.
- Pytest exited before running any tests.

### Suggested Fix

Use the unambiguous `rg --files-without-match` spelling.

### Metadata

- Reproducible: yes
- Related Files: `tests/`

### Resolution

- **Resolved**: 2026-08-29T20:49:34+08:00
- **Notes**: The corrected command completed with 521 passing tests.

---

## [ERR-20260829-011] draft_pr_body_sensitive_payload_rejected

**Logged**: 2026-08-29T20:54:10+08:00
**Priority**: medium
**Status**: blocked
**Area**: infra

### Summary

The external approval reviewer rejected a Draft PR body update that repeated
detailed evaluation and campaign-accounting values.

### Error

```text
Rejected: external PR update contained evaluation and financial ledger details
without payload-specific disclosure approval.
```

### Context

- The Git branch push and CI dispatch had already succeeded.
- The rejected operation did not change the Draft PR body or repository data.
- No alternate publishing path was attempted.

### Suggested Fix

Keep the existing Draft PR body unchanged unless the user explicitly approves
publishing that exact result-and-accounting summary there.

### Metadata

- Reproducible: unknown
- Related Files: `docs/opaque-relationship-effect-results.md`

---

## [ERR-20260829-008] ambiguity_reservation_mode_missing_from_cli

**Logged**: 2026-08-29T00:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: eval

### Summary
The first opaque-relationship formal dispatch passed the frozen-input,
calibration, and budget-envelope checks but stopped before reservation because
the new admission mode was absent from the CLI argument choices.

### Error

```text
argument --mode: invalid choice: 'pilot_stage_contract_ambiguity'
```

### Context

- GitHub Actions run `33249211372` failed before a ledger reservation or model
  call, so it created no provider spend.
- The policy table and workflow mapping already admitted the mode; the
  separately maintained argparse list had drifted.

### Suggested Fix

Keep the CLI choice list covered by a command-boundary regression test whenever
a reservation policy mode is added.

### Metadata

- Reproducible: yes
- Related Files: `benchmarks/formal_eval/campaign_reservation.py`,
  `tests/test_formal_eval_campaign_reservation.py`
- Pattern-Key: eval.reservation.cli_policy_drift
- Recurrence-Count: 2
- Last-Seen: 2026-08-29

### Resolution

- **Resolved**: 2026-08-29T00:00:00+08:00
- **Notes**: The CLI choice list now derives from the policy table, and workflow
  parity plus every enabled CLI mode are covered by regression tests.

---

## [ERR-20260829-006] resume_reservation_mode_missing_from_cli

**Logged**: 2026-08-29T00:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: eval

### Summary
The first infrastructure-resume dispatch passed every source-binding check but
stopped before reservation because the new policy mode was absent from the CLI
argument choices.

### Error

```text
argument --mode: invalid choice: 'pilot_stage_contract_safety_resume'
```

### Context

- GitHub Actions run `33245167866` failed before a ledger reservation or model call.
- The policy and workflow input binding existed, but the separately maintained
  argparse choice list and its CLI coverage had not been updated.

### Suggested Fix

Add the resume mode to the CLI choices and test the command boundary directly,
not only the underlying policy table.

### Metadata

- Reproducible: yes
- Related Files: `benchmarks/formal_eval/campaign_reservation.py`,
  `tests/test_formal_eval_campaign_reservation.py`
- Pattern-Key: eval.reservation.cli_policy_drift
- Recurrence-Count: 2
- Last-Seen: 2026-08-29

### Resolution

- **Resolved**: 2026-08-29T00:00:00+08:00
- **Notes**: The CLI choice list now derives from the policy table, and workflow
  parity plus every enabled CLI mode are covered by regression tests.

---

## [ERR-20260829-004] macos_tar_extended_attributes

**Logged**: 2026-08-29T00:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: infra

### Summary
The first query-contract calibration stopped before reservation because the macOS-built tar archive carried provenance extended attributes.

### Error

```text
ValueError: required input is not frozen: databases/._education_v2.sqlite
```

### Context

- GitHub Actions run 33241878997 failed at frozen-input verification.
- Budget reservation and all model steps were skipped, so no provider spend was created.
- The PAX headers contained `com.apple.provenance`; Linux extraction materialized an AppleDouble sidecar not present in the input lock.

### Suggested Fix

Build release archives with the repository's deterministic archive helper,
then download and verify the uploaded asset before dispatch.

### Metadata

- Reproducible: yes
- Related Files: `benchmarks/formal_eval/release_archive.py`,
  `tests/test_formal_eval_release_archive.py`
- Pattern-Key: eval.archive.appledouble_xattr
- Recurrence-Count: 2
- Last-Seen: 2026-08-29

### Resolution

- **Resolved**: 2026-08-29T00:00:00+08:00
- **Notes**: The first asset was replaced and verified. After the same class
  recurred in run `33248395766`, a deterministic helper and AppleDouble/xattr
  rejection tests replaced the manual tar recipe.

---

## [ERR-20260829-003] disabled_calibration_workflow_dispatch

**Logged**: 2026-08-29T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
GitHub rejected the first calibration dispatch because the workflow was disabled.

### Error

```text
HTTP 422: Cannot trigger a workflow_dispatch on a disabled workflow
```

### Context

- No Actions run, campaign reservation, or provider spend was created.
- Paid workflows are intentionally disabled between approved runs.

### Suggested Fix

Enable only `formal-pilot-canary.yml`, confirm its active state, and retry the identical dispatch inputs.

### Metadata

- Reproducible: yes
- Related Files: `.github/workflows/formal-pilot-canary.yml`

### Resolution

- **Resolved**: 2026-08-29T00:00:00+08:00
- **Notes**: The retry path enables only the approved calibration workflow.

---

## [ERR-20260829-002] local_inspect_ai_import_hang

**Logged**: 2026-08-29T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
The combined local test command appeared to stall before the first Inspect
mock evaluation on macOS.

### Error

```text
KeyboardInterrupt while Inspect was loading installed extension/provider modules
```

### Context

- The first attribution to the Textual display import was incorrect: a complete
  traceback placed the delay in Inspect startup and provider initialization.
- The old `.venv` contained both local `eval` and workflow-only `remote` extras.
  Inspect scans installed `inspect_ai` entry points before the first task, which
  loaded `inspect-sandboxes`, `inspect-swe`, and their provider dependencies.
- A clean `dev,eval` environment reduced each representative Inspect path from
  about 70 seconds to about 6.6 seconds.

### Suggested Fix

Keep local mock evaluation in a clean `dev,eval` environment. Install `remote`
only in the formal workflow image or a separate remote preflight environment.

### Metadata

- Reproducible: yes
- Related Files: `tests/test_agent_join_arm_isolation.py`,
  `tests/test_formal_eval_guidance_diagnostic.py`,
  `tests/test_formal_eval_inspect_smoke.py`, `pyproject.toml`
- Pattern-Key: local.inspect_ai_import_hang
- Recurrence-Count: 6
- Last-Seen: 2026-08-30

### Resolution

- **Resolved**: 2026-08-30T00:00:00+08:00
- **Notes**: A clean `dev,eval` environment ran the full local suite without
  deselection: 735 passed, 3 skipped in 20.31 seconds.

---

## [ERR-20260829-001] campaign_ledger_python_alias

**Logged**: 2026-08-29T00:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
The campaign-ledger initializer used an unavailable unversioned `python` command.

### Error

```text
zsh: command not found: python
```

### Context

- The failed command exited before importing project code or contacting GitHub.
- This checkout provides the required Python 3.12 runtime at `.venv/bin/python`.

### Suggested Fix

Use `.venv/bin/python` for repository Python commands in this local workspace.

### Metadata

- Reproducible: yes
- Related Files: `benchmarks/formal_eval/campaign_ledger.py`
- Pattern-Key: local.python_alias_missing
- Recurrence-Count: 2
- Last-Seen: 2026-08-30

### Resolution

- **Resolved**: 2026-08-29T00:00:00+08:00
- **Notes**: Switched both retries to the repository virtual environment. The
  second recurrence occurred during architecture-document validation on
  2026-08-30.

---

## [ERR-20260828-002] gh_api_zsh_query_string

**Logged**: 2026-08-28T03:39:19Z
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
An unquoted GitHub API endpoint containing `?` was expanded as a zsh glob.

### Error

```text
zsh: no matches found: endpoint?recursive=1
```

### Context

- A read-only `gh api` tree request used a query string without shell quotes.
- The first independent ref lookup succeeded; the tree lookup never ran.

### Suggested Fix

Single-quote every `gh api` endpoint containing `?` or `&`.

### Metadata

- Reproducible: yes
- Related Files: `.learnings/ERRORS.md`

---

## [ERR-20260828-001] git_add_permission_review

**Logged**: 2026-08-28T03:29:34Z
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary
Repository staging could not start because the managed approval review timed out twice.

### Error

```text
The automatic permission approval review did not finish before its deadline.
```

### Context

- `git add` needs write access to the sandbox-read-only `.git` directory.
- The normal escalated execution path was retried once and timed out both times.
- No Git write operation ran; workspace source changes remain intact.

### Suggested Fix

Retry after the approval service recovers or obtain an explicit interactive approval; do not bypass the repository permission boundary.

### Metadata

- Reproducible: no
- Related Files: `.git/index`

---

## [ERR-20260829-005] formal_stage_aborted_on_isolated_infrastructure_failure

**Logged**: 2026-08-29T00:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: eval

### Summary
The formal effect stage aborted after one isolated zero-token infrastructure
failure even though the scorer had already preserved that sample as an
intention-to-treat zero.

### Error

```text
RuntimeError: pilot batch contains an infrastructure failure
```

### Context

- GitHub Actions run `33242737801` completed one of four batches.
- Nine control samples succeeded and one control sample recorded
  `INFRASTRUCTURE_FAILURE`; no treatment batch ran.
- Calibration must reject any infrastructure failure, but an effect stage must
  retain isolated failures in its denominator.

### Suggested Fix

Allow isolated lifecycle infrastructure failures only in formal effect stages;
continue to reject incomplete and systemic batches and keep calibration strict.

### Metadata

- Reproducible: yes
- Related Files: `benchmarks/formal_eval/pilot_dispatch.py`,
  `benchmarks/formal_eval/pilot_stage.py`

### Resolution

- **Resolved**: 2026-08-29T00:00:00+08:00
- **Notes**: Commit `bc2bc0fdc8535831101658ddb1f302aabc09561a`
  passed full Linux CI in runs `33243609433` and `33243612750`.

---
## [ERR-20260829-007] pilot_source_manifest_bound_random_staging_path

**Logged**: 2026-08-29T18:36:00+08:00
**Priority**: high
**Status**: resolved
**Area**: eval

### Summary

Two schema-v8 Pilot freezes built from identical inputs produced different
input locks because the carried source `agent_tasks` digest included a random
staging-directory name in each task's pre-normalization database path.

### Error

```text
identical task, manifest, registration, and database bytes; different
source-manifest.json agent_tasks.sha256, input-lock, lineage, and run-plan
```

### Context

- The final `agent-tasks.json` already normalized every database path.
- `source-manifest.json` retained the earlier source-builder file record,
  although that temporary file was not part of the frozen bundle.
- A single successful `pilot verify` therefore did not prove reproducibility.

### Suggested Fix

Bind the source manifest to the final frozen task and manifest records, then
require two independent full-bundle builds to match byte for byte.

### Metadata

- Reproducible: yes
- Related Files: `benchmarks/formal_eval/pilot.py`,
  `tests/test_formal_eval_pilot.py`

### Resolution

- **Resolved**: 2026-08-29T18:36:00+08:00
- **Notes**: Final bundle construction now has a byte-for-byte double-build
  regression before release upload.

---
