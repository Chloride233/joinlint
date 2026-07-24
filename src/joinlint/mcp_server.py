from __future__ import annotations

from pathlib import Path
from typing import Callable

from mcp.server.fastmcp import FastMCP

from joinlint.contracts import Envelope, envelope_for
from joinlint.errors import JoinLintError
from joinlint.paths import SafeProject
from joinlint.services import get_data_model, find_join_path, validate_cached_edges, validate_cached_path


def create_server(project: Path) -> FastMCP:
    """Create a local STDIO-only MCP adapter for one trusted project."""
    with SafeProject(project) as trusted_project:
        root = trusted_project.root
    mcp = FastMCP("JoinLint")

    @mcp.tool(name="get_data_model")
    def get_data_model_tool() -> dict[str, object]:
        return _response("get_data_model", lambda: get_data_model(root))

    @mcp.tool(name="find_join_path")
    def find_join_path_tool(source_entity: str, target_entity: str, max_depth: int = 4) -> dict[str, object]:
        return _response(
            "find_join_path",
            lambda: find_join_path(root, source_entity, target_entity, max_depth),
        )

    @mcp.tool(name="validate_join")
    def validate_join_tool(
        edge_ids: list[str] | None = None, path: list[dict[str, str]] | None = None
    ) -> dict[str, object]:
        if (edge_ids is None) == (path is None):
            return envelope_for(
                command="validate_join", status="error", error_code="INVALID_ARGUMENT"
            ).model_dump(mode="json")
        if path is not None:
            return _response("validate_join", lambda: validate_cached_path(root, path))
        return _response("validate_join", lambda: validate_cached_edges(root, edge_ids or []))

    return mcp


def run_server(project: Path) -> None:
    create_server(project).run(transport="stdio")


def _response(command: str, action: Callable[[], Envelope]) -> dict[str, object]:
    try:
        envelope = action()
    except JoinLintError as error:
        status = "inconclusive" if error.exit_code == 3 else "error"
        envelope = envelope_for(command=command, status=status, error_code=error.code)
    except Exception:
        envelope = envelope_for(command=command, status="error", error_code="INTERNAL_ERROR")
    if len(envelope.model_dump_json(exclude_none=False).encode("utf-8")) > 1_048_576:
        envelope = envelope_for(command=command, status="inconclusive", error_code="OUTPUT_LIMIT_EXCEEDED")
    return envelope.model_dump(mode="json")
