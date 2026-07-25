# JoinLint MCP Value Evaluation Design

**Date:** 2026-07-25

**Status:** Design approved in conversation; pending written spec review

**Product:** JoinLint

## 1. Summary

This evaluation determines whether a SQL agent that is correctly configured
with the JoinLint MCP server consults its confirmed relationship model, uses
the returned edges in generated SQL, respects blocking validation findings,
and consequently produces fewer wrong joins.

The first milestone is an exploratory Spider pilot. It validates the protocol,
condition isolation, MCP traces, SQL scorers, and leakage controls. It does not
produce a marketing claim. A later, separately preregistered BIRD evaluation
may produce a quantitative product claim if it satisfies the publication gates
in this design.

This work is separate from JoinSafetyBench. JoinSafetyBench remains an
implementation-regression suite for deterministic candidate and safety rules;
it does not establish downstream Agent value.

## 2. Research questions

The evaluation answers four distinct questions:

1. Does correct relationship context improve join selection?
2. Can an Agent retrieve and apply that context through MCP without material
   loss compared with receiving the same context inline?
3. How much of the oracle relationship graph can the actual JoinLint discovery
   and human-confirmation workflow deliver without access to evaluation tasks?
4. Does the complete JoinLint workflow reduce wrong joins relative to a schema
   that lacks trusted relationship metadata?

The product-level question is:

> When JoinLint MCP is correctly configured, does the Agent call it, follow it,
> and therefore produce fewer wrong joins?

## 3. Claims and non-claims

### 3.1 Claims the experiment may support

- MCP tools were discoverable and callable under the pinned setup.
- The tested Agent used, ignored, or bypassed returned relationships at a
  measured rate.
- JoinLint changed wrong-join and execution outcomes by measured absolute
  percentage points on the named model, dataset, tasks, and configuration.
- The actual JoinLint-confirmed graph recovered a measured portion of the
  database-level declared relationship graph in the compatible v0 subset.

### 3.2 Claims the experiment may not support

- JoinLint prevents all AI join errors.
- Results generalize to other models, databases, prompts, or Agent hosts.
- Declared-foreign-key recovery proves general business-relationship discovery.
- Spider pilot results establish production value or discovery generalization.
- A tool call alone means the final SQL was grounded in the tool response.
- Equivalent execution on one database proves semantic equivalence.

All published wording must name the model, dataset, evaluation date, and MCP
configuration.

## 4. Scope

### 4.1 Included

- Inspect AI as the local evaluation runner.
- DeepSeek API through its OpenAI-compatible endpoint.
- `deepseek-v4-flash` in non-thinking mode for the pilot.
- JoinLint's local STDIO MCP server.
- The `get_data_model`, `find_join_path`, and `validate_join` tools.
- A four-arm, paired evaluation on a frozen Spider subset.
- Explicit single-column equality joins supported by JoinLint v0.
- SQL edge scoring, official execution scoring, MCP trace scoring, and local
  safety micro-cases.
- Full logs, hashes, failure accounting, and an exploratory pilot report.

### 4.2 Excluded from the pilot

- Marketing publication or product-effect claims.
- BIRD or BEAVER execution.
- Composite keys, self-joins, non-equality joins, and cross-database joins.
- Multiple model providers or model comparisons.
- Codex CLI, Claude Code, or another real Agent-host replication.
- Automatic relationship confirmation.
- Hosted dashboards or upload of databases and traces to a SaaS platform.
- JoinLint changes made in response to hidden pilot results.

## 5. Evaluation platform and model

Inspect AI owns task execution, Agent loops, MCP connections, event logs, and
scorer invocation. It connects to JoinLint with its STDIO MCP transport. It
connects to DeepSeek through the OpenAI-compatible endpoint
`https://api.deepseek.com`.

The pilot freezes these model settings:

- model ID: `deepseek-v4-flash`;
- thinking: disabled;
- temperature: `0`;
- maximum output: 4,096 tokens;
- maximum Agent loop: four MCP calls followed by one final answer;
- per-run wall-clock timeout: 120 seconds;
- API credential: `DEEPSEEK_API_KEY` from the environment only.

The API key must never appear in configuration, logs, reports, fixtures, or Git
history. Every run records the requested model ID, response model field when
present, timestamps, token usage, Inspect AI version, and JoinLint commit.

The DeepSeek model ID is a hosted alias rather than an immutable snapshot. The
pilot therefore runs in one bounded window. If the provider announces a model
change or returns a different model identity during that window, the run is
invalidated and restarted as one complete batch. Silent provider changes that
are not surfaced remain a documented validity limitation.

## 6. Dataset and eligibility

### 6.1 Spider pilot

The pilot contains exactly 16 tasks from exactly four Spider SQLite databases,
with four tasks per database. Spider is pinned by release identifier and file
SHA-256; upstream data is not committed unless its license explicitly permits
redistribution.

