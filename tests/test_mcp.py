from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.server.fastmcp.exceptions import ToolError
from mcp.client.stdio import StdioServerParameters, stdio_client

from joinlint.mcp_contracts import GetJoinPlanResponse, ValidateSQLResponse
from joinlint.mcp_server import _response, create_server, run_server


def make_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE customers(id INTEGER PRIMARY KEY);
        CREATE TABLE orders(
          id INTEGER PRIMARY KEY,
          customer_id INTEGER NOT NULL,
          FOREIGN KEY(customer_id) REFERENCES customers(id)
        );
        INSERT INTO customers VALUES (1), (2);
        INSERT INTO orders VALUES (10, 1), (11, 1), (12, 2);
        """
    )
    connection.commit()
    connection.close()


def test_mcp_exposes_exactly_two_stage1_tools(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    server = create_server(tmp_path, ("data.sqlite",), cache_root=tmp_path / "cache")

    tools = asyncio.run(server.list_tools())

    assert {tool.name for tool in tools} == {"get_join_plan", "validate_sql"}
    plan = next(tool for tool in tools if tool.name == "get_join_plan")
    validate = next(tool for tool in tools if tool.name == "validate_sql")
    entity_ref_schema = plan.inputSchema["properties"]["entity_refs"]["items"]
    assert entity_ref_schema == {"$ref": "#/$defs/EntityRef"}
    entity_ref_schema = plan.inputSchema["$defs"]["EntityRef"]
    assert entity_ref_schema["properties"].keys() == {"ref", "entity"}
    assert entity_ref_schema["required"] == ["ref", "entity"]
    assert entity_ref_schema["additionalProperties"] is False
    assert "sql" in validate.inputSchema["properties"]
    assert all(tool.inputSchema["additionalProperties"] is False for tool in tools)
    assert all("execute" not in tool.name and "schema" not in tool.name for tool in tools)
    assert "referencing child" in plan.description
    assert "aggregation" in plan.description


def test_plan_reports_grain_incompatibility_separately_from_missing_path(
    tmp_path: Path,
) -> None:
    make_database(tmp_path / "data.sqlite")
    server = create_server(tmp_path, ("data.sqlite",), cache_root=tmp_path / "cache")

    result = asyncio.run(
        server.call_tool(
            "get_join_plan",
            {
                "entity_refs": [
                    {"ref": "customers", "entity": "customers"},
                    {"ref": "orders", "entity": "orders"},
                ],
                "start_ref": "customers",
                "expected_grain_ref": "customers",
            },
        )
    )

    response = GetJoinPlanResponse.model_validate(result[1])
    assert response.status == "inconclusive"
    assert response.error is not None
    assert response.error.code == "GRAIN_INCOMPATIBLE"
    assert response.schema_version == 3
    assert response.error.guidance.next_action == "change_expected_grain"
    assert response.error.guidance.affected_refs == ("customers",)


def test_plan_identifies_an_unconnected_entity_ref(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    connection = sqlite3.connect(tmp_path / "data.sqlite")
    connection.execute("CREATE TABLE orphan(id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    server = create_server(tmp_path, ("data.sqlite",), cache_root=tmp_path / "cache")

    result = asyncio.run(
        server.call_tool(
            "get_join_plan",
            {
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                    {"ref": "orphan", "entity": "orphan"},
                ],
                "start_ref": "orphan",
                "expected_grain_ref": "orphan",
            },
        )
    )

    response = GetJoinPlanResponse.model_validate(result[1])
    assert response.status == "inconclusive"
    assert response.error is not None
    assert response.error.code == "UNCONNECTED_ENTITY_REF"
    assert response.error.guidance.next_action == "fix_entity_refs"
    assert response.error.guidance.affected_refs == ("orphan",)


def test_two_call_flow_returns_current_proof_and_bound_validation(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    cache = tmp_path / "cache"
    server = create_server(tmp_path, ("data.sqlite",), cache_root=cache)

    plan = asyncio.run(
        server.call_tool(
            "get_join_plan",
            {
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                ],
                "start_ref": "orders",
                "expected_grain_ref": "orders",
            },
        )
    )
    plan_response = GetJoinPlanResponse.model_validate(plan[1])
    assert plan_response.status == "ok"
    assert plan_response.schema_version == 3
    assert plan_response.data is not None
    assert plan_response.data.lifecycle.status == "current"
    assert plan_response.data.execution_count == 0

    validation = asyncio.run(
        server.call_tool(
            "validate_sql",
            {
                "sql": (
                    "SELECT * FROM orders o JOIN customers c "
                    "ON o.customer_id = c.id"
                ),
                "plan_id": plan_response.data.proof.plan_id,
            },
        )
    )
    validation_response = ValidateSQLResponse.model_validate(validation[1])
    assert validation_response.status == "ok"
    assert validation_response.schema_version == 3
    assert validation_response.data is not None
    assert validation_response.data.proof_matched is True
    assert validation_response.data.execution_count == 0
    assert "proof_binding" in validation_response.data.validated_scope
    assert "answer_correctness" in validation_response.data.not_validated_scope


def test_stale_proof_never_downgrades_to_independent_lint(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    server = create_server(tmp_path, ("data.sqlite",), cache_root=tmp_path / "cache")
    plan = GetJoinPlanResponse.model_validate(
        asyncio.run(
            server.call_tool(
                "get_join_plan",
                {
                    "entity_refs": [
                        {"ref": "orders", "entity": "orders"},
                        {"ref": "customers", "entity": "customers"},
                    ],
                    "start_ref": "orders",
                },
            )
        )[1]
    )
    assert plan.data is not None
    connection = sqlite3.connect(tmp_path / "data.sqlite")
    connection.execute("INSERT INTO customers VALUES (3)")
    connection.commit()
    connection.close()

    result = asyncio.run(
        server.call_tool(
            "validate_sql",
            {
                "sql": "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id",
                "plan_id": plan.data.proof.plan_id,
            },
        )
    )

    response = ValidateSQLResponse.model_validate(result[1])
    assert response.status == "inconclusive"
    assert response.error is not None and response.error.code == "PROOF_STALE"
    assert response.error.guidance.next_action == "replan"
    assert response.error.guidance.affected_refs == ("customers", "orders")
    assert response.error.guidance.freshness_reason == "SOURCE_SNAPSHOT_CHANGED"
    assert response.data is None


def test_mcp_internal_errors_are_sanitized() -> None:
    response = _response(
        "get_join_plan",
        lambda: (_ for _ in ()).throw(RuntimeError("/private/secret")),
    )

    assert response["status"] == "error"
    assert response["error"]["code"] == "INTERNAL_ERROR"
    assert response["error"]["message"] == "INTERNAL_ERROR"
    assert response["error"]["guidance"] == {
        "retryable": False,
        "next_action": "stop",
        "affected_refs": [],
        "blocking_relationship_ids": [],
        "freshness_reason": None,
    }
    assert "/private/secret" not in str(response)


def test_mcp_response_limit_fails_closed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class HugeResponse:
        def model_dump_json(self, *, exclude_none: bool) -> str:
            del exclude_none
            return "x" * 1_048_577

    response = _response("get_join_plan", lambda: HugeResponse())  # type: ignore[arg-type]

    assert response["status"] == "inconclusive"
    assert response["error"]["code"] == "OUTPUT_LIMIT_EXCEEDED"


def test_mcp_rejects_unknown_tool_arguments(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    server = create_server(tmp_path, ("data.sqlite",), cache_root=tmp_path / "cache")

    try:
        asyncio.run(
            server.call_tool(
                "validate_sql",
                {"sql": "SELECT 1", "unexpected": "not-allowed"},
            )
        )
    except ToolError as error:
        assert str(error) == "INVALID_ARGUMENT"
    else:
        raise AssertionError("unknown tool arguments must be rejected")


def test_stdio_flow_does_not_write_to_project(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    before = _project_files(tmp_path)

    responses = asyncio.run(
        _call_tools(
            tmp_path,
            [
                (
                    "get_join_plan",
                    {
                        "entity_refs": [
                            {"ref": "orders", "entity": "orders"},
                            {"ref": "customers", "entity": "customers"},
                        ],
                        "start_ref": "orders",
                    },
                )
            ],
        )
    )

    assert responses[0]["status"] == "ok"
    assert _project_files(tmp_path) == before
    assert "/Users/" not in str(responses)


def test_same_entity_names_across_sources_are_inconclusive(tmp_path: Path) -> None:
    make_database(tmp_path / "first.sqlite")
    make_database(tmp_path / "second.sqlite")
    server = create_server(
        tmp_path,
        ("first.sqlite", "second.sqlite"),
        cache_root=tmp_path / "cache",
    )

    result = asyncio.run(
        server.call_tool(
            "get_join_plan",
            {
                "entity_refs": [
                    {"ref": "orders", "entity": "orders"},
                    {"ref": "customers", "entity": "customers"},
                ],
                "start_ref": "orders",
            },
        )
    )
    response = GetJoinPlanResponse.model_validate(result[1])

    assert response.status == "inconclusive"
    assert response.error is not None and response.error.code == "SOURCE_AMBIGUOUS"
    assert response.error.guidance.next_action == "specify_source"
    assert response.error.guidance.affected_refs == ("customers", "orders")


def test_blocking_validation_returns_bounded_repair_guidance(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    server = create_server(tmp_path, ("data.sqlite",), cache_root=tmp_path / "cache")
    plan = GetJoinPlanResponse.model_validate(
        asyncio.run(
            server.call_tool(
                "get_join_plan",
                {
                    "entity_refs": [
                        {"ref": "orders", "entity": "orders"},
                        {"ref": "customers", "entity": "customers"},
                    ],
                    "start_ref": "orders",
                },
            )
        )[1]
    )
    assert plan.data is not None

    result = asyncio.run(
        server.call_tool(
            "validate_sql",
            {
                "sql": "SELECT * FROM orders o JOIN customers c ON o.id = c.id",
                "plan_id": plan.data.proof.plan_id,
            },
        )
    )
    response = ValidateSQLResponse.model_validate(result[1])

    assert response.status == "findings"
    assert len(response.findings) == 1
    finding = response.findings[0]
    assert finding.code == "PROOF_GRAPH_MISMATCH"
    assert finding.guidance.retryable
    assert finding.guidance.next_action == "revise_sql"
    assert finding.guidance.affected_refs == ("customers", "orders")


def test_grain_finding_identifies_only_contributing_relationships(tmp_path: Path) -> None:
    make_database(tmp_path / "data.sqlite")
    server = create_server(tmp_path, ("data.sqlite",), cache_root=tmp_path / "cache")

    result = asyncio.run(
        server.call_tool(
            "validate_sql",
            {
                "sql": (
                    "SELECT * FROM customers c LEFT JOIN orders o "
                    "ON c.id = o.customer_id"
                ),
                "expected_grain_ref": "customers",
            },
        )
    )
    response = ValidateSQLResponse.model_validate(result[1])

    assert response.status == "findings"
    finding = response.findings[0]
    assert finding.code == "GRAIN_INCOMPATIBLE"
    assert finding.guidance.next_action == "change_expected_grain"
    assert finding.guidance.affected_refs == ("customers",)
    assert len(finding.guidance.blocking_relationship_ids) == 1


def test_run_server_scrubs_provider_credentials(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    observed: dict[str, str | None] = {}

    class FakeServer:
        def run(self, *, transport: str) -> None:
            assert transport == "stdio"
            observed["OPENAI_API_KEY"] = os.environ.get("OPENAI_API_KEY")
            observed["ANTHROPIC_API_KEY"] = os.environ.get("ANTHROPIC_API_KEY")
            observed["GITHUB_TOKEN"] = os.environ.get("GITHUB_TOKEN")
            observed["MODAL_TOKEN_SECRET"] = os.environ.get("MODAL_TOKEN_SECRET")

    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "secret")
    monkeypatch.setattr("joinlint.mcp_server.create_server", lambda *args, **kwargs: FakeServer())

    run_server(tmp_path)

    assert observed == {
        "OPENAI_API_KEY": None,
        "ANTHROPIC_API_KEY": None,
        "GITHUB_TOKEN": None,
        "MODAL_TOKEN_SECRET": None,
    }


async def _call_tools(
    project: Path,
    calls: list[tuple[str, dict[str, object]]],
) -> list[dict[str, object]]:
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        "XDG_CACHE_HOME": str(project.parent / "mcp-cache"),
    }
    server = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "joinlint",
            "serve-mcp",
            "--project",
            str(project),
            "--source",
            "data.sqlite",
        ],
        cwd=project,
        env=environment,
    )
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            results = [await session.call_tool(name, arguments) for name, arguments in calls]
    return [_mcp_response(result.structuredContent) for result in results]


def _mcp_response(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _project_files(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in sorted(project.rglob("*"))
        if path.is_file()
    }
