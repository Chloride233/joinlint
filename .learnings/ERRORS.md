# Errors

Command failures and integration errors.

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
