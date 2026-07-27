from __future__ import annotations

from collections import Counter

from benchmarks.formal_eval.deterministic_suite_builder import _sql_cases


def test_sql_case_matrix_has_frozen_shape_and_danger_coverage() -> None:
    cases = tuple(case for index in range(20) for case in _sql_cases("sealed/suite", index))

    assert len(cases) == 400
    assert sum(case.sql_class == "safe" for case in cases) == 200
    assert sum(case.sql_class == "dangerous" for case in cases) == 200
    assert set(case.sql_shape for case in cases) == {
        "alias",
        "cte",
        "subquery",
        "where_equality",
        "using",
        "self_join",
        "composite",
        "wrong_edge",
        "grain_change",
        "many_to_many",
        "compound_fanout",
        "ddl_dml",
        "multi_statement",
    }
    assert set(case.danger_type for case in cases if case.danger_type) == {
        "unsupported_edge",
        "missing_required_edge",
        "uniqueness",
        "orphan",
        "cardinality",
        "grain",
        "many_to_many",
        "compound_fanout",
        "unsafe_statement",
        "multi_statement",
    }
    assert all(count == 20 for count in Counter(case.danger_type for case in cases if case.danger_type).values())
