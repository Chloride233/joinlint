from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from inspect_ai.model import ChatMessageAssistant, ChatMessageTool
from inspect_ai.tool import ToolCall

from benchmarks.agent_join.contracts import Edge, TraceScore
from benchmarks.agent_join.sql_edges import canonical_edge


JOINLINT_TOOLS = {"get_data_model", "find_join_path", "validate_join"}


@dataclass
class ParsedTrace:
    called_tools: list[str] = field(default_factory=list)
    successful_tools: list[str] = field(default_factory=list)
    returned_by_id: dict[str, Edge] = field(default_factory=dict)
    validated_ids: set[str] = field(default_factory=set)
    blocking_ids: set[str] = field(default_factory=set)
    blocking_without_edge: bool = False
    path_queries: list[tuple[str, str]] = field(default_factory=list)
    tool_error: bool = False


def parse_joinlint_trace(messages: Sequence[object]) -> ParsedTrace:
    parsed = ParsedTrace()
    calls: dict[str, tuple[str, ToolCall]] = {}
    completed: set[str] = set()

    for message in messages:
        if isinstance(message, ChatMessageAssistant):
            for call in message.tool_calls or []:
                tool_name = _joinlint_tool_name(call.function)
                if tool_name is None:
                    continue
                parsed.called_tools.append(tool_name)
                if call.id in calls or call.parse_error is not None:
                    parsed.tool_error = True
                    continue
                calls[call.id] = (tool_name, call)
        elif isinstance(message, ChatMessageTool):
            tool_name = _joinlint_tool_name(message.function or "")
            if tool_name is None:
                continue
            call_id = message.tool_call_id
            if call_id is None or call_id not in calls or call_id in completed:
                parsed.tool_error = True
                continue
            expected_name, call = calls[call_id]
            completed.add(call_id)
            if expected_name != tool_name or message.error is not None:
                parsed.tool_error = True
                continue
            response = _response_document(message.content)
            if response is None or not _valid_envelope(response, expected_name):
                parsed.tool_error = True
                continue
            if response["status"] not in {"ok", "findings"}:
                parsed.tool_error = True
                continue
            parsed.successful_tools.append(tool_name)
            if tool_name == "get_data_model":
                _record_model_response(parsed, response)
            elif tool_name == "find_join_path":
                _record_path_response(parsed, response, call)
            else:
                _record_validation_response(parsed, response, call)
            _record_blocking_findings(parsed, response, tool_name, call)

    if set(calls) != completed:
        parsed.tool_error = True
    return parsed


def score_trace(
    messages: Sequence[object],
    predicted_edges: Iterable[Edge],
    required_edges: Iterable[Edge],
    warning: str,
) -> TraceScore:
    parsed = parse_joinlint_trace(messages)
    predicted = frozenset(canonical_edge(*edge) for edge in predicted_edges)
    required = frozenset(canonical_edge(*edge) for edge in required_edges)
    returned = frozenset(parsed.returned_by_id.values())
    predicted_ids = {
        relationship_id
        for relationship_id, edge in parsed.returned_by_id.items()
        if edge in predicted
    }
    blocking_edges = {
        parsed.returned_by_id[relationship_id]
        for relationship_id in parsed.blocking_ids
        if relationship_id in parsed.returned_by_id
    }
    blocking_occurred = bool(parsed.blocking_ids) or parsed.blocking_without_edge
    if not blocking_occurred:
        blocking_compliant = None
    else:
        avoided = bool(blocking_edges) and not bool(predicted & blocking_edges)
        if parsed.blocking_without_edge:
            avoided = not predicted
        blocking_compliant = avoided or bool(warning.strip())

    validated = (
        bool(predicted)
        and predicted <= returned
        and predicted_ids <= parsed.validated_ids
    )
    successful = bool(parsed.successful_tools)
    required_entities = {
        endpoint.rsplit(".", 1)[0]
        for edge in required
        for endpoint in edge
    }
    path_is_appropriate = any(
        source in required_entities and target in required_entities
        for source, target in parsed.path_queries
    )
    correct_tool_use = (
        "get_data_model" in parsed.successful_tools
        and validated
        and (len(required) <= 1 or path_is_appropriate)
    )
    return TraceScore(
        tool_called=bool(parsed.called_tools),
        successful_tool_call=successful,
        correct_tool_use=correct_tool_use,
        grounded=bool(predicted) and predicted <= returned,
        validated=validated,
        blocking_compliant=blocking_compliant,
        bypassed=bool(predicted - returned) and bool(returned),
        tool_error=parsed.tool_error,
        returned_edges=sorted(returned, key=_edge_sort_key),
        called_tools=parsed.called_tools,
    )


