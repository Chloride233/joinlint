# Opaque relationship-ambiguity evaluation

**Status:** local schema-v8 construction and freeze verification complete; no
model outcomes exist for this corpus. Formal dispatch remains a separate step.

## Why this is independent

The completed trusted query-contract experiment reached a 19/20 control ceiling
and a 20/20 treatment result. Its semantic relationship column names, such as
`customer_ref` and `policy_ref`, made the intended edge easy to infer without
JoinLint. The frozen v1 result remains immutable and must not be expanded,
rerun, or pooled with this experiment.

This design tests a narrower setting: a trusted query contract supplies the
complete entity set, output fields, and row grain, while the physical schema
contains opaque legacy relationship columns and multiple plausible same-type
join graphs. Treatment alone receives JoinLint's declared relationship proof.

## Frozen construction rule

Dataset `semantic-join-contract-ambiguity-pilot-v1` contains all 20
preconstructed tasks across four new SQLite databases. No model output is used
to include, exclude, rank, or rewrite a task.

For every task, construction fails closed unless:

- gold and decoy SQL both execute and return different results;
- gold and decoy graphs cover the same required entities;
- both graphs have the same join depth;
- every candidate edge joins columns of the same declared type;
- every visible relationship-side column is named only `link_a` through
  `link_d` rather than by business role;
- the visible task schema omits `REFERENCES` declarations;
- JoinLint produces a usable proof for the gold graph;
- proof-bound validation accepts gold SQL and blocks decoy SQL; and
- the evaluation database tool denies `sqlite_schema`, `sqlite_master`, and
  table-valued PRAGMA access, preventing recovery of hidden FK declarations.

The input lock binds the source manifest containing these gates, the 20 tasks,
four databases, registration, run plan, and calibration selection.

## Preregistered analysis

- Registration schema: 8.
- Stage: `semantic_join_contract_ambiguity_v1`.
- Claim scope: `trusted_query_contract_opaque_relationship_stress`.
- Model: frozen DeepSeek V4 Flash identity.
- Paired units: 20 tasks, one control and one treatment run per task.
- Primary outcome: `join_correct_task_completion`.
- Test: exact two-sided McNemar at alpha 0.05.
- Positive result: positive absolute improvement and `p < 0.05`.

With zero control-only wins, six treatment-only wins are the smallest result
that can cross the test boundary: five gives `p = 0.0625`, while six gives
`p = 0.03125`. This is a frozen decision threshold, not a promise that the
experiment will be significant. Reverse discordances require more treatment
wins.

## Claim boundary

A positive result would support only an effect for the named model, hosts,
prompts, product commit, and synthetic opaque-relationship stress corpus. It
would not estimate natural SQL error rates, prove business semantics, or show
that JoinLint discovers undeclared relationships.

The completed CNY 70 campaign remains immutable. Campaign
`joinlint-safety-effect-2026-08-v3` carries its CNY 66.050286 conservative
accounting forward as the opening balance, raises the public cumulative cap to
CNY 82, and begins with CNY 15.949714 available. Its pinned genesis is
`c3a748b999059cfefe6eeb6eeccc1cfbf998db1d`. Calibration and the formal stage
must obtain separate atomic reservations from that ledger before provider
credentials become reachable.
