# Benchmarking

## Frozen regression inputs

JoinSafetyBench has a committed 24-pair candidate manifest: 12 positive and
12 negative pairs. The candidate gate computes `TP/(TP+FP)` and `TP/(TP+FN)`
from those labels and requires precision ≥ 0.85 and recall ≥ 0.80.

Its committed join-safety manifest has 16 expected-finding cases and 8 benign
controls. The public recipes materialize duplicate parents, null or orphan
children, cardinality and schema drift, many-to-many, and compound-fan-out
data. Each case runs the scanner and validator (or the drift checker) rather
than asserting a fixture label alone.

Run both gates:

```bash
python -m pytest tests/test_benchmark.py -q
```

The CSV/SQLite conformance fixture compares normalized schemas, candidates,
cardinalities, and finding codes:

```bash
python -m pytest tests/test_scanner.py -q
```

The pinned Chinook SQLite smoke fixture and its SHA-256 are documented in
[`tests/fixtures/chinook/README.md`](../tests/fixtures/chinook/README.md).
`tests/test_end_to_end.py` covers the public source-registration, scan,
confirmation, validation, baseline, check, and report workflow.

## Performance gate

Run the local performance fixture after installing the package:

```bash
python scripts/run_benchmark.py --rows 1000000 --output performance.json
```

The JSON result records the requested row count, wall-clock scan time, and
process peak RSS. The Linux CI gate requires no more than 60 seconds and less
than 1 GiB peak RSS for one million rows.

The command above was run locally on 2026-07-24 with Python 3.12.13 on macOS
Darwin 25.5.0 arm64. For one million rows it recorded 4.2125421250239015 seconds
and 341,098,496 peak RSS bytes while scanning and validating one confirmed
relationship. This hardware-specific measurement is a
reproducible regression record, not a general performance or discovery claim.

Together, the committed candidate, join-safety, drift, conformance, smoke, and
performance commands provide the v0 relationship-fixture, safety, drift,
runtime, and memory evidence. They test implementation regression only; they
do not establish discovery generalization.

## Exploratory downstream MCP evaluation

`benchmarks/agent_join` contains a separate Spider pilot for measuring whether
an SQL Agent retrieves and uses relationship context through JoinLint MCP. It
is not part of JoinSafetyBench, and pilot effects are not product or marketing
claims.

The current checkpoint uses `train_spider` because the unchanged strict
eligibility filter found only four `dev` tasks from one database. Training-set
memorization is therefore an explicit limitation. Selection and oracle inputs
are frozen, but human relationship review and all Agent runs remain pending as
of 2026-07-25.

Install the isolated evaluation dependencies and run the free gates:

```bash
python -m pip install -e '.[dev,eval]'
python -m pytest \
  tests/test_agent_join_selection.py \
  tests/test_agent_join_scorers.py \
  tests/test_agent_join_arm_isolation.py \
  tests/test_agent_join_reporting.py -q
python -m benchmarks.agent_join.joinlint_eval dry-run \
  --work-dir benchmarks/agent_join/.work \
  --log-dir benchmarks/agent_join/logs/dry-run
```

The focused pytest summary must contain no skips. The dry run uses Inspect's
local mock model, starts the real STDIO MCP server, covers all four arms plus
the zero-configuration and safety diagnostics, and makes no provider request.
Normal CI installs only `dev`; it neither installs the evaluation extra nor
calls DeepSeek. The paid 200-run batch requires a separately recorded approval
after the official Spider inputs, human relationship review, hashes, and all
free gates are frozen.

## Completed-product formal evaluation

`benchmarks/formal_eval` is the schema-v2 release framework for the completed
two-tool product contract: `get_join_plan` and `validate_sql`. It keeps the
relationship-evidence, SQL-validation, and Agent-product questions separate;
its only confirmatory product endpoint is Join-Correct Task Completion Rate.
Join correctness is not evidence that a metric definition, filter, business
interpretation, or complete answer is correct.

Run the free, non-evidentiary checks with:

```bash
python -m pip install -e '.[dev,eval]'
python -m pytest tests/test_formal_eval_*.py -q
python -m benchmarks.formal_eval.cli fake-model \
  --output /tmp/joinlint-formal-smoke
python -m benchmarks.formal_eval.cli inspect-smoke \
  --project tests/fixtures/chinook \
  --output /tmp/joinlint-inspect-smoke
```

The fake rows exercise all gates and the minimum 1,440-run confirmatory matrix,
but must never be quoted as product evidence. The legacy Spider pilot remains
read-only history and is not pooled with schema-v2 results.

The Inspect smoke uses `mockllm/model`, the real two-tool STDIO MCP, Chinook,
and the formal trace scorer. It proves only that Inspect can carry a plan ID
through `get_join_plan -> validate_sql`; it is not the Modal diagnostic canary
and cannot support an Agent-effect claim.

Paid execution is only through `.github/workflows/formal-evaluation.yml`. The
protected `formal-evaluation` environment requires explicit approval, verifies
the input lock and content-derived duplicate fingerprints, freezes one lineage,
checks the pilot-derived power requirement against the exact run plan, and
generates deterministic evidence from the raw locked suite. It runs the
diagnostic canary before the confirmatory batch in Modal. Raw Inspect logs stay
runner-ephemeral and are never uploaded. Formal evidentiary sanitized outputs
are uploaded only after the separate, commit-pinned strict verifier accepts
their exact inventory and content contract. Formal runs must not be launched
from a developer machine. The separate checkout pins code origin, not process
isolation from an adversarial target commit; paid execution remains blocked
until that trust boundary is approved or moved to a clean verifier runner.

Every deterministic bundle carries the raw-suite digest and fixed runner ID.
Every Agent bundle must contain exactly the sample IDs in the frozen run plan.
The final report remains publication-ineligible until the exact preregistered
sample has per-output, arm-blinded human decisions bound to the result and run
plan digests.

The full preregistration contract, thresholds, failure taxonomy, and evidence
boundary are frozen in
[`docs/superpowers/specs/2026-07-25-joinlint-completed-product-evaluation-design.md`](superpowers/specs/2026-07-25-joinlint-completed-product-evaluation-design.md).
The strict two-tool Stage 1 MCP now exists as a Developer Preview. Until the
sealed corpora, independent pilot, exact model and host versions, image digest,
and confirmatory remote runs are complete, only deterministic capability-gate
results may be reported. Join-correct completion must not be described as
query or answer correctness.
