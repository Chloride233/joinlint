# Errors

Command failures and integration errors.

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

### Resolution

- **Resolved**: 2026-08-29T00:00:00+08:00
- **Notes**: Added the missing CLI choice and a regression test before retrying.

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

Build release archives with `COPYFILE_DISABLE=1 tar --no-xattrs`, then download the uploaded asset, extract it, and rerun the frozen-input verifier before dispatch.

### Metadata

- Reproducible: yes
- Related Files: `.github/workflows/formal-pilot-canary.yml`

### Resolution

- **Resolved**: 2026-08-29T00:00:00+08:00
- **Notes**: Replaced the Draft Release asset; readback SHA-256 is c9bdcef881b9534a9d0f7f130e3fa1a835756e6a6b434f5cb2f8c38204bcf195 and extracted verification reports ready.

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
The combined local test command stalled while importing `inspect_ai` on macOS.

### Error

```text
KeyboardInterrupt while importing inspect_ai._display.textual
```

### Context

- Forty tests passed before the import stopped making progress.
- The sandbox also denied process-list inspection, so the test was interrupted through its existing execution session.
- The three known local-only Inspect integration tests are exercised by Linux GitHub CI.

### Suggested Fix

Deselect the three Inspect import tests for local verification and require the full GitHub CI suite before paid dispatch.

### Metadata

- Reproducible: yes
- Related Files: `tests/test_formal_eval_pilot.py`

### Resolution

- **Resolved**: 2026-08-29T00:00:00+08:00
- **Notes**: The remaining local suite passed with 71 passed and 3 deselected; campaign reservation and ledger suites passed separately.

---

## [ERR-20260829-001] campaign_ledger_python_alias

**Logged**: 2026-08-29T00:00:00+08:00
**Priority**: low
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

### Resolution

- **Resolved**: 2026-08-29T00:00:00+08:00
- **Notes**: Switched the retry to the repository virtual environment.

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
