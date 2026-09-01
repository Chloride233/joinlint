# Trusted query-contract join-safety effect results

Status: completed. The paired estimate is directionally positive but does not
meet the preregistered significance gate.

## Evidence boundary

- Evaluated product commit:
  `eda51713acb53d676165cfa5dbd6906a25837fb5`.
- Frozen input lock:
  `84bcdbdffd7746924d6f4f92caa1dae4c94c8697a6f8d832a853f6d563e3d710`.
- Public campaign ledger branch: `joinlint-campaign-ledger-contract-v1`.
- Pinned empty genesis:
  `3c33a92297e4a8b530a80cfbb6cdf6caa0931aef`.
- Public campaign ceiling: CNY 70.000000.
- Conservative opening balance: CNY 57.606231.
- Primary outcome: `join_correct_task_completion`.
- Test: exact two-sided McNemar, alpha 0.05.

The experiment evaluates the composition of a trusted, complete physical query
contract with JoinLint. It does not test whether JoinLint independently infers
business intent or a complete physical entity set.

## Successful calibration

- GitHub Actions run:
  [33242076599](https://github.com/Chloride233/joinlint/actions/runs/33242076599).
- Infrastructure, harness, and scoring cells: 8/8 passed.
- Actual model cost: CNY 0.16519036.
- Evidence-bound conservative upper: CNY 2.52477436.
- GitHub Actions artifact ID: `9711761689`.
- Server-reported artifact SHA-256:
  `ac5f0663472f6328bae41a86a4d8e543cd665ee646c5bc0daa2d712dbf8bea6f`.

The CNY 4.000000 reservation was settled at CNY 2.524775, rounded upward to
the next micro-CNY and bound to that artifact digest.

## Incomplete formal attempt

- GitHub Actions run:
  [33242737801](https://github.com/Chloride233/joinlint/actions/runs/33242737801).
- Workflow commit:
  `a29d218fd882ce9f9081b1d8abc2995c364c2776`.
- Completed batches: 1/4.
- Completed model samples: 10/40, all from the control condition.
- Control task outcomes: 9 successes and 1 zero-token
  `INFRASTRUCTURE_FAILURE`.
- Treatment task outcomes: not run.
- Actual model cost: CNY 0.01989120.
- GitHub Actions artifact ID: `9711956025`.
- Server-reported artifact SHA-256:
  `1ada78564601993b5804713e02c4aaeb6a897db20fff9dfbdb72a856a871ec5d`.

The stage produced no paired effect estimate. The 9/10 control result must not
be compared with a later treatment-only run or presented as product evidence.

The failed CNY 8.000000 reservation was settled at CNY 2.469372. This is the
upward micro-CNY rounding of the observed model cost, one of four equal full
Modal compute upper-bound batches, and the complete image-build reserve:

```text
0.01989120 + (1.79792000 / 4) + 2.00000000 = 2.46937120 CNY
```

## Harness correction

The formal runner contradicted the preregistered intention-to-treat rule: it
exported isolated infrastructure failures as zero-score task outcomes, but then
aborted the whole stage when it saw one. Commit
`bc2bc0fdc8535831101658ddb1f302aabc09561a` corrects the boundary:

- formal effect stages keep isolated infrastructure failures in the denominator;
- incomplete batches and systemic infrastructure failures still stop the run;
- calibration remains fail-closed on any infrastructure failure.

Both the push and pull-request Linux CI runs passed the full test suite, Ruff,
and the million-row benchmark:
[33243609433](https://github.com/Chloride233/joinlint/actions/runs/33243609433) and
[33243612750](https://github.com/Chloride233/joinlint/actions/runs/33243612750).

## Completed evidence-preserving recovery

- GitHub Actions run:
  [33245404583](https://github.com/Chloride233/joinlint/actions/runs/33245404583).
- Workflow-control commit:
  `e0c361676cb29b4f8887f02b8a7b37959ee9fa74`.
- Source rows retained unchanged: 10.
- Missing preregistered rows executed: 30.
- Complete result rows: 40/40; incomplete artifacts: 0.
- GitHub Actions artifact ID: `9712912695`.
- Server-reported artifact SHA-256:
  `eec8ea1eccebe755b6f89f7bf4ba148c0b6ae4ca04f3fc196bf92f1ed85210c8`.

The recovery attestation binds the source run, workflow commit, original
reservation, source artifact ID and digest, source stage and result-bundle
digests, new reservation, frozen input lock, and both run plans. The exported
artifact passed the strict `pilot-stage-resume` public scan.

## Paired result

- Control: 19/20 join-correct completions.
- Treatment: 20/20 join-correct completions.
- Treatment wins: 1; control wins: 0; both success: 19; both failure: 0.
- Absolute improvement: +0.05.
- Exact two-sided McNemar p-value: 1.0.
- Preregistered significance decision: false.
- Treatment `plan_called`, MCP-grounded, final-SQL-validated, and
  protocol-compliant rows: 20/20 each.

The only control failure is the retained zero-token infrastructure failure from
the incomplete attempt. It remains an intention-to-treat zero and was not
replaced. The observed direction favors treatment, but one discordant pair is
far below the evidence needed for `p < 0.05`; this run therefore does not prove
that JoinLint improves task completion. The frozen corpus must not be rerun or
expanded in response to this result.

## Campaign closeout

The CNY 6.350000 recovery reservation
`fe3fd99277a8ea5d4033f891a2a842ad0d2c3e4ff6371d358449dd382c926b86`
was settled at CNY 3.449908 and bound to the server-reported recovery artifact
digest. This is the upward micro-CNY accounting of:

```text
0.101468 + 1.348440 + 2.000000 = 3.449908 CNY
```

The first term is observed model cost for the 30 newly executed rows; the other
terms are the full incremental Modal compute upper bound and image-build
reserve. Settlement ID
`4f836541a3ff7056dbee0e6b65164392e9602058cb0e450796421411291833d5`
produced public ledger head
`3dcbbe801cd5cce0f54439d434f4a162517745de`. Final cumulative conservative
accounting is CNY 66.050286 and the remaining CNY 70 campaign balance is
CNY 3.949714. The paid formal workflow was disabled after the terminal run.

## Historical retry boundary

A full 40-sample rerun still requires a CNY 8.000000 atomic reservation and has
a CNY 7.797920 resource upper bound, so it remains forbidden under this
campaign. The completed evidence-preserving recovery retained the exact 10
control rows and ran only the 30 missing preregistered sample IDs. Its
incremental resource upper bound was CNY 6.348440 and its fixed reservation was
CNY 6.350000.

The recovery is bound before reservation to workflow run `33242737801`, its
workflow commit, original reservation, repository identity, failed conclusion,
artifact ID, server-reported artifact digest, frozen input lock, run plan, and
the exact completed sample set. It never replaces or resamples an observed
row. The final exact McNemar test uses all 20 original pairs and remains the
single preregistered effect estimate.