The exploratory protocol pilot uses `train_spider`, not `dev`. This is an
approved preregistration amendment made before any model run after the pinned
`dev` split produced only four eligible tasks from one database under the
unchanged eligibility rule. The official `train_spider` split produced 200
eligible tasks across 17 databases, including 15 databases with at least four.
Because training examples may be memorized by the evaluated model, these
results validate only the evaluation protocol and must not support a product or
marketing effect claim. A later formal evaluation must use the separately
preregistered BIRD dataset.

An eligibility filter runs only on the pinned Spider metadata and gold SQL. A
task is eligible when:

- it uses one SQLite database;
- it requires at least one and at most three cross-table equality joins;
- every required join is a single-column edge in the database-level declared
  foreign-key graph;
- it contains no self-join, composite join, or non-equality join;
- SQLGlot can parse the gold SQL and normalize every required edge.

Eligibility never depends on JoinLint output. A task remains in the denominator
when JoinLint fails to propose or confirm an edge.

Eligible databases and tasks are ranked deterministically with SHA-256 using
the seed `20260725`; the first four databases with at least four eligible tasks
and the first four ranked tasks in each database form the pilot. The frozen
manifest records the selected IDs and hashes.

### 6.2 Schema presentation

All four arms receive the same deterministic schema rendering: table names,
column names, physical types, and primary-key or grain information. Foreign-key
declarations and relationship annotations are omitted. The Agent cannot read
or execute the SQLite file during generation.

This is a disclosed metadata-ablation setting. Results apply only to schemas
that lack trusted relationship metadata; they do not describe agents that
already receive complete foreign keys.

### 6.3 Oracle graph

The oracle graph is the complete database-level declared foreign-key graph for
the eligible single-column subset. It is built from Spider schema metadata, not
from the joins needed by an individual question. Each arm receives a whole
database graph rather than task-specific answer edges.

### 6.4 JoinLint graph

The JoinLint graph is created from the ablated database and table contents by
the public v0 workflow. The relationship reviewer may inspect table and column
names, deterministic candidate evidence, cardinality, and fan-out validation.
The reviewer may not inspect natural-language questions, gold SQL, Spider
foreign-key metadata, oracle graphs, or Agent outputs.

For the exploratory pilot, one human reviewer confirms or rejects candidates;
an independent Agent audits the process and artifacts but does not make the
semantic decision. A formal result requires two independent human relationship
reviewers, with disagreements resolved by a third reviewer.

The confirmed model and its digest are frozen before any Agent run. Evaluation
tasks are never revealed to the relationship-review process.

## 7. Four paired arms

Every task is run in every arm. Each arm uses a fresh conversation.

| Arm | Relationship delivery | Purpose |
| --- | --- | --- |
| A | Ablated schema; no MCP | Baseline |
| B | Ablated schema plus the oracle graph and its validation results inline | Value of correct relationship content |
| C | Ablated schema plus the same oracle graph and validation results through JoinLint MCP | MCP delivery effectiveness |
| D | Ablated schema plus the actual blind-reviewed JoinLint graph through MCP | End-to-end product effect |

The causal comparisons are:

- A versus B: value of relationship context;
- B versus C: loss or gain from delivering identical content through MCP;
- C versus D: headroom attributable to JoinLint graph coverage and confirmation;
- A versus D: end-to-end JoinLint product effect.

For error rates, a positive improvement is always reported as baseline rate
minus treatment rate. Tables must not switch sign conventions between metrics.

Arms C and D receive this integration instruction:

> Before generating multi-table SQL, consult JoinLint for confirmed
> relationships and validate the selected join path. Base join conditions only
> on confirmed edges returned during this run. If validation returns a blocking
> finding, do not silently use that path.

Failure to call MCP after receiving this instruction remains in the result. A
separate four-task diagnostic makes MCP available without the instruction to
measure zero-configuration tool discovery; it is not part of the product-effect
estimate.

## 8. MCP behavior evaluation

Final SQL correctness cannot establish that MCP was used. Inspect AI traces are
therefore scored separately.

### 8.1 Primary MCP behavior metric

**MCP-grounded Join Rate** is the percentage of eligible C and D runs with at
least one final join edge where every normalized final edge appeared in a
successful JoinLint MCP response during that run.

A call without corresponding use in final SQL is not grounded. A correct edge
that the Agent guessed without receiving it from MCP is correct but ungrounded.
An incorrect edge returned by a flawed confirmed model can be grounded but
incorrect; outcome scoring remains separate by design.

### 8.2 Supporting MCP metrics

- **Tool Call Rate:** percentage of eligible runs with at least one JoinLint
  call.
- **Successful Tool Call Rate:** percentage with a successful JoinLint response.
- **Correct Tool Rate:** percentage using tools appropriate to the selected
  entities and path.
