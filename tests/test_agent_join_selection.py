from __future__ import annotations

import copy
import hashlib
import json
import stat
import zipfile
from collections import Counter
from pathlib import Path

import pytest

pytest.importorskip("inspect_ai")
pytest.importorskip("sqlglot")

from benchmarks.agent_join.prepare_spider import (  # noqa: E402
    classify_eligibility,
    find_spider_root,
    select_pilot,
    write_pilot_lock,
)
from benchmarks.agent_join.projects import (  # noqa: E402
    apply_evaluation_reviews,
    apply_review_sheet,
    audit_review_bundle,
    build_oracle_project,
    build_review_project,
    prepare_evaluation_projects,
)
from joinlint.model import load_model  # noqa: E402
from joinlint.errors import JoinLintError  # noqa: E402
from joinlint.services import (  # noqa: E402
    get_data_model,
    validate_cached_edges,
    validate_cached_path,
)
from tests.agent_join_helpers import (  # noqa: E402
    accept_only_order_customer,
    build_mini_spider,
    build_orders_database,
    load_review_sheet,
    oracle_schema,
    orders_spider_metadata,
)


def test_selection_is_seeded_balanced_and_independent_of_joinlint(tmp_path: Path) -> None:
    spider = build_mini_spider(tmp_path)
    first = select_pilot(
        spider,
        seed=20260725,
        database_count=4,
        tasks_per_database=4,
    )
    private = spider / ".joinlint" / "generated"
    private.mkdir(parents=True)
    (private / "candidates.json").write_text('{"changed":true}', encoding="utf-8")
    second = select_pilot(
        spider,
        seed=20260725,
        database_count=4,
        tasks_per_database=4,
    )
    assert first == second
    assert len(first) == 16
    assert set(Counter(task.db_id for task in first).values()) == {4}


def test_selection_supports_the_approved_train_spider_split(tmp_path: Path) -> None:
    spider = build_mini_spider(tmp_path)
    (spider / "train_spider.json").write_bytes((spider / "dev.json").read_bytes())
    tasks = select_pilot(
        spider,
        seed=20260725,
        database_count=4,
        tasks_per_database=4,
        split="train_spider",
    )
    assert len(tasks) == 16
    assert all(task.task_id.startswith("spider-train-spider-") for task in tasks)


def test_rendered_schema_contains_primary_keys_but_no_foreign_keys(tmp_path: Path) -> None:
    spider = build_mini_spider(tmp_path)
    task = select_pilot(
        spider,
        seed=20260725,
        database_count=4,
        tasks_per_database=4,
    )[0]
    assert "PRIMARY KEY" in task.schema_text
    assert "FOREIGN KEY" not in task.schema_text
    assert "REFERENCES" not in task.schema_text


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "SELECT * FROM orders a JOIN orders b ON a.id = b.customer_id",
            "SELF_JOIN",
        ),
        (
            "SELECT * FROM orders o JOIN customers c "
            "ON o.customer_id = c.id AND o.id = c.id",
            "COMPOSITE_JOIN",
        ),
        (
            "SELECT * FROM orders o JOIN customers c ON o.customer_id > c.id",
            "NON_EQUALITY_JOIN",
        ),
        (
            "SELECT * FROM orders o JOIN customers c ON o.id = c.id",
            "MISSING_DECLARED_FK",
        ),
    ],
)
def test_ineligible_join_shapes_have_stable_reason_codes(query: str, expected: str) -> None:
    metadata = orders_spider_metadata("fixture")
    assert classify_eligibility(query, metadata) == expected


def test_joined_table_requires_exactly_one_primary_key() -> None:
    metadata = orders_spider_metadata("fixture")
    metadata["primary_keys"] = [1]
    query = "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id"
    assert classify_eligibility(query, metadata) == "UNSUPPORTED_GRAIN"


def test_dotted_table_name_is_ineligible() -> None:
    metadata = orders_spider_metadata("fixture")
    metadata["table_names_original"] = ["customer.data", "orders"]
    query = 'SELECT * FROM orders o JOIN "customer.data" c ON o.customer_id = c.id'
    assert classify_eligibility(query, metadata) == "DOTTED_TABLE_NAME"


def test_more_than_three_edges_is_ineligible() -> None:
    metadata = _chain_metadata()
    query = (
        "SELECT * FROM a JOIN b ON b.a_id = a.id "
        "JOIN c ON c.b_id = b.id JOIN d ON d.c_id = c.id JOIN e ON e.d_id = d.id"
    )
    assert classify_eligibility(query, metadata) == "JOIN_EDGE_COUNT"


