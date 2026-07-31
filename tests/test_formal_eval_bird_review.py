from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3

from benchmarks.formal_eval.bird_review import (
    grain_provable,
    reviewer_template,
    select_review_candidates,
)


def test_review_selection_is_deterministic_and_does_not_prefer_fk_support() -> None:
    candidates = {
        "alpha": [_task("alpha", index, depth=(index % 3) + 1) for index in range(8)],
        "beta": [_task("beta", index, depth=(index % 2) + 1) for index in range(8)],
    }

    first = select_review_candidates(
        candidates,
        database_ids=("alpha", "beta"),
        tasks_per_database=3,
    )
    changed_support = deepcopy(candidates)
    for values in changed_support.values():
        for task in values:
            task["all_edges_declared_fk"] = not task["all_edges_declared_fk"]
    second = select_review_candidates(
        changed_support,
        database_ids=("alpha", "beta"),
        tasks_per_database=3,
    )

    assert [task["task_id"] for task in first] == [task["task_id"] for task in second]
    assert len(first) == 6
    assert len({(task["question_template_id"], task["sql_structure_id"]) for task in first}) == 6


def test_review_selection_requires_enough_unique_near_duplicate_pairs() -> None:
    candidates = {
        "alpha": [
            {
                **_task("alpha", index, depth=1),
                "question_template_id": "same-question",
                "sql_structure_id": "same-sql",
            }
            for index in range(3)
        ]
    }

    try:
        select_review_candidates(
            candidates,
            database_ids=("alpha",),
            tasks_per_database=2,
        )
    except ValueError as error:
        assert "insufficient unique review candidates" in str(error)
    else:
        raise AssertionError("near-duplicate-only candidates must be rejected")


def test_review_builder_never_imports_joinlint_runtime_or_outputs() -> None:
    source = Path("benchmarks/formal_eval/bird_review.py").read_text(encoding="utf-8")

    assert "joinlint.runtime" not in source
    assert '"uses_joinlint_output": False' in source
    assert '"status": "pending_freeze_validation"' in source
    assert '"relationship_scope": "declared_fk_only"' in source
    assert '"curated_edges_in_confirmatory": 0' in source


def test_reviewer_template_omits_machine_graphs_and_fk_support() -> None:
    task = {
        **_task("alpha", 1, depth=1),
        "source_split": "dev",
        "question": "Which parents have children?",
        "gold_sql": "SELECT * FROM child JOIN parent ON child.parent_id = parent.id",
        "schema_text": "TABLE child; TABLE parent",
        "schema": {"child": {"parent_id": "integer"}, "parent": {"id": "integer"}},
        "physical_join_graph": [["child.parent_id", "parent.id"]],
        "proposed_allowed_graphs": [[[["child.parent_id", "parent.id"]]]],
        "declared_fk_edges": [["child.parent_id", "parent.id"]],
        "curation_required_edges": [],
    }

    assignment = reviewer_template([task])
    serialized_task = assignment["tasks"][0]

    assert set(serialized_task) == {
        "task_id",
        "database_id",
        "source_split",
        "source_index",
        "question",
        "gold_sql",
        "schema_text",
        "schema",
        "annotation",
    }
    assert serialized_task["annotation"]["physical_join_graph"] == []


def test_grain_provable_requires_a_unique_expected_entity(tmp_path: Path) -> None:
    database = tmp_path / "genes-like.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE Classification ("
            "GeneID TEXT NOT NULL PRIMARY KEY, Localization TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE Genes (GeneID TEXT NOT NULL REFERENCES Classification(GeneID), "
            "Chromosome INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO Classification VALUES ('g1','plasma'),('g2','nucleus'),('g3','plasma')"
        )
        connection.execute(
            "INSERT INTO Genes VALUES ('g1',1),('g1',2),('g2',3),('g3',4),('g3',5),('g3',6)"
        )

    assert grain_provable(
        database,
        [("Classification.GeneID", "Genes.GeneID")],
    ) is False
    assert grain_provable(
        database,
        [("Genes.GeneID", "Classification.GeneID")],
    ) is False


def test_grain_provable_accepts_many_to_one_from_unique_child_grain(
    tmp_path: Path,
) -> None:
    database = tmp_path / "orders-like.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE customers (id INTEGER NOT NULL PRIMARY KEY, name TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE orders (id INTEGER NOT NULL PRIMARY KEY, "
            "customer_id INTEGER NOT NULL REFERENCES customers(id))"
        )
        connection.execute("INSERT INTO customers VALUES (1,'a'),(2,'b')")
        connection.execute(
            "INSERT INTO orders VALUES (10,1),(11,1),(12,2),(13,1)"
        )

    assert grain_provable(
        database,
        [("orders.customer_id", "customers.id")],
    ) is True


def _task(database_id: str, index: int, *, depth: int) -> dict[str, object]:
    return {
        "task_id": f"{database_id}-{index}",
        "database_id": database_id,
        "source_index": index,
        "join_depth": depth,
        "question_template_id": f"question-{database_id}-{index}",
        "sql_structure_id": f"sql-{database_id}-{index}",
        "all_edges_declared_fk": bool(index % 2),
    }
