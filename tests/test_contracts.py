from __future__ import annotations

from joinlint.contracts import canonical_json, envelope_for


def test_canonical_json_is_key_order_independent() -> None:
    assert canonical_json({"b": 1, "a": [True, None]}) == canonical_json(
        {"a": [True, None], "b": 1}
    )
    assert canonical_json({"b": 1, "a": [True, None]}) == b'{"a":[true,null],"b":1}'


def test_inconclusive_envelope_has_no_authoritative_data() -> None:
    response = envelope_for(
        command="scan", status="inconclusive", error_code="INCONCLUSIVE_SCAN"
    )

    assert response.schema_version == 1
    assert response.data is None
    assert response.error is not None
    assert response.error.code == "INCONCLUSIVE_SCAN"
    assert response.findings == []
