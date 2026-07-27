from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path

from joinlint.runtime.cache import RuntimeCache
from joinlint.runtime.domain import EvidenceMeasurements, EvidenceRecord


def record() -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id="1" * 64,
        relationship_id="2" * 64,
        snapshot_id="3" * 64,
        provenance="declared",
        verifier_version="sqlite-exact-v1",
        measurements=EvidenceMeasurements(
            child_row_count=1,
            parent_row_count=1,
            child_non_null_count=1,
            child_null_count=0,
            child_distinct_count=1,
            parent_distinct_count=1,
            inclusion_numerator=1,
            inclusion_denominator=1,
            orphan_count=0,
            parent_unique=True,
            observed_cardinality="one_to_one",
            max_children_per_parent=1,
            max_parents_per_child=1,
        ),
        verified_at=datetime.now(UTC).isoformat(),
    )


def test_cache_is_private_and_round_trips_strict_evidence(tmp_path: Path) -> None:
    cache = RuntimeCache(tmp_path / "cache")
    value = record()

    cache.store_evidence(value)

    assert cache.load_evidence(value.evidence_id) == value
    assert cache.load_evidence_for(
        value.relationship_id,
        value.snapshot_id,
        value.provenance,
        value.verifier_version,
    ) == value
    assert stat.S_IMODE(cache.root.stat().st_mode) == 0o700
    serialized = (cache.root / "evidence" / f"{value.evidence_id}.json").read_text()
    assert "/Users/" not in serialized
    assert "raw_values" not in serialized


def test_corrupted_cache_record_is_removed_and_treated_as_missing(tmp_path: Path) -> None:
    cache = RuntimeCache(tmp_path / "cache")
    value = record()
    cache.store_evidence(value)
    path = cache.root / "evidence" / f"{value.evidence_id}.json"
    path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")

    assert cache.load_evidence(value.evidence_id) is None
    assert not path.exists()


def test_cache_lock_serializes_one_content_key(tmp_path: Path) -> None:
    cache = RuntimeCache(tmp_path / "cache")

    with cache.locked("1" * 64):
        assert (cache.root / "locks" / f"{'1' * 64}.lock").exists()
