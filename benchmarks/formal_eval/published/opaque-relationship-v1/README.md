# Opaque-relationship v1 published evidence

This directory is the repository-pinned, sanitized evidence copy for the
preregistered bounded synthetic Pilot recorded in
[`docs/opaque-relationship-effect-results.md`](../../../../docs/opaque-relationship-effect-results.md).
It is evaluation evidence only and does not change JoinLint's released runtime
contract.

`calibration/` and `pilot-stage/` pass the repository's strict public-artifact
profiles. `result-lock.json` binds those files to their SHA-256 values, the
evaluated commit and input lock, GitHub Actions artifacts, Draft Release assets,
and the final public campaign-ledger head. CI also rebuilds the effect from all
40 sanitized result rows and verifies the documented sensitivity analysis.

Run the local checks from an activated development environment with:

```bash
python -m benchmarks.formal_eval.public_artifacts \
  --profile pilot-calibration --root \
  benchmarks/formal_eval/published/opaque-relationship-v1/calibration
python -m benchmarks.formal_eval.public_artifacts \
  --profile pilot-stage --root \
  benchmarks/formal_eval/published/opaque-relationship-v1/pilot-stage
python -m pytest -q tests/test_opaque_relationship_published_evidence.py
```

The evidence supports only
`trusted_query_contract_opaque_relationship_stress`. It does not estimate
natural SQL error rates, prove business semantics, demonstrate undeclared
relationship discovery, or authorize pooling or rerunning the frozen corpus.
