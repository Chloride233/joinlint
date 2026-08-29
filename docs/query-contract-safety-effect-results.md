# Trusted query-contract join-safety effect results

Status: incomplete infrastructure attempt. No paired treatment effect has been
estimated yet.

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

## Campaign state and retry boundary

The public ledger head after both evidence-bound settlements is
`e240aca8cd3a03b2be3b3f49a4fca7c98f168428`. Cumulative conservative accounting
is CNY 62.600378 and the remaining balance is CNY 7.399622.

The frozen contract-safety stage requires a CNY 8.000000 atomic reservation and
has a CNY 7.797920 resource upper bound. It therefore cannot be retried under
the current CNY 70 campaign without weakening the frozen resource contract.
A retry requires a separately authorized public campaign ceiling and must be
identified as an infrastructure retry, not as repeated sampling after an
observed treatment effect.
