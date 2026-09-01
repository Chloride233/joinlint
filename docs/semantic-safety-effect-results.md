# Semantic join-safety effect results

Status: completed negative result. These experiments do not prove that the
current JoinLint treatment improves join-correct task completion.

## Evidence boundary

- Evaluated product commit:
  `cd0fa65fad74e84d53729ce30d3d61ed04dcc974`.
- Frozen input lock:
  `ce9ef14d40c7f19e8c5a1c930f02702e829e24351677e1fe55bd2020f67dea23`.
- Public campaign ledger branch: `joinlint-campaign-ledger-safety-v1`.
- Pinned empty genesis:
  `96d689731148b8eb69812b4d0a82dcac75bf2fd1`.
- Public campaign ceiling: CNY 60.000000.
- Conservative opening balance: CNY 46.717684.
- Primary outcome: `join_correct_task_completion`.
- Test: exact two-sided McNemar, alpha 0.05.

The synthetic corpus deliberately stresses dangerous same-type and
wrong-hierarchy joins. Results apply only to this frozen corpus, model, prompts,
and host allocation. The hard-case confirmation is posthoc and must not be
pooled with the first safety run as if it were an independent population
sample.

## First synthetic safety run

- GitHub Actions run:
  [31687536527](https://github.com/Chloride233/joinlint/actions/runs/31687536527).
- Paired units: 20; model runs: 40.
- Control successes: 17/20.
- Treatment successes: 14/20.
- Treatment-only wins: 0; control-only wins: 3.
- Exact two-sided McNemar p-value: 0.25.
- Sanitized artifact SHA-256:
  `3cee7ce64037f33e2a26883ee13749964b77d9f4399372a4427c1eee96f4b1bf`.
- Evidence-bound conservative cost: CNY 3.901770.

This run did not show a positive effect. Its two treatment infrastructure
failures were kept in the denominator. The remaining treatment regressions
motivated, but did not predetermine, a separately frozen hard-case confirmation.

## Posthoc hard-case confirmation

- GitHub Actions run:
  [31694051893](https://github.com/Chloride233/joinlint/actions/runs/31694051893).
- Stage: `semantic_join_safety_confirmation_v1`.
- Claim scope: `posthoc_synthetic_hard_case_confirmation`.
- Tasks: `sjf-commerce-ordered_products`,
  `sjf-healthcare-visit_diagnosis`, `sjf-healthcare-visit_lab`, and
  `sjf-subscriptions-account_owner`.
- Design: Codex host, three repetitions, paired control/treatment.
- Paired units: 12; model runs: 24.
- Control successes: 3/12.
- Treatment successes: 1/12.
- Both success: 0; both failure: 8; treatment-only wins: 1; control-only wins:
  3.
- Absolute treatment change: -0.1667.
- Exact two-sided McNemar p-value: 0.625.
- Significant improvement: false.
- Sanitized artifact ID: `9179071941`.
- Sanitized artifact SHA-256:
  `e81e8c355167b50ac631c3fab30c49d98926b0f22bf25917fcf1cc0662c80afc`.
- Actual model cost: CNY 0.05772084.
- Modal compute upper: CNY 1.078752.
- Image-build reserve: CNY 2.000000.
- Evidence-bound conservative total: CNY 3.136473.

There were no treatment infrastructure or tool errors. All 12 treatment samples
called `get_join_plan`, received a usable plan, followed the protocol, and
validated the final SQL. Eight of the eleven treatment failures were
`ENTITY_SET_INCOMPLETE`; the other three were `WRONG_PLAN`. This localizes the
main observed limitation: the product can prove a join path for the physical
entities requested by the agent, but the current workflow cannot prove that
the agent translated the user's request into a complete entity set.

## Campaign closeout

The confirmation reservation was settled against its sanitized artifact at
CNY 3.136473. The public ledger head after settlement is
`7128223a93ec97415afde3e955e369dfc472b61c`; cumulative conservative accounting
is CNY 57.606231 and the remaining campaign balance is CNY 2.393769.

Under an exact two-sided McNemar test, at least six discordant pairs all in the
treatment direction are required to reach p < 0.05. The current frozen resource
contract cannot fund a new six-pair experiment from the remaining balance.
Further paid confirmation therefore requires a separately approved public
campaign ceiling and a new preregistration. It must not be started by repeatedly
sampling until a favorable result appears.
