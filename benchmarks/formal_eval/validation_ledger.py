from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


VALIDATION_LEDGER_WRITE_FAILED = "EVALUATION_VALIDATION_LEDGER_WRITE_FAILED"


class ValidationLedger:
    """Evaluation-only record of the most recent successful SQL validation."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def reset(self) -> None:
        self.path.unlink(missing_ok=True)

    def record(self, sql: str) -> None:
        payload = json.dumps(
            {
                "schema_version": 1,
                "validated_sql_sha256": _sql_sha256(sql),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, self.path)

    def matches(self, sql: str) -> bool:
        try:
            payload = json.loads(self.path.read_bytes())
        except (OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("schema_version") == 1
            and payload.get("validated_sql_sha256") == _sql_sha256(sql)
        )


def _sql_sha256(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()
