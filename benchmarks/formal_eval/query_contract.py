from __future__ import annotations

from benchmarks.formal_eval.contracts import SealedAgentTask
from joinlint.contracts import canonical_json


def render_task_input(task: SealedAgentTask) -> str:
    body = f"Question:\n{task.question}\n\nSchema:\n{task.schema_text}"
    if task.query_contract is None:
        return body
    contract = canonical_json(task.query_contract.model_dump(mode="json")).decode("utf-8")
    return (
        body
        + "\n\nTrusted query contract (defines intent, not join predicates):\n"
        + contract
    )
