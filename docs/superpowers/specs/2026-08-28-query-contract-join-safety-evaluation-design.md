# Trusted query-contract Join-safety evaluation

**Status:** formal attempt incomplete after an isolated infrastructure failure;
no paired treatment effect has been estimated. See
[`docs/query-contract-safety-effect-results.md`](../../query-contract-safety-effect-results.md).

## 1. Why a new design is necessary

The completed semantic-safety runs found a negative treatment effect and
localized most treatment failures to `ENTITY_SET_INCOMPLETE`. A task audit also
found that some questions did not explicitly state every output entity encoded
by their gold SQL. For example, a question about the product on each order item
was scored against SQL that additionally returned an order identifier.

That design mixes two capabilities:

1. mapping business intent to the complete physical entity set; and
2. choosing and validating the physical Join graph.

JoinLint Stage 1 implements only the second capability. A new experiment must
hold the first capability constant instead of treating an unstated entity as a
JoinLint failure.

The old corpus, inputs, results, and negative conclusion remain immutable. They
must not be pooled with this experiment.

## 2. Shared query contract

Every task carries one strict, input-locked query contract containing:

- the complete required physical entity set;
- the requested physical output fields and stable aliases; and
- the required pre-aggregation row-grain entity.

The contract does not contain Join predicates, relationship evidence, allowed
graphs, dangerous graphs, or gold SQL. Control and treatment receive the exact
same rendered task input and query contract. Only treatment receives JoinLint.

This represents a trusted upstream semantic mapping, not a new JoinLint claim.
The evaluated composition is:

```text
trusted query contract -> JoinLint Join Proof -> SQL generation -> JoinLint validation
```

## 3. Independent stress corpus

`semantic-join-contract-failure-v1` contains 20 new tasks across four new
generated SQLite databases: logistics, education, media, and insurance. None of
the prior safety tasks are reused.

For every task, deterministic construction verifies that:

- the required entity set equals the scorer's expected entity set;
- every requested output field exists in the visible schema;
- the gold Join graph equals the only allowed graph;
- the dangerous SQL executes but returns a result different from gold;
- the query contract produces a usable JoinLint proof;
- proof-bound validation accepts the gold SQL; and
- proof-bound validation reports blocking findings for the dangerous SQL.

The visible schema omits foreign-key declarations for both arms. The corpus is
a synthetic relationship-safety stress test, not an estimate of natural SQL
error rates.

## 4. Paired experiment

- Dataset release: `semantic-join-contract-safety-pilot-v1`.
- Stage: `semantic_join_contract_safety_v1`.
- Model: the frozen cost-efficient DeepSeek V4 Flash identity.
- Paired units: 20 tasks, one control and one treatment run per task.
- Host allocation: the existing deterministic Codex/Claude Code crossover.
- Primary outcome: `join_correct_task_completion`.
- Test: exact two-sided McNemar test at alpha 0.05.
- Positive result: absolute treatment improvement greater than zero and
  `p < 0.05`.
- Resource ceiling: CNY 8.00 for the stage, subject to the separate cumulative
  campaign ledger.

All model limits, timeouts, malformed outputs, tool failures, protocol failures,
and unsafe abstentions remain in the intention-to-treat denominator. The run is
single-shot after calibration; it is not repeated until favorable.

## 5. Claim boundary

A positive result would support only this statement:

> With a trusted, complete physical query contract on the frozen synthetic
> stress corpus, the evaluated JoinLint workflow improved join-correct task
> completion for the named model, hosts, prompts, and product commit.

It would not show that JoinLint independently understands business intent,
builds a semantic layer, validates selected columns or metrics, executes SQL,
or improves natural-world SQL accuracy.
