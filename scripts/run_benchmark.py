from __future__ import annotations

import argparse
import json
import resource
import sys
import tempfile
import time
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from joinlint.config import add_source
from joinlint.services import scan_project


def run_performance_fixture(rows: int, output: Path) -> dict[str, int | float]:
    with tempfile.TemporaryDirectory(prefix="joinlint-performance-") as temporary:
        project = Path(temporary)
        data = project / "data"
        data.mkdir()
        _write_rows(data / "records.csv", rows)
        (project / ".joinlint").mkdir()
        (project / ".joinlint" / "config.yaml").write_text("version: 1\nsources: {}\n", encoding="utf-8")
        (project / ".joinlint" / "model.yaml").write_text(
            "version: 1\nentities: {}\nrelationships: []\n", encoding="utf-8"
        )
        add_source(project, "performance", "data", "csv_directory")
        started = time.monotonic()
        scan_project(project)
        payload: dict[str, int | float] = {
            "rows": rows,
            "elapsed_seconds": time.monotonic() - started,
            "peak_rss_bytes": _peak_rss_bytes(),
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return payload


def _write_rows(path: Path, rows: int) -> None:
    with path.open("w", encoding="utf-8") as destination:
        destination.write("record_id,group_id\n")
        for index in range(rows):
            destination.write(f"{index},{index % 1_000}\n")


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    run_performance_fixture(arguments.rows, arguments.output)


if __name__ == "__main__":
    main()
