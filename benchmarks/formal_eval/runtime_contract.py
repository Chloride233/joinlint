from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

from benchmarks.formal_eval.contracts import StrictModel
from joinlint.mcp_server import create_server


TARGET_TOOLS = frozenset({"get_join_plan", "validate_sql"})


class RuntimeContractReport(StrictModel):
    schema_version: Literal[2] = 2
    actual_tools: tuple[str, ...]
    expected_tools: tuple[str, ...]
    ready: bool


def check_tool_names(names: set[str]) -> RuntimeContractReport:
    return RuntimeContractReport(
        actual_tools=tuple(sorted(names)),
        expected_tools=tuple(sorted(TARGET_TOOLS)),
        ready=names == TARGET_TOOLS,
    )


def check_runtime_contract(project: Path) -> RuntimeContractReport:
    tools = asyncio.run(create_server(project).list_tools())
    return check_tool_names({tool.name for tool in tools})