- **Validation Rate:** percentage whose final edges or returned path were passed
  to `validate_join` before the final answer.
- **Blocking Compliance Rate:** percentage of blocking validations followed by
  refusal, a safe alternative, or an explicit warning.
- **Bypass Rate:** percentage where applicable MCP context was returned but the
  final SQL used another, unsupported edge.
- **Tool Error Rate:** percentage affected by connection, schema, timeout, stale
  evidence, or invalid-argument errors.

### 8.3 Safety micro-suite

Four committed local cases exercise a safe direct edge, a cardinality mismatch,
a compound fan-out path, and stale evidence. These cases measure tool selection
and blocking compliance only. They validate the MCP evaluation plumbing and do
not contribute to Spider product-effect metrics.

## 9. Outcome evaluation

### 9.1 Primary outcome

**Query-level Wrong Join Rate** is the percentage of runs containing at least
one disallowed join edge or omitting at least one required join edge.

The public effect is reported as the absolute percentage-point change from arm
A to arm D. Relative reduction may appear only as a secondary value alongside
the absolute counts and rates.

### 9.2 Supporting outcomes

- join-edge precision, recall, and F1;
- official Spider execution accuracy;
- valid SQL rate;
- MCP calls and successful grounding;
- input and output tokens;
- wall-clock latency and API cost;
- relationship-review time and candidate acceptance rate;
- results and failure types by database.

SQLGlot extracts explicit `JOIN ... ON` and equality predicates in `WHERE`,
resolves aliases, and normalizes edges as unordered `table.column` pairs.
Cardinality direction is scored separately. Each task may contain multiple
predeclared equivalent join graphs.

Parser failure, no SQL, timeout, tool failure, and refusal remain in the
intention-to-treat denominator. Infrastructure failures are identified
separately but may be rerun only under the frozen retry rules.

Execution accuracy uses the official Spider evaluator. It is secondary because
one database instance can make semantically different SQL return the same empty
or coincidental result.

Only one read-only `SELECT` statement or CTE followed by `SELECT` may reach the
execution evaluator. DDL, DML, multiple statements, attach operations, and
unsupported pragmas are scored invalid and never executed. Allowed SQL runs
against a disposable read-only database snapshot with a statement deadline and
bounded result materialization.

## 10. Roles, blinding, and leakage controls

The task administrator may inspect questions and gold SQL. They freeze task
eligibility, equivalent graphs, schema rendering, prompts, and scorers, but may
not tune JoinLint policy or confirm relationships.

The relationship reviewer may inspect only the ablated database, JoinLint
candidate evidence, and validation results. They may not inspect evaluation
questions, gold SQL, oracle relations, or Agent output.

Before any Agent run, the following are hashed and recorded:

- dataset files and task manifest;
- schema renderer and eligibility filter;
- prompts and integration instruction;
- empty, oracle, and JoinLint models;
- JoinLint commit and candidate policy version;
- Inspect AI task and scorer code;
- model settings, retry rules, and randomization seed.

Development and formal evaluation split by database, never by question. A
formal database cannot be used to tune candidate rules, prompts, tool
descriptions, or scoring.

Automatic scoring hides arm labels. Parser ambiguities are reviewed with arm
labels and tool traces removed. Decisions and reasons are appended to an audit
log; the reviewer cannot alter generated SQL.

## 11. Execution and randomization

The pilot creates 192 planned Spider runs:

```text
16 tasks x 4 arms x 3 repetitions = 192 runs
```

Each run starts a fresh Agent session and MCP process. A preregistered seed
randomizes arm order within each task and balances arm order within each
database. Temperature zero is not treated as deterministic; all repetitions
remain separate observations.

Inspect AI writes append-only local logs for prompts, messages, tool calls,
tool results, outputs, usage, timing, errors, and run metadata. Raw logs are
excluded from Git. Sanitized manifests, aggregate results, and reproduction
instructions are committed. Public raw artifacts may be attached to a GitHub
release only after secret and redistribution review.

## 12. Components and repository layout

```text
benchmarks/agent_join/
  preregistration.yaml
  README.md
  tasks/
    spider-pilot-manifest.json
    spider-pilot-hashes.json
  prompts/
    base.txt
    mcp-integration.txt
  fixtures/
    mcp-safety/
  models/
    empty/
    oracle/
    joinlint/
  prepare_spider.py
  joinlint_eval.py
  scorers.py
  execution.py
  report.py
tests/
  test_agent_join_selection.py
  test_agent_join_scorers.py
  test_agent_join_arm_isolation.py
  test_agent_join_reporting.py
```

`prepare_spider.py` verifies upstream hashes, applies the frozen eligibility
filter, renders schemas, and emits the task manifest. It does not call JoinLint.

