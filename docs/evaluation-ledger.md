# Evaluation and failure ledger

Last reconciled: 2026-08-30.

This is the short index of JoinLint experiments and recurring evaluation
failures. Detailed result files remain authoritative. Raw private traces stay
ignored; this file records only sanitized outcomes, run identities, and root
causes.

See the [current product and experiment architecture](current-architecture.md)
for the execution path from the two-tool MCP through paired scoring, strict
verification, and durable sanitized evidence.

## Classification rule

- **Experiment outcome**: the harness completed and the result may be positive,
  negative, or inconclusive. A negative result is not a CI failure.
- **Infrastructure failure**: the intended sample did not run or the batch was
  incomplete. It cannot be reported as product evidence.
- **Admission block**: a frozen-input, budget, authorization, or workflow guard
  stopped before model execution. This is expected fail-closed behavior.
- **Tooling/UI failure**: local operator or review tooling failed without
  changing the frozen result.

Every repeated root cause has one `Pattern-Key` in `.learnings/ERRORS.md`. A
recurrence reopens that pattern and requires a deterministic guard before the
same operation is retried.

## Product-effect experiments

| Scope | Result | Interpretation | Evidence |
| --- | --- | --- | --- |
| Flash full-dataset synthetic Pilot, 20 pairs | Control 17/20, treatment 16/20, exact McNemar p=1.0 | Completed negative result; no improvement claim | [run 31668040353](https://github.com/Chloride233/joinlint/actions/runs/31668040353) |
| Semantic join-safety synthetic Pilot, 20 pairs | 17/20 vs 14/20, p=0.25; two treatment infrastructure failures retained as zeros | Negative result | [result](semantic-safety-effect-results.md), [run 31687536527](https://github.com/Chloride233/joinlint/actions/runs/31687536527) |
| Posthoc hard-case confirmation, 12 pairs | 3/12 vs 1/12, p=0.625; eight treatment failures were `ENTITY_SET_INCOMPLETE`, three were `WRONG_PLAN` | Negative result; showed that relationship proof cannot establish a complete user-intent entity set | [result](semantic-safety-effect-results.md), [run 31694051893](https://github.com/Chloride233/joinlint/actions/runs/31694051893) |
| Trusted query-contract safety, 20 pairs | First run stopped after 10/40 rows; evidence-preserving recovery finished 19/20 vs 20/20, p=1.0 | Directionally positive, not significant; retained the original infrastructure zero | [result](query-contract-safety-effect-results.md), [failed run 33242737801](https://github.com/Chloride233/joinlint/actions/runs/33242737801), [recovery 33245404583](https://github.com/Chloride233/joinlint/actions/runs/33245404583) |
| Trusted query contract with opaque relationships, 20 pairs | 6/20 vs 20/20, p=0.0001220703125 | Positive preregistered synthetic stress result; does not prove business meaning, discovery, or natural error rates | [result](opaque-relationship-effect-results.md), [run 33249402590](https://github.com/Chloride233/joinlint/actions/runs/33249402590) |
| Local natural BIRD probe, 11 pairs | 10/11 vs 5/11, p=0.0625; treatment mechanism calls completed for only 5-6 rows | Negative local diagnostic, not durable product evidence. Four failures returned no verified/evidence-backed path and two returned `GRAIN_UNPROVABLE`; deeper review links the no-path cases to conservative evidence rejection and relationship/path metadata | ignored local `effect.json`; input lock `fde10e58...` |

The current evidence therefore supports one narrow claim: JoinLint materially
helped when the complete physical query contract was trusted and relationship
columns were opaque. It does not support a general claim that adding the MCP
improves natural-language SQL tasks. The local BIRD result makes that boundary
more important, not less.

## Recurring operational failures and guards

| Pattern-Key | Observed recurrence | Root cause | Current guard/status |
| --- | ---: | --- | --- |
| `ci.duplicate_push_pr` | 120 extra CI runs across 136 commits; 11 failed commits emitted two red runs | Feature-branch pushes and pull requests both triggered ordinary CI | Feature pushes now run pull-request CI only; direct `main` pushes remain covered; superseded same-ref runs cancel |
| `eval.archive.appledouble_xattr` | 2 paid-workflow attempts | macOS tar metadata produced AppleDouble/xattr members after Linux extraction | `release_archive.py` creates deterministic portable archives, rejects hidden metadata, and round-trip verifies the frozen bundle |
| `eval.reservation.cli_policy_drift` | 2 attempts | Policy, workflow mapping, and argparse choices were separate lists | CLI modes now derive from the policy table; a workflow-parity test covers the mapping |
| `eval.formal.itt_abort` | 1 effect attempt | One isolated infrastructure zero incorrectly aborted the entire formal stage | Effect stages retain isolated failures in the intention-to-treat denominator; incomplete/systemic batches still stop |
| `eval.freeze.random_staging_path` | 1 freeze comparison | A temporary staging path leaked into the source-manifest digest | Full bundle is built twice and must match byte for byte |
| `eval.artifact_scan.missing_root` | repeated secondary errors | `always()` scanners ran even when an upstream failure produced no artifact directory | Scans now require a matching artifact tree before execution |
| `local.inspect_ai_import_hang` | 6 local attempts | A local venv mixed `eval` and `remote`; Inspect scanned installed remote extension entry points before the first mock task | Resolved by using a clean `dev,eval` environment. The full local suite completed with 735 passed, 3 skipped in 20.31 seconds |
| `eval.workflow_disabled` | expected | Paid workflows are intentionally disabled outside approved runs | Classify as an admission block with zero spend, not an experiment failure |

Low-impact browser/Phoenix control issues, local Python selection, shell quoting,
Git staging permission review, and disclosure-scope rejection remain itemized in
`.learnings/ERRORS.md`. They are not product failures and are grouped here to
avoid duplicating the full incident text.

## Expiring raw evidence preservation

The following private Inspect artifacts were downloaded on 2026-08-30 before
GitHub expiry. They remain in the ignored local results tree and must not be
committed or published without a separate sanitization review.

| Workflow run | Artifact ID | Files | GitHub expiry | Local tree SHA-256 |
| --- | ---: | ---: | --- | --- |
| 30681622870 | 8813533677 | 8 | 2026-08-31 | `72dca68ad28262d0b5d4a598bf800368446776ed0ac38d5af9f862ebbabbe127` |
| 30781613842 | 8845283296 | 8 | 2026-09-02 | `ce05040f347162f852a9c2074bc229a30624c74f58c4773536071cd9a38408f1` |
| 30788020053 | 8848234623 | 8 | 2026-09-02 | `7c16cf40dea1a087bdf676b0955536989725459757aeb7b609e3c9454b64f6e7` |
| 30820150183 | 8861531612 | 8 | 2026-09-02 | `cfb6721ea9808cbff17d1c3faabfa16927657994df516671d97c13fb9d27cfff` |
| 30818641550 | 8858324933 | 1 | 2026-09-02 | `b93f3d40e5f67a82a7383ecf29cb498b99e56c94b63d8843088ed1c9e8b5d815` |

Combined: 33 `.eval` files; local tree SHA-256
`3f38ae8cf15227e76b9341192f57c9d47acf8acd06c5ce677d6e5dcb631f9000`.

## Open evidence gaps

- Several early formal runs have raw checkpoints but no durable, sanitized
  effect or root-cause report. The local preservation above prevents immediate
  loss; it does not make those runs publication-ready.
- The local BIRD result and its raw traces are ignored and machine-local. A
  sanitized, replay-verifiable export is needed before treating it as durable
  portfolio evidence.
- Expected authorization/policy denials can still appear red in GitHub. They
  are safe but visually noisy; changing their conclusion semantics must not
  weaken the fail-closed boundary.
