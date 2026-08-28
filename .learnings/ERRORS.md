# Errors

Command failures and integration errors.

---

## [ERR-20260828-001] git_add_permission_review

**Logged**: 2026-08-28T03:29:34Z
**Priority**: medium
**Status**: pending
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

- Reproducible: unknown
- Related Files: `.git/index`

---
