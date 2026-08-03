from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from joinlint.errors import JoinLintError
from joinlint.mcp_contracts import (
    GetJoinPlanRequest,
    GetJoinPlanResponse,
    ValidateSQLRequest,
    ValidateSQLResponse,
    error_response,
    mcp_finding,
)
from joinlint.runtime.cache import RuntimeCache
from joinlint.runtime.domain import EntityRef, RuntimeFinding
from joinlint.runtime.service import RuntimeService
from joinlint.runtime.sql import SQLValidationError


MAX_RESPONSE_BYTES = 1_048_576
_ALLOWED_ENVIRONMENT_KEYS = {
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "XDG_CACHE_HOME",
}


def create_server(
    project: Path,
    sources: tuple[str, ...] = (),
    *,
    auto: bool = True,
    cache_root: Path | None = None,
) -> FastMCP:
    """Create the strict SQLite-only, STDIO-only Stage 1 MCP server."""
    service: RuntimeService | None = None

    def runtime_service() -> RuntimeService:
        nonlocal service
        if service is None:
            service = RuntimeService(
                project,
                sources,
                auto=auto,
                cache=RuntimeCache(cache_root) if cache_root is not None else None,
            )
        return service

    @asynccontextmanager
    async def lifespan(_server):  # type: ignore[no-untyped-def]
        try:
            yield {}
        finally:
            if service is not None:
                service.close()

    mcp = FastMCP(
        "JoinLint",
        instructions=(
            "Use get_join_plan before generating multi-table SQL, then call validate_sql "
            "with the final SQL and plan_id. JoinLint validates physical joins only. "
            "Follow error.guidance or finding.guidance exactly: retryable means retry only "
            "after applying next_action, never retry an unchanged request, and stop means "
            "do not execute or retry. "
            "JoinLint proof != query correctness."
        ),
        lifespan=lifespan,
    )

    @mcp.tool(name="get_join_plan")
    def get_join_plan_tool(
        entity_refs: list[EntityRef],
        start_ref: str,
        expected_grain_ref: str | None = None,
        max_depth: int = 4,
        include_alternatives: bool = False,
    ) -> dict[str, object]:
        """Return an exact physical Join Proof.

        Each entity_refs item is exactly {"ref": "orders", "entity": "orders"}.
        ref is request-local; entity is the physical table name. This proof does
        not prove query correctness. expected_grain_ref is the instance whose
        unique key must remain one row per output row before aggregation. In a
        many-to-one join, the referencing child usually preserves that grain;
        for a child-row count or other aggregate, use that child as the
        pre-aggregation grain even when the result is grouped by its parent.
        Aggregation, DISTINCT, and GROUP BY do not restore grain in Stage 1.
        If planning is inconclusive, apply error.guidance.next_action. Do not
        invent an edge or retry the unchanged request. A GRAIN_INCOMPATIBLE
        result may be corrected by changing only expected_grain_ref after
        checking the intended pre-aggregation row grain.
        UNCONNECTED_ENTITY_REF permits one changed request only after you
        confirm the affected reference is not required by the intended query.
        """
        return _response(
            "get_join_plan",
            lambda: runtime_service().get_join_plan(
                GetJoinPlanRequest(
                    entity_refs=tuple(entity_refs),
                    start_ref=start_ref,
                    expected_grain_ref=expected_grain_ref,
                    max_depth=max_depth,
                    include_alternatives=include_alternatives,
                )
            ),
        )

    @mcp.tool(name="validate_sql")
    def validate_sql_tool(
        sql: str,
        dialect: str = "sqlite",
        source_id: str | None = None,
        plan_id: str | None = None,
        expected_grain_ref: str | None = None,
    ) -> dict[str, object]:
        """Validate a SQL join graph without executing SQL or judging answer correctness.

        Execute SQL only when status is ok. For findings or errors, apply the
        bounded guidance next_action; stop means do not execute or retry.
        """
        return _response(
            "validate_sql",
            lambda: runtime_service().validate_sql(
                ValidateSQLRequest(
                    sql=sql,
                    dialect=dialect,
                    source_id=source_id,
                    plan_id=plan_id,
                    expected_grain_ref=expected_grain_ref,
                )
            ),
        )

    _forbid_unknown_arguments(mcp, "get_join_plan")
    _forbid_unknown_arguments(mcp, "validate_sql")
    _install_strict_argument_dispatch(mcp)
    return mcp


def run_server(
    project: Path,
    sources: tuple[str, ...] = (),
    *,
    auto: bool = True,
) -> None:
    allowed_environment = {
        key: value
        for key, value in os.environ.items()
        if key in _ALLOWED_ENVIRONMENT_KEYS
    }
    os.environ.clear()
    os.environ.update(allowed_environment)
    create_server(project, sources, auto=auto).run(transport="stdio")


def _response(
    command: Literal["get_join_plan", "validate_sql"],
    action: Callable[[], GetJoinPlanResponse | ValidateSQLResponse],
) -> dict[str, object]:
    try:
        response = action()
    except SQLValidationError as error:
        if error.blocking:
            response = ValidateSQLResponse(
                status="findings",
                findings=(
                    mcp_finding(
                        RuntimeFinding(
                            code=error.code,
                            severity="blocking",
                            message=error.code,
                        )
                    ),
                ),
            )
        else:
            response = error_response("validate_sql", error.code, inconclusive=False)
    except ValidationError:
        response = error_response(command, "INVALID_ARGUMENT", inconclusive=False)
    except JoinLintError as error:
        response = error_response(
            command,
            error.code,
            inconclusive=error.exit_code == 3,
            affected_refs=error.affected_refs,
            blocking_relationship_ids=error.blocking_relationship_ids,
            freshness_reason=error.freshness_reason,  # type: ignore[arg-type]
        )
    except Exception:
        response = error_response(command, "INTERNAL_ERROR", inconclusive=False)
    if len(response.model_dump_json(exclude_none=False).encode("utf-8")) > MAX_RESPONSE_BYTES:
        response = error_response(command, "OUTPUT_LIMIT_EXCEEDED", inconclusive=True)
    return response.model_dump(mode="json")


def _forbid_unknown_arguments(mcp: FastMCP, tool_name: str) -> None:
    tool = mcp._tool_manager.get_tool(tool_name)  # type: ignore[attr-defined]
    if tool is None:
        raise RuntimeError(f"tool registration failed: {tool_name}")
    tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
    tool.fn_metadata.arg_model.model_rebuild(force=True)
    tool.parameters = tool.fn_metadata.arg_model.model_json_schema(by_alias=True)


def _install_strict_argument_dispatch(mcp: FastMCP) -> None:
    manager = mcp._tool_manager  # type: ignore[attr-defined]
    original = manager.call_tool
    allowed = {
        name: frozenset(tool.fn_metadata.arg_model.model_fields)
        for name in ("get_join_plan", "validate_sql")
        if (tool := manager.get_tool(name)) is not None
    }

    async def strict_call_tool(
        name: str,
        arguments: dict[str, object],
        context: object | None = None,
        convert_result: bool = False,
    ):  # type: ignore[no-untyped-def]
        expected = allowed.get(name)
        if expected is not None and not set(arguments) <= expected:
            raise ToolError("INVALID_ARGUMENT")
        return await original(
            name,
            arguments,
            context=context,
            convert_result=convert_result,
        )

    manager.call_tool = strict_call_tool
