from __future__ import annotations

import html
from collections.abc import Iterable
from typing import Any


def render_report(data: dict[str, Any]) -> str:
    """Render a static report without scripts, remote assets, or raw rows."""
    identifiers = _identifiers(data.get("identifiers", []))
    list_items = "".join(f"<li>{html.escape(identifier, quote=True)}</li>" for identifier in identifiers)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta http-equiv=\"Content-Security-Policy\" "
        "content=\"default-src 'none'; style-src 'unsafe-inline'\">"
        "<title>JoinLint report</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:2rem}li{margin:.25rem 0}</style>"
        "</head><body><h1>JoinLint report</h1><ul>"
        f"{list_items}</ul></body></html>"
    )


def _identifiers(values: object) -> Iterable[str]:
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError("report identifiers must be strings")
    return sorted(values, key=lambda value: value.encode("utf-8"))