def test_public_lock_contains_only_hashes_and_sealed_file_contains_sources(
    tmp_path: Path,
) -> None:
    spider = build_mini_spider(tmp_path)
    tasks = select_pilot(
        spider,
        seed=20260725,
        database_count=4,
        tasks_per_database=4,
    )
    archive = tmp_path / "spider.zip"
    archive.write_bytes(b"fixture archive identity")
    manifest = tmp_path / "tracked" / "manifest.json"
    hashes = tmp_path / "tracked" / "hashes.json"
    work = tmp_path / "work"
    write_pilot_lock(
        tasks,
        spider_root=spider,
        archive=archive,
        work_dir=work,
        manifest_path=manifest,
        hashes_path=hashes,
    )
    public_text = manifest.read_text(encoding="utf-8")
    assert tasks[0].question not in public_text
    assert tasks[0].gold_sql not in public_text
    assert len(json.loads(public_text)["tasks"]) == 16
    sealed = json.loads((work / "sealed" / "spider-pilot.json").read_text(encoding="utf-8"))
    assert len(sealed) == 16
    assert sealed[0]["question"]
    assert json.loads(hashes.read_text(encoding="utf-8"))["sources"]["archive"]


def test_archive_discovery_accepts_one_safe_spider_root(tmp_path: Path) -> None:
    spider = build_mini_spider(tmp_path / "source-fixture")
    archive = tmp_path / "spider.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in spider.rglob("*"):
            if path.is_file():
                bundle.write(path, Path("nested") / "spider" / path.relative_to(spider))
    discovered = find_spider_root(archive, tmp_path / "extracted")
    assert discovered.name == "spider"
    assert (discovered / "dev.json").is_file()
    assert find_spider_root(archive, tmp_path / "extracted") == discovered

    changed_archive = tmp_path / "changed.zip"
    with zipfile.ZipFile(changed_archive, "w") as bundle:
        bundle.writestr("different/dev.json", "[]")
    with pytest.raises(ValueError, match="does not match"):
        find_spider_root(changed_archive, tmp_path / "extracted")


@pytest.mark.parametrize("unsafe_name", ["../escape", "/absolute", "C:/windows"])
def test_archive_discovery_rejects_escaping_paths(tmp_path: Path, unsafe_name: str) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(unsafe_name, "unsafe")
    with pytest.raises(ValueError, match="unsafe archive member"):
        find_spider_root(archive, tmp_path / "work")


def test_archive_discovery_rejects_symlinks(tmp_path: Path) -> None:
    archive = tmp_path / "symlink.zip"
    member = zipfile.ZipInfo("link")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(member, "target")
    with pytest.raises(ValueError, match="unsafe archive member"):
        find_spider_root(archive, tmp_path / "work")


def test_review_bundle_excludes_questions_gold_and_foreign_keys(tmp_path: Path) -> None:
    source = build_orders_database(tmp_path)
    review = build_review_project(source, tmp_path / "review")
    serialized = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in review.rglob("*")
        if path.is_file() and path.suffix in {".json", ".yaml", ".md"}
    )
    assert "How many orders" not in serialized
    assert "SELECT" not in serialized
    assert "foreign_keys" not in serialized


def test_review_decisions_create_entities_only_from_reviewed_grains(
    tmp_path: Path,
) -> None:
    review = build_review_project(build_orders_database(tmp_path), tmp_path / "review")
    sheet = load_review_sheet(review)
    accept_only_order_customer(sheet)
    apply_review_sheet(review, sheet)
    model = load_model(review)
    assert model.entities["customers"].grain.keys == ["id"]
    assert model.entities["orders"].grain.keys == ["id"]
    assert {(edge.from_, edge.to) for edge in model.relationships} == {
        ("orders.customer_id", "customers.id")
    }


def test_oracle_project_serves_full_database_graph_and_fresh_validation(
    tmp_path: Path,
) -> None:
    project = build_oracle_project(
        build_orders_database(tmp_path),
        oracle_schema(),
        tmp_path / "oracle",
    )
    model = load_model(project)
    assert {(edge.from_, edge.to) for edge in model.relationships} == {
        ("orders.customer_id", "customers.id")
    }
    assert get_data_model(project).status == "ok"
    result = validate_cached_edges(project, [model.relationships[0].id])
    assert result.status in {"ok", "findings"}
    assert not any(finding.severity == "blocking" for finding in result.findings)


def test_review_sheet_rejects_pending_and_changed_evidence(tmp_path: Path) -> None:
    review = build_review_project(build_orders_database(tmp_path), tmp_path / "review")
    sheet = load_review_sheet(review)
    sheet["reviewer"] = "fixture-reviewer"
    sheet["reviewed_at"] = "2026-07-25T00:00:00Z"
    with pytest.raises(ValueError, match="select one displayed unique grain"):
        apply_review_sheet(review, sheet)