def _joinlint_tool_name(function: str) -> str | None:
    if function in JOINLINT_TOOLS:
        return function
    if function.startswith("JoinLint_") and function[len("JoinLint_") :] in JOINLINT_TOOLS:
        return function[len("JoinLint_") :]
    return None


def _response_document(content: object) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    try:
        document = json.loads(content)
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def _valid_envelope(document: dict[str, Any], expected_command: str) -> bool:
    return (
        set(document)
        <= {"schema_version", "command", "status", "data", "findings", "error"}
        and {"command", "status", "data"} <= set(document)
        and document.get("command") == expected_command
        and document.get("status") in {"ok", "findings", "inconclusive", "error"}
        and isinstance(document.get("findings", []), list)
        and (document.get("data") is None or isinstance(document.get("data"), dict))
    )


def _record_model_response(parsed: ParsedTrace, response: dict[str, Any]) -> None:
    data = response.get("data") or {}
    relationships = data.get("relationships")
    if not isinstance(relationships, list):
        parsed.tool_error = True
        return
    for relationship in relationships:
        if (
            not isinstance(relationship, dict)
            or not isinstance(relationship.get("id"), str)
            or not isinstance(relationship.get("from"), str)
            or not isinstance(relationship.get("to"), str)
            or relationship.get("status") != "confirmed"
        ):
            parsed.tool_error = True
            continue
        relationship_id = relationship["id"]
        edge = canonical_edge(relationship["from"], relationship["to"])
        existing = parsed.returned_by_id.get(relationship_id)
        if existing is not None and existing != edge:
            parsed.tool_error = True
            continue
        parsed.returned_by_id[relationship_id] = edge


def _record_path_response(
    parsed: ParsedTrace,
    response: dict[str, Any],
    call: ToolCall,
) -> None:
    source = call.arguments.get("source_entity")
    target = call.arguments.get("target_entity")
    if not isinstance(source, str) or not isinstance(target, str):
        parsed.tool_error = True
        return
    parsed.path_queries.append((source, target))
    data = response.get("data") or {}
    paths = data.get("paths")
    if not isinstance(paths, list):
        parsed.tool_error = True
        return
    for path in paths:
        if not isinstance(path, list):
            parsed.tool_error = True
            continue
        for step in path:
            relationship_id = step.get("id") if isinstance(step, dict) else None
            if not isinstance(relationship_id, str) or relationship_id not in parsed.returned_by_id:
                parsed.tool_error = True


def _record_validation_response(
    parsed: ParsedTrace,
    response: dict[str, Any],
    call: ToolCall,
) -> None:
    referenced = _referenced_ids(call.arguments)
    if not referenced or not referenced <= set(parsed.returned_by_id):
        parsed.tool_error = True
        return
    data = response.get("data") or {}
    relationships = data.get("relationships")
    if not isinstance(relationships, list):
        parsed.tool_error = True
        return
    returned_ids = {
        row.get("relationship_id")
        for row in relationships
        if isinstance(row, dict) and isinstance(row.get("relationship_id"), str)
    }
    if returned_ids != referenced:
        parsed.tool_error = True
        return
    parsed.validated_ids.update(referenced)


def _record_blocking_findings(
    parsed: ParsedTrace,
    response: dict[str, Any],
    tool_name: str,
    call: ToolCall,
) -> None:
    blocking = [
        finding
        for finding in response.get("findings", [])
        if isinstance(finding, dict) and finding.get("severity") == "blocking"
    ]
    if not blocking:
        return
    if tool_name == "validate_join":
        referenced = _referenced_ids(call.arguments)
        if referenced:
            parsed.blocking_ids.update(referenced)
            return
    if tool_name == "get_data_model" and parsed.returned_by_id:
        parsed.blocking_ids.update(parsed.returned_by_id)
        return
    parsed.blocking_without_edge = True


def _referenced_ids(arguments: dict[str, object]) -> set[str]:
    edge_ids = arguments.get("edge_ids")
    if isinstance(edge_ids, list) and all(isinstance(value, str) for value in edge_ids):
        return set(edge_ids)
    path = arguments.get("path")
    if isinstance(path, list):
        values = {
            row.get("id")
            for row in path
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        }
        if len(values) == len(path):
            return values
    return set()


def _edge_sort_key(edge: Edge) -> tuple[bytes, bytes]:
    return edge[0].encode("utf-8"), edge[1].encode("utf-8")
