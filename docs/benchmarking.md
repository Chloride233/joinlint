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
