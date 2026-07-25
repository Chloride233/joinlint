from __future__ import annotations

import json

import pytest

pytest.importorskip("inspect_ai")
pytest.importorskip("sqlglot")

from inspect_ai.model import ChatMessageAssistant, ChatMessageTool  # noqa: E402
from inspect_ai.tool import ToolCall  # noqa: E402

from benchmarks.agent_join.trace import score_trace  # noqa: E402


EDGE = ("customers.id", "orders.customer_id")
SECOND_EDGE = ("items.order_id", "orders.id")


def model_messages(*, blocking: bool = False, prefix: str = "JoinLint_") -> list[object]:
    call = ToolCall(id="call-model", function=f"{prefix}get_data_model", arguments={})
    response = {
        "command": "get_data_model",
        "status": "findings" if blocking else "ok",
        "data": {
            "entities": {},
            "relationships": [
                {
                    "id": "order_customer",
                    "from": "orders.customer_id",
                    "to": "customers.id",
                    "cardinality": "many_to_one",
                    "status": "confirmed",
                }
            ],
        },
        "findings": (
            [
                {
                    "code": "COMPOUND_FANOUT",
                    "severity": "blocking",
                    "message": "COMPOUND_FANOUT",
                }
            ]
            if blocking
            else []
        ),
    }
    return [
        ChatMessageAssistant(content="", tool_calls=[call]),
        ChatMessageTool(
            content=json.dumps(response),
            tool_call_id=call.id,
            function=call.function,
        ),
    ]


def validation_messages(*, validated_ids: list[str] | None = None) -> list[object]:
    messages = model_messages()
    edge_ids = validated_ids if validated_ids is not None else ["order_customer"]
    call = ToolCall(
        id="call-validation",
        function="JoinLint_validate_join",
        arguments={"edge_ids": edge_ids},
    )
    response = {
        "command": "validate_join",
        "status": "ok",
        "data": {
            "relationships": [
                {"relationship_id": relationship_id, "findings": []}
                for relationship_id in edge_ids
            ]
        },
        "findings": [],
    }
    messages.extend(
        [
            ChatMessageAssistant(content="", tool_calls=[call]),
            ChatMessageTool(
                content=json.dumps(response),
                tool_call_id=call.id,
                function=call.function,
            ),
        ]
    )
    return messages


def test_call_is_grounded_only_when_final_sql_uses_returned_edges() -> None:
    grounded = score_trace(model_messages(), frozenset({EDGE}), frozenset({EDGE}), "")
    guessed = score_trace([], frozenset({EDGE}), frozenset({EDGE}), "")
    assert grounded.tool_called is True
    assert grounded.grounded is True
    assert guessed.tool_called is False
    assert guessed.grounded is False


def test_validation_requires_every_final_returned_edge() -> None:
    validated = score_trace(validation_messages(), {EDGE}, {EDGE}, "")
    unknown = score_trace(validation_messages(validated_ids=["unknown"]), {EDGE}, {EDGE}, "")
    assert validated.validated is True
    assert validated.correct_tool_use is True
    assert unknown.validated is False
    assert unknown.tool_error is True


def test_multi_edge_graph_requires_an_appropriate_find_path_call() -> None:
    relationships = [
        {
            "id": "order_customer",
            "from": "orders.customer_id",
            "to": "customers.id",
            "cardinality": "many_to_one",
            "status": "confirmed",
        },
        {
            "id": "item_order",
            "from": "items.order_id",
            "to": "orders.id",
            "cardinality": "many_to_one",
            "status": "confirmed",
        },
    ]
    messages = _tool_exchange(
        "model",
        "get_data_model",
        {},
        {"entities": {}, "relationships": relationships},
    )
    messages += _tool_exchange(
        "path",
        "find_join_path",
        {"source_entity": "customers", "target_entity": "items", "max_depth": 4},
        {
            "paths": [
                [
                    {"id": "order_customer", "direction": "reverse", "cardinality": "one_to_many"},
                    {"id": "item_order", "direction": "reverse", "cardinality": "one_to_many"},
                ]
            ]
        },
    )
    messages += _tool_exchange(
        "validation",
        "validate_join",
        {"edge_ids": ["order_customer", "item_order"]},
        {
            "relationships": [
                {"relationship_id": "order_customer", "findings": []},
                {"relationship_id": "item_order", "findings": []},
            ]
        },
    )
    required = {EDGE, SECOND_EDGE}
    with_path = score_trace(messages, required, required, "")
    without_path = score_trace(messages[:2] + messages[-2:], required, required, "")
    assert with_path.correct_tool_use is True
    assert without_path.correct_tool_use is False


def test_blocking_path_requires_avoidance_or_explicit_warning() -> None:
    silent = score_trace(model_messages(blocking=True), {EDGE}, {EDGE}, "")
    warned = score_trace(model_messages(blocking=True), {EDGE}, {EDGE}, "fan-out risk")
    avoided = score_trace(model_messages(blocking=True), set(), {EDGE}, "")
    assert silent.blocking_compliant is False
    assert warned.blocking_compliant is True
    assert avoided.blocking_compliant is True


def test_returned_context_can_be_bypassed() -> None:
    unsupported = ("customers.id", "orders.id")
    score = score_trace(model_messages(), {unsupported}, {EDGE}, "")
    assert score.bypassed is True
    assert score.grounded is False


def test_malformed_missing_duplicate_and_result_before_call_are_errors() -> None:
    call = ToolCall(id="duplicate", function="get_data_model", arguments={})
    duplicate = [ChatMessageAssistant(content="", tool_calls=[call, call])]
    assert score_trace(duplicate, set(), {EDGE}, "").tool_error is True

    malformed = model_messages()
    malformed[-1] = ChatMessageTool(
        content="not-json",
        tool_call_id="call-model",
        function="JoinLint_get_data_model",
    )
    assert score_trace(malformed, set(), {EDGE}, "").tool_error is True

    result_before_call = [
        ChatMessageTool(
            content="{}",
            tool_call_id="missing",
            function="JoinLint_get_data_model",
        )
    ]
    assert score_trace(result_before_call, set(), {EDGE}, "").tool_error is True


def test_only_bare_or_exact_joinlint_prefix_is_recognized() -> None:
    bare = score_trace(model_messages(prefix=""), {EDGE}, {EDGE}, "")
    wrong_prefix = score_trace(model_messages(prefix="Other_"), {EDGE}, {EDGE}, "")
    assert bare.tool_called is True
    assert wrong_prefix.tool_called is False


def test_unreturned_correct_edge_is_correct_but_not_grounded() -> None:
    score = score_trace([], {EDGE}, {EDGE}, "")
    assert score.grounded is False
    assert score.bypassed is False


def _tool_exchange(
    call_id: str,
    function: str,
    arguments: dict[str, object],
    data: dict[str, object],
) -> list[object]:
    call = ToolCall(
        id=f"call-{call_id}",
        function=f"JoinLint_{function}",
        arguments=arguments,
    )
    response = {
        "command": function,
        "status": "ok",
        "data": data,
        "findings": [],
    }
    return [
        ChatMessageAssistant(content="", tool_calls=[call]),
        ChatMessageTool(
            content=json.dumps(response),
            tool_call_id=call.id,
            function=call.function,
        ),
    ]
