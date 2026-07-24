# Benchmarking

Run JoinSafetyBench's frozen candidate manifest:

```bash
python -m pytest tests/test_benchmark.py -q
```

Run the local performance fixture after installing the package:

```bash
python scripts/run_benchmark.py --rows 1000000 --output performance.json
```

The JSON result records the requested row count, wall-clock scan time, and
process peak RSS. The Linux CI gate requires no more than 60 seconds and less
than 1 GiB peak RSS for one million rows. These fixtures test implementation
regression only; they do not establish discovery generalization.
