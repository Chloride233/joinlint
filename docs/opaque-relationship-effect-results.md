# Opaque relationship-ambiguity effect results

Status: completed positive result. The preregistered paired estimate meets the
significance gate within the frozen synthetic stress scope.

Evidence tier: preregistered bounded synthetic Pilot; exploratory
product-effect evidence, not the separate naturalistic confirmatory program.

## Evidence boundary

- Evaluated product commit:
  `eda51713acb53d676165cfa5dbd6906a25837fb5`.
- Frozen input lock:
  `64d43e78e71de8ec676cf60aa7a4847a610f7c5cb652c7f9c51bc381708017f1`.
- Stage: `semantic_join_contract_ambiguity_v1`.
- Dataset: `semantic-join-contract-ambiguity-pilot-v1`.
- Claim scope: `trusted_query_contract_opaque_relationship_stress`.
- Model: `openai-api/deepseek/deepseek-v4-flash`.
- Hosts: Codex and Claude Code, 10 paired tasks each.
- Primary outcome: `join_correct_task_completion`.
- Test: exact two-sided McNemar, alpha 0.05.
- Public campaign ledger branch: `joinlint-campaign-ledger-ambiguity-v1`.
- Pinned empty genesis:
  `c3a748b999059cfefe6eeb6eeccc1cfbf998db1d`.
- Public campaign ceiling: CNY 82.000000.
- Conservative opening balance: CNY 66.050286.

This experiment tests whether JoinLint relationship proof improves Agent task
completion when a trusted query contract supplies the complete entity set,
output fields, and row grain, but the physical relationship columns are opaque
and multiple same-type join graphs are executable. It does not estimate natural
SQL error rates, prove business semantics, or show that JoinLint discovers
undeclared relationships.

## Successful calibration

- GitHub Actions run:
  [33248554896](https://github.com/Chloride233/joinlint/actions/runs/33248554896).
- Infrastructure, harness, scoring, and resource cells: 8/8 passed.
- Actual model cost: CNY 0.07351804.
- Evidence-bound conservative upper: CNY 2.43310204, settled upward at
  CNY 2.433103.
- GitHub Actions artifact ID: `9713719441`.
- Server-reported artifact SHA-256:
  `c12955c0c5ba72a18c279ea87f6c4f3d898f0d0b0c40a08fb2fc4cc246323296`.
- Reservation ID:
  `fa9fb258efd27f74f2302f7aa1e0e73a7cf565f1b1c8e627d66d20ff0df1c532`.
- Settlement ID:
  `8aae67122fa74d5bf77491761254cb59214006e17089d8b3d5b57284017fc910`.

## Formal paired result

- GitHub Actions run:
  [33249402590](https://github.com/Chloride233/joinlint/actions/runs/33249402590).
- Workflow-control commit:
  `9c7f09f231eb9b073b283afd712a42e0ae48ce6e`.
- Paired units: 20; model runs: 40; complete result rows: 40/40.
- Control successes: 6/20.
- Treatment successes: 20/20.
- Both success: 6; both failure: 0.
- Treatment-only wins: 14; control-only wins: 0.
- Absolute improvement: +0.70.
- Exact two-sided McNemar p-value: `0.0001220703125`.
- Preregistered significant improvement: true.
- Treatment `plan_called`, MCP-grounded, final-SQL-validated, and
  protocol-compliant rows: 20/20 each.
- GitHub Actions artifact ID: `9714292688`.
- Server-reported artifact SHA-256:
  `8bc0e5fab38ddd03553d3780bcae75390e6966380382d68945e332acbab8ba6c`.
- Durable Draft Release asset:
  `joinlint-opaque-result-run-33249402590.tar.gz`, ID `535169362`.
- Draft Release tag name (not a Git tag ref):
  `joinlint-formal-opaque-v1-eda517`.
- Frozen-input asset: `pilot-input.tar.gz`, ID `535093165`, SHA-256
  `d9cd3fff1c7c957b3810553df0ddbb33afda47cd83415af1febb50f4f34bf36e`.
- Release-asset SHA-256 after upload and download readback:
  `a4fedc10811830eab5dfd93db3c7ba5ccc35b98905e95dfed13cc2def30ebd02`.
- Actual model cost: CNY 0.38191628.
- Modal compute upper: CNY 1.79792000.
- Image-build reserve: CNY 2.00000000.
- Evidence-bound conservative total: CNY 4.17983628, settled upward at
  CNY 4.179837.

The sanitized artifact passed the repository's strict `pilot-stage` public
artifact validator after download. Its stage record binds the evaluated commit,
input lock, run plans, campaign reservation and ledger commit, model, hosts, and
GitHub run identity.

The repository-pinned sanitized files, per-file hashes, external archive
anchors, and CI reconstruction gate live in
[`benchmarks/formal_eval/published/opaque-relationship-v1/`](../benchmarks/formal_eval/published/opaque-relationship-v1/).

## Failure and sensitivity analysis

The 14 control failures were seven `SQL_PARSE_FAILED`, two `WRONG_PLAN`, three
`MODEL_TIMEOUT`, and two `MODEL_LIMIT` outcomes. The frozen
intention-to-treat rule correctly keeps resource-censored model outcomes in the
primary denominator; the artifact contains no incomplete rows.

As a posthoc robustness check, treating all five timeout/limit outcomes as if
control had succeeded leaves nine treatment-only wins and no control-only wins.
The corresponding exact two-sided p-value is `0.00390625`, so this deliberately
conservative recoding does not change the significance conclusion. It is not a
new primary analysis. Host cells also favor treatment (Claude Code 10/10 versus
5/10 control; Codex 10/10 versus 1/10 control), but the experiment was not
powered for a host-effect comparison.

## Campaign closeout

The formal reservation
`fe57eb804f612007950a04969ab375927d1f70360c592c05fde9bf6acfdc51ac`
was settled against the server-reported artifact digest at CNY 4.179837.
Settlement ID
`81afd54c43c6ed8b915e858c78694d5bb6e63615a12d30b8153f103b089961e5`
produced final public ledger head
`84e38400c7489a4deb024cc1f872e291852509be`.

Calibration and formal model cost totals CNY 0.45543432. Their combined
evidence-bound conservative accounting is CNY 6.612940. Final cumulative
campaign accounting is CNY 72.663226, leaving CNY 9.336774 under the public
CNY 82 ceiling. Both paid workflows were manually disabled after the terminal
formal run.

The frozen corpus must not be rerun, expanded, or pooled with earlier semantic
safety or query-contract experiments in response to this positive result.
