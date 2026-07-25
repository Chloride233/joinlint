from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("inspect_ai")
pytest.importorskip("sqlglot")

from benchmarks.agent_join.joinlint_eval import (  # noqa: E402
    AUTHORITATIVE_ORACLE,
    _deepseek_should_retry,
    build_samples,
    run_eval,
    sanitized_mcp_environment,
)
import httpx  # noqa: E402
from inspect_ai.model import (  # noqa: E402
    ChatMessageAssistant,
    ChatMessageTool,
    ModelOutput,
    get_model,
)
from inspect_ai.tool import ToolCall  # noqa: E402
from openai import APIConnectionError, APIStatusError, APITimeoutError  # noqa: E402
from tests.agent_join_helpers import build_frozen_inputs  # noqa: E402


@pytest.fixture
def frozen_inputs(tmp_path: Path) -> Path:
    return build_frozen_inputs(tmp_path)


def test_sample_matrix_is_complete_balanced_and_deterministic(
    frozen_inputs: Path,
) -> None:
    samples = build_samples(frozen_inputs)
    primary = [sample for sample in samples if sample.metadata["suite"] == "primary"]
    assert len(samples) == 200
    assert len(primary) == 192
    assert Counter(sample.metadata["arm"] for sample in primary) == {
        "A": 48,
        "B": 48,
        "C": 48,
        "D": 48,
    }
    assert Counter(sample.metadata["repetition"] for sample in primary) == {
        0: 64,
        1: 64,
        2: 64,
    }
    assert sum(sample.metadata["suite"] == "zero_config" for sample in samples) == 4
    assert sum(sample.metadata["suite"] == "safety" for sample in samples) == 4
    assert [sample.id for sample in samples] == [
        sample.id for sample in build_samples(frozen_inputs)
    ]


def test_only_mcp_arms_receive_projects_and_channels(frozen_inputs: Path) -> None:
    by_key = {
        (sample.metadata["task_id"], sample.metadata["repetition"], sample.metadata["arm"]): sample
        for sample in build_samples(frozen_inputs)
        if sample.metadata["suite"] == "primary"
    }
    task_id = sorted({key[0] for key in by_key})[0]
    for repetition in range(3):
        a = by_key[(task_id, repetition, "A")]
        b = by_key[(task_id, repetition, "B")]
        c = by_key[(task_id, repetition, "C")]
        d = by_key[(task_id, repetition, "D")]
        assert a.metadata["mcp_project"] is None
        assert b.metadata["mcp_project"] is None
        assert c.metadata["mcp_project"].endswith("/oracle/" + c.metadata["db_id"])
        assert d.metadata["mcp_project"].endswith("/joinlint/" + d.metadata["db_id"])
        assert AUTHORITATIVE_ORACLE in b.input
        assert AUTHORITATIVE_ORACLE not in a.input
        assert c.input == d.input == a.input
        assert c.metadata["oracle_relationships"] == b.metadata["oracle_relationships"]
        assert c.metadata["oracle_validations"] == b.metadata["oracle_validations"]


def test_primary_order_is_balanced_within_each_database(frozen_inputs: Path) -> None:
    grouped: dict[tuple[str, str, int], list[str]] = defaultdict(list)
    for sample in build_samples(frozen_inputs):
        if sample.metadata["suite"] == "primary":
            key = (
                sample.metadata["db_id"],
                sample.metadata["task_id"],
                sample.metadata["repetition"],
            )
            grouped[key].append(sample.metadata["arm"])
    by_database: dict[str, list[list[str]]] = defaultdict(list)
    for (db_id, _, _), arm_order in grouped.items():
        by_database[db_id].append(arm_order)
    for orders in by_database.values():
        assert len(orders) == 12
        for position in range(4):
            assert Counter(order[position] for order in orders) == {
                "A": 3,
                "B": 3,
                "C": 3,
                "D": 3,
            }


def test_mcp_environment_does_not_inherit_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sentinel-deepseek-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-openai-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "sentinel-github-secret")
    environment = sanitized_mcp_environment()
    assert "DEEPSEEK_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert "GITHUB_TOKEN" not in environment
    assert not any("sentinel" in value for value in environment.values())


def test_deepseek_retry_policy_matches_the_frozen_status_allowlist() -> None:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")

    def status_error(status: int) -> APIStatusError:
        return APIStatusError(
            "fixture",
            response=httpx.Response(status, request=request),
            body=None,
        )

    assert _deepseek_should_retry(APIConnectionError(request=request)) is True
    assert _deepseek_should_retry(APITimeoutError(request=request)) is False
    for status in (429, 500, 502, 503, 504):
        assert _deepseek_should_retry(status_error(status)) is True
    for status in (400, 408, 409, 501, 505):
        assert _deepseek_should_retry(status_error(status)) is False


def test_mock_model_dry_run_calls_mcp_then_submits(
    frozen_inputs: Path,
    tmp_path: Path,
) -> None:
    sample = next(
        sample
        for sample in build_samples(frozen_inputs)
        if sample.metadata["suite"] == "primary" and sample.metadata["arm"] == "C"
    )

    def outputs(
        messages: list[object],
        tools: list[Any],
        tool_choice: object,
        config: object,
    ) -> ModelOutput:
        del tool_choice, config
        names = {candidate.name for candidate in tools}
        if any(
            isinstance(message, ChatMessageTool)
            and (message.function or "").endswith("get_data_model")
            for message in messages
        ):
            function = next(name for name in names if name.endswith("submit_sql"))
            arguments = {
                "sql": "SELECT c.name FROM customers c "
                "JOIN orders o ON c.id = o.customer_id",
                "warning": "",
            }
            call_id = "mock-submit"
        else:
            function = next(name for name in names if name.endswith("get_data_model"))
            arguments = {}
            call_id = "mock-model"
        message = ChatMessageAssistant(
            content="",
            tool_calls=[ToolCall(id=call_id, function=function, arguments=arguments)],
        )
        return ModelOutput.from_message(message, stop_reason="tool_calls")

    model = get_model(
        "mockllm/model",
        custom_outputs=outputs,
        memoize=False,
    )
    logs = run_eval(
        frozen_inputs,
        tmp_path / "logs",
        model=model,
        samples=[sample],
    )
    assert len(logs) == 1
    assert logs[0].status == "success"
    assert logs[0].samples is not None and len(logs[0].samples) == 1
    assert logs[0].samples[0].error is None
    functions = [
        call.function
        for message in logs[0].samples[0].messages
        if isinstance(message, ChatMessageAssistant)
        for call in message.tool_calls or []
    ]
    assert any(function.endswith("get_data_model") for function in functions)
    assert any(function.endswith("submit_sql") for function in functions)
    assert logs[0].samples[0].output.completion
