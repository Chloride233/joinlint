from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from joinlint.errors import JoinLintError
from joinlint.mcp_server import create_server
from joinlint.services import find_join_path


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
