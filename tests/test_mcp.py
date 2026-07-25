from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from joinlint.config import add_source
from joinlint.errors import JoinLintError
from joinlint.contracts import Envelope
from joinlint.mcp_server import _response, create_server
from joinlint.services import find_join_path, scan_project, validate_cached_edges, validate_cached_path


def test_mcp_exposes_exactly_three_tools(project: Path) -> None:
    server = create_server(project)

    tools = asyncio.run(server.list_tools())

    assert {tool.name for tool in tools} == {"get_data_model", "find_join_path", "validate_join"}


def test_find_join_path_rejects_unconfirmed_entities(project: Path) -> None:
    (project / ".joinlint" / "model.yaml").write_text(
        "version: 1\nentities: {}\nrelationships: []\n", encoding="utf-8"
    )
    with pytest.raises(JoinLintError) as captured:
        find_join_path(project, "orders", "items", max_depth=4)

    assert captured.value.code == "ENTITY_NOT_FOUND"


def test_find_join_path_refuses_to_silently_truncate(project: Path) -> None:
    add_source(project, "sales", "data", "csv_directory")
    relationships = "\n".join(
        f"  - id: edge_{index:02d}\n    from: source.id\n    to: target.id\n    cardinality: one_to_one\n    status: confirmed"
        for index in range(21)
    )
    (project / ".joinlint" / "model.yaml").write_text(
        """version: 1
entities:
  source:
    source: sales
    object: source.csv
    grain:
      keys: [id]
      status: confirmed
  target:
    source: sales
    object: target.csv
    grain:
      keys: [id]
      status: confirmed
relationships:
"""
        + relationships
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(JoinLintError) as captured:
        find_join_path(project, "source", "target", max_depth=1)

    assert captured.value.code == "OUTPUT_LIMIT_EXCEEDED"


def test_mcp_response_limit_returns_inconclusive_envelope() -> None:
    response = _response(
        "get_data_model",
        lambda: Envelope(command="get_data_model", status="ok", data={"value": "x" * 1_048_576}),
    )

    assert response["status"] == "inconclusive"
    assert response["error"] == {
        "code": "OUTPUT_LIMIT_EXCEEDED",
        "message": "OUTPUT_LIMIT_EXCEEDED",
    }


def test_mcp_internal_errors_are_stable() -> None:
    response = _response("get_data_model", lambda: (_ for _ in ()).throw(RuntimeError("/private/secret")))

    assert response["status"] == "error"
    assert response["error"] == {"code": "INTERNAL_ERROR", "message": "INTERNAL_ERROR"}


def test_cached_validation_rejects_duplicate_edge_ids(project: Path) -> None:
    _prepare_mcp_project(project)

    with pytest.raises(JoinLintError) as captured:
        validate_cached_edges(project, ["child_to_parent", "child_to_parent"])

    assert captured.value.code == "INVALID_ARGUMENT"


def test_cached_validation_rejects_non_string_edge_ids(project: Path) -> None:
    _prepare_mcp_project(project)

    with pytest.raises(JoinLintError) as captured:
        validate_cached_edges(project, [1])  # type: ignore[list-item]

    assert captured.value.code == "INVALID_ARGUMENT"


def test_cached_path_requires_model_shape_and_reports_compound_fanout(project: Path) -> None:
    (project / "data").mkdir()
    (project / "data" / "children.csv").write_text(
        "id,parent_id\n1,a\n2,a\n3,b\n", encoding="utf-8"
    )
    (project / "data" / "parents.csv").write_text("id,grand_id\na,g1\nb,g1\n", encoding="utf-8")
    (project / "data" / "grands.csv").write_text("id\ng1\n", encoding="utf-8")
    add_source(project, "sales", "data", "csv_directory")
    (project / ".joinlint" / "model.yaml").write_text(
        """version: 1
entities:
  children:
    source: sales
    object: children.csv
    grain:
      keys: [id]
      status: confirmed
  parents:
    source: sales
    object: parents.csv
    grain:
      keys: [id]
      status: confirmed
  grands:
    source: sales
    object: grands.csv
    grain:
      keys: [id]
      status: confirmed
relationships:
  - id: child_to_parent
    from: children.parent_id
    to: parents.id
    cardinality: many_to_one
    status: confirmed
  - id: parent_to_grand
    from: parents.grand_id
    to: grands.id
    cardinality: many_to_one
    status: confirmed
""",
        encoding="utf-8",
    )
    scan_project(project)
    path = [
        {"id": "child_to_parent", "direction": "forward", "cardinality": "many_to_one"},
        {"id": "parent_to_grand", "direction": "forward", "cardinality": "many_to_one"},
    ]

    result = validate_cached_path(project, path)

    assert "COMPOUND_FANOUT" in {finding.code for finding in result.findings}
    path[0]["direction"] = "reverse"
    with pytest.raises(JoinLintError) as captured:
        validate_cached_path(project, path)
    assert captured.value.code == "INVALID_ARGUMENT"


def test_cached_validation_refuses_symlinked_evidence(project: Path) -> None:
    _prepare_mcp_project(project)
    path = project / ".joinlint" / "generated" / "validation.json"
    outside = project.parent / "outside-validation.json"
    outside.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.unlink()
    path.symlink_to(outside)

    with pytest.raises(JoinLintError) as captured:
        validate_cached_edges(project, ["child_to_parent"])

    assert captured.value.code == "EVIDENCE_STALE"


def test_cached_validation_rejects_a_mismatched_relationship_digest(project: Path) -> None:
    _prepare_mcp_project(project)
    path = project / ".joinlint" / "generated" / "validation.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["relationships"][0]["relationship_digest"] = "0" * 64
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(JoinLintError) as captured:
        validate_cached_edges(project, ["child_to_parent"])

    assert captured.value.code == "EVIDENCE_STALE"


def test_cached_validation_rejects_untrusted_extra_artifact_fields(project: Path) -> None:
    _prepare_mcp_project(project)
    path = project / ".joinlint" / "generated" / "validation.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["relationships"][0]["raw_rows"] = ["sensitive-value"]
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(JoinLintError) as captured:
        validate_cached_edges(project, ["child_to_parent"])

    assert captured.value.code == "EVIDENCE_STALE"


def test_stdio_mcp_rejects_sql_shaped_arguments(project: Path) -> None:
    _prepare_mcp_project(project)

    result = asyncio.run(_call_tools(project, [("validate_join", {"sql": "select * from children"})]))

    assert result[0]["status"] == "error"
    assert result[0]["error"]["code"] == "INVALID_ARGUMENT"


def test_stdio_mcp_current_and_stale_evidence_never_mutates_project(project: Path) -> None:
    _prepare_mcp_project(project)
    before = _joinlint_state(project)

    current = asyncio.run(_call_tools(project, [("get_data_model", {}), ("find_join_path", {"source_entity": "children", "target_entity": "parents"}), ("validate_join", {"edge_ids": ["child_to_parent"]})]))

    assert {tool.name for tool in asyncio.run(create_server(project).list_tools())} == {
        "get_data_model",
        "find_join_path",
        "validate_join",
    }
    assert current[0]["status"] == "ok"
    assert current[1]["data"]["paths"]
    assert current[2]["status"] == "findings"
    assert _joinlint_state(project) == before

    (project / "data" / "children.csv").write_text("id,parent_id\n1,a\n2,missing\n", encoding="utf-8")
    source_stale = asyncio.run(_call_tools(project, [("validate_join", {"edge_ids": ["child_to_parent"]})]))
    assert source_stale[0]["status"] == "inconclusive"
    assert source_stale[0]["error"]["code"] == "EVIDENCE_STALE"

    scan_project(project)
    model_path = project / ".joinlint" / "model.yaml"
    model_path.write_text(model_path.read_text(encoding="utf-8").replace("many_to_one", "one_to_one"), encoding="utf-8")
    model_stale = asyncio.run(_call_tools(project, [("validate_join", {"edge_ids": ["child_to_parent"]})]))
    assert model_stale[0]["status"] == "inconclusive"
    assert model_stale[0]["error"]["code"] == "EVIDENCE_STALE"


def _prepare_mcp_project(project: Path) -> None:
    (project / "data").mkdir()
    (project / "data" / "parents.csv").write_text("id\na\nb\n", encoding="utf-8")
    (project / "data" / "children.csv").write_text("id,parent_id\n1,a\n2,a\n", encoding="utf-8")
    add_source(project, "sales", "data", "csv_directory")
    (project / ".joinlint" / "model.yaml").write_text(
        """version: 1
entities:
  children:
    source: sales
    object: children.csv
    grain:
      keys: [id]
      status: confirmed
  parents:
    source: sales
    object: parents.csv
    grain:
      keys: [id]
      status: confirmed
relationships:
  - id: child_to_parent
    from: children.parent_id
    to: parents.id
    cardinality: many_to_one
    status: confirmed
""",
        encoding="utf-8",
    )
    scan_project(project)


async def _call_tools(project: Path, calls: list[tuple[str, dict[str, object]]]) -> list[dict[str, object]]:
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "joinlint", "serve-mcp", "--project", str(project)],
        cwd=project,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            results = [await session.call_tool(name, arguments) for name, arguments in calls]
    return [_mcp_response(result.structuredContent) for result in results]


def _mcp_response(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _joinlint_state(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted((project / ".joinlint").rglob("*"))
        if path.is_file()
    }
