from __future__ import annotations

from pathlib import Path

from joinlint.mcp_server import create_server
from joinlint.report import render_report


def test_mcp_has_no_sql_or_mutation_tool(project: Path) -> None:
    import asyncio

    tools = asyncio.run(create_server(project).list_tools())

    assert {tool.name for tool in tools} == {"get_data_model", "find_join_path", "validate_join"}
    assert all("sql" not in tool.inputSchema["properties"] for tool in tools)


def test_report_never_embeds_raw_identifier_as_markup() -> None:
    report = render_report({"identifiers": ["<script>steal()</script>"]})

    assert "<script>steal()" not in report
    assert "default-src 'none'" in report


def test_readme_states_measured_scope_and_exclusions() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    assert "Quick start" in readme
    assert "Parquet" in readme and "not supported" in readme
    assert "% improvement" not in readme
