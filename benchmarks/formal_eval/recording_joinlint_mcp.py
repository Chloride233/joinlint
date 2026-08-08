from __future__ import annotations

import argparse
from pathlib import Path

from benchmarks.formal_eval.validation_failure_marker import ValidationFailureMarker
from benchmarks.formal_eval.validation_ledger import (
    VALIDATION_LEDGER_WRITE_FAILED,
    ValidationLedger,
)
from joinlint.mcp_contracts import error_response
from joinlint.mcp_server import create_server


def record_validation_outcome(
    ledger: ValidationLedger,
    sql: str,
    response: dict[str, object],
    *,
    failure_marker: ValidationFailureMarker | None = None,
) -> None:
    if response.get("status") == "ok":
        try:
            ledger.record(sql)
        except OSError:
            if failure_marker is not None:
                try:
                    failure_marker.mark_failed()
                except Exception:
                    pass
            failure = error_response(
                "validate_sql",
                VALIDATION_LEDGER_WRITE_FAILED,
                inconclusive=False,
            )
            response.clear()
            response.update(failure.model_dump(mode="json"))


def create_recording_server(
    project: Path,
    ledger: ValidationLedger,
    failure_marker: ValidationFailureMarker,
):
    def record_successful_validation(sql: str, response: dict[str, object]) -> None:
        record_validation_outcome(
            ledger,
            sql,
            response,
            failure_marker=failure_marker,
        )

    return create_server(project, auto=True, on_validate_sql=record_successful_validation)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--validation-ledger", type=Path, required=True)
    parser.add_argument("--validation-failure-marker", type=Path, required=True)
    arguments = parser.parse_args(argv)
    failure_marker = ValidationFailureMarker(arguments.validation_failure_marker)
    try:
        create_recording_server(
            arguments.project,
            ValidationLedger(arguments.validation_ledger),
            failure_marker,
        ).run(transport="stdio")
    finally:
        failure_marker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