def test_review_bundle_audit_rejects_forbidden_and_unexpected_files(tmp_path: Path) -> None:
    review_root = tmp_path / "bundle"
    review_root.mkdir()
    decision = review_root / "fixture-decisions.json"
    decision.write_text('{"schema_version":1}', encoding="utf-8")
    audit_review_bundle(review_root, set())
    digest = hashlib.sha256(decision.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="forbidden source"):
        audit_review_bundle(review_root, {digest})
    (review_root / "unexpected.yaml").write_text("value: true", encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected file"):
        audit_review_bundle(review_root, set())


def test_prepare_and_apply_freeze_four_models_and_safety_projects(
    tmp_path: Path,
) -> None:
    work = tmp_path / "evaluation"
    spider = build_mini_spider(work / "source")
    tasks = select_pilot(
        spider,
        seed=20260725,
        database_count=4,
        tasks_per_database=4,
    )
    archive = tmp_path / "spider.zip"
    archive.write_bytes(b"fixture archive identity")
    hashes = tmp_path / "tracked" / "hashes.json"
    write_pilot_lock(
        tasks,
        spider_root=spider,
        archive=archive,
        work_dir=work,
        manifest_path=tmp_path / "tracked" / "manifest.json",
        hashes_path=hashes,
    )
    oracle_models = tmp_path / "tracked" / "oracle"
    review_dir = tmp_path / "review"
    prepare_evaluation_projects(work, hashes, oracle_models, review_dir)
    assert len(list(oracle_models.glob("*.yaml"))) == 4
    decisions = sorted(review_dir.glob("*-decisions.json"))
    assert len(decisions) == 4

    for path in decisions:
        sheet = json.loads(path.read_text(encoding="utf-8"))
        accept_only_order_customer(sheet)
        path.write_text(json.dumps(sheet), encoding="utf-8")
    (review_dir / "audit.txt").write_text("complete and allowlisted\n", encoding="utf-8")
    joinlint_models = tmp_path / "tracked" / "joinlint"
    apply_evaluation_reviews(work, review_dir, joinlint_models, hashes)
    assert len(list(joinlint_models.glob("*.yaml"))) == 4
    lock = json.loads(hashes.read_text(encoding="utf-8"))
    assert len(lock["models"]["oracle"]) == 4
    assert len(lock["models"]["joinlint"]) == 4
    assert lock["acquisition"]["digest_provenance"] == (
        "computed_locally_not_published_by_spider"
    )

    safe = get_data_model(work / "projects" / "safety" / "safe_direct")
    mismatch = validate_cached_edges(
        work / "projects" / "safety" / "cardinality_mismatch",
        ["left_to_right"],
    )
    with pytest.raises(JoinLintError) as stale_error:
        validate_cached_edges(
            work / "projects" / "safety" / "stale_evidence",
            ["left_to_right"],
        )
    compound = validate_cached_path(
        work / "projects" / "safety" / "compound_fanout",
        [
            {
                "id": "child_to_parent",
                "direction": "forward",
                "cardinality": "many_to_one",
            },
            {
                "id": "parent_to_grand",
                "direction": "forward",
                "cardinality": "many_to_one",
            },
        ],
    )
    assert not any(finding.severity == "blocking" for finding in safe.findings)
    assert "CARDINALITY_DRIFT" in {finding.code for finding in mismatch.findings}
    assert stale_error.value.code == "EVIDENCE_STALE"
    assert "COMPOUND_FANOUT" in {finding.code for finding in compound.findings}


def _chain_metadata() -> dict[str, object]:
    tables = ["a", "b", "c", "d", "e"]
    columns: list[list[object]] = [[-1, "*"]]
    types = ["text"]
    primary_keys = []
    foreign_keys = []
    id_indexes: list[int] = []
    previous_fk: int | None = None
    for table_index, table in enumerate(tables):
        del table
        id_indexes.append(len(columns))
        columns.append([table_index, "id"])
        types.append("number")
        primary_keys.append(len(columns) - 1)
        if table_index:
            previous_fk = len(columns)
            columns.append([table_index, f"{tables[table_index - 1]}_id"])
            types.append("number")
            foreign_keys.append([previous_fk, id_indexes[table_index - 1]])
    return {
        "db_id": "chain",
        "table_names": copy.copy(tables),
        "table_names_original": tables,
        "column_names": copy.deepcopy(columns),
        "column_names_original": columns,
        "column_types": types,
        "primary_keys": primary_keys,
        "foreign_keys": foreign_keys,
    }