`joinlint_eval.py` defines Inspect AI tasks, the four arms, MCP processes,
DeepSeek settings, and structured final output. It does not contain scoring
policy.

`scorers.py` performs deterministic SQL and trace scoring. `execution.py`
adapts the official Spider evaluator without changing its equivalence rules.
`report.py` produces aggregate JSON and Markdown, paired arm comparisons, and
database-clustered confidence intervals.

The pilot implements only the DeepSeek provider and one dataset. It creates no
general provider plugin system, hosted UI, or benchmark framework abstraction.

## 13. Error and retry policy

- A valid model response is never retried because its SQL scored poorly.
- Transient HTTP 429, 500, 502, 503, or 504 responses may be retried twice with
  recorded exponential backoff.
- Connection failure before any model output may follow the same retry rule.
- A timeout, malformed final answer, MCP non-use, MCP application error, SQL
  parser failure, or refusal is a completed failed run and is not retried.
- Infrastructure corruption, mismatched hashes, provider identity change, or a
  scorer crash stops the batch before results are inspected.
- Restarting after a batch-level stop reruns the complete batch with a new run
  ID; results from different batches are never combined.

Every failure is retained in the manifest. Reports distinguish infrastructure
availability from intention-to-treat product outcomes without silently removing
either.

## 14. Statistics

The Spider pilot reports counts, rates, paired differences, and exploratory
intervals. It makes no confirmatory significance or marketing claim.

The later formal evaluation uses the pilot only to estimate variance and
simulate the number of databases and tasks required for 80% power to detect a
10 percentage-point absolute Wrong Join Rate reduction. Its provisional floor
is eight databases, 40 tasks, and five repetitions per arm. If licensing or the
eligible corpus cannot meet the powered sample, the result remains exploratory.

Formal confidence intervals use hierarchical cluster bootstrap: sample
databases first, then tasks within databases, then repetitions. Individual API
calls are never treated as independent samples.

## 15. Pilot acceptance gates

The pilot protocol passes only when:

- at least 95% of planned runs produce complete Inspect AI artifacts;
- deterministic scorers agree with blinded manual review on at least 95% of a
  stratified sample;
- arm-isolation tests prove that schema, task, model settings, and unrelated
  prompt text are equal where required;
- all frozen inputs and outputs map to recorded hashes;
- no API key, hidden gold SQL, or forbidden oracle relationship appears in an
  Agent-visible artifact;
- MCP traces distinguish tool discovery, successful calls, returned edges,
  final SQL use, and validation compliance;
- the complete report can be regenerated from sanitized logs.

Passing these gates means the evaluation pipeline is trustworthy. It does not
require JoinLint to improve the pilot result. Negative and null effects are
valid outcomes.

## 16. Formal publication gates

A later marketing article may make a quantitative improvement claim only when
the separately preregistered formal arm D versus arm A comparison meets all of
these conditions:

- Wrong Join Rate decreases by at least 10 absolute percentage points;
- the database-clustered 95% confidence interval for the decrease has a lower
  bound above zero;
- execution accuracy does not decrease by more than five absolute percentage
  points;
- all eligible tasks, failures, MCP non-use, and no-improvement databases remain
  in the report;
- the model, dataset, prompts, JoinLint version, absolute counts, costs, and
  reproduction commands are disclosed;
- the claim is limited to the evaluated model, dataset, date, and configured
  JoinLint MCP workflow.

Regardless of effect direction, the complete formal result is published. The
article narrative is written only after results are frozen.

## 17. Verification strategy

Unit tests cover alias resolution, reversed edge order, `WHERE` joins, CTEs,
missing edges, extra edges, parse failures, and multiple allowed graphs.

Arm-isolation tests compare rendered inputs and prove that only the intended
relationship-delivery channel differs. Trace tests distinguish a call from
actual grounding and verify that blocking findings require an observable Agent
response.

A fake-model dry run exercises all four arms without spending API credit. A
four-case local MCP safety run validates stdio lifecycle, tool traces, stale
evidence, and blocking compliance before Spider execution.

Before the paid pilot, the repository runs targeted tests, the full existing
test suite, Ruff, `git diff --check`, and a secret scan over all new artifacts.

## 18. Delivery sequence

1. Implement and test the local Inspect AI harness and deterministic scorers.
2. Freeze the Spider release, eligibility manifest, prompts, models, and hashes.
3. Run the fake-model and MCP safety gates.
4. Complete blind JoinLint relationship review and freeze its model digest.
5. Run the 192-run DeepSeek pilot as one batch.
6. Perform blinded scorer review and generate the exploratory report.
7. Decide whether the protocol is ready for a separately designed BIRD formal
   evaluation.
8. After a valid formal result, replicate the product workflow in Codex CLI or
   another real MCP host.
9. Only then write the marketing article and quantitative product story.
