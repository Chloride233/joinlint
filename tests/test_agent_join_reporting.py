from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("inspect_ai")
pytest.importorskip("sqlglot")

from inspect_ai.dataset import Sample  # noqa: E402
from inspect_ai.model import (  # noqa: E402
    ChatMessageAssistant,
    ChatMessageTool,
    ModelUsage,
)
from inspect_ai.scorer import Score  # noqa: E402
from inspect_ai.tool import ToolCall  # noqa: E402

from benchmarks.agent_join.contracts import PilotSample, ResultRow  # noqa: E402
from benchmarks.agent_join.report import (  # noqa: E402
    SanitizedRowsDocument,
    build_report,
    cluster_bootstrap,
    evaluate_pilot_gates,
    load_result_rows,
    paired_effects,
    read_sanitized_rows,
    render_markdown,
    summarize_relationship_review,
    write_sanitized_rows,
    write_report,
)
from benchmarks.agent_join.trace import score_trace  # noqa: E402


EDGE = ("customers.id", "orders.customer_id")
SECOND_EDGE = ("items.order_id", "orders.id")
HASHES = {"spider-pilot-manifest.json": "a" * 64}
LOG_HASH = "b" * 64


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


def test_effects_use_baseline_minus_treatment_for_error_rates() -> None:
    rows = _complete_rows()
    rows = [
        row.model_copy(update={"wrong_join": row.arm == "A"})
        if row.suite == "primary"
        else row
        for row in rows
    ]
    effects = paired_effects(rows)
    assert effects["wrong_join_rate_points"]["A_vs_D"] == 100.0
    assert effects["wrong_join_rate_points"]["A_vs_B"] == 100.0


def test_cluster_bootstrap_is_seeded_and_resamples_databases_first() -> None:
    rows = _complete_rows()
    first = cluster_bootstrap(rows, draws=100, seed=20260725)
    second = cluster_bootstrap(rows, draws=100, seed=20260725)
    assert first == second
    assert first.draw_count == 100
    assert first.seed == 20260725
    assert first.intervals["wrong_join_rate_points"]["A_vs_D"].lower == 0.0


def test_protocol_gates_do_not_require_a_positive_product_effect() -> None:
    rows = [
        row.model_copy(update={"wrong_join": row.arm == "D"})
        if row.suite == "primary"
        else row
        for row in _complete_rows()
    ]
    document = _document(rows)
    gates = evaluate_pilot_gates(
        document,
        manual_scorer_agreement=1.0,
        expected_sample_ids={row.sample_id for row in rows},
        expected_input_hashes=HASHES,
        expected_log_sha256=LOG_HASH,
    )
    assert paired_effects(rows)["wrong_join_rate_points"]["A_vs_D"] == -100.0
    assert gates["passed"] is True


def test_relationship_review_is_separate_and_scores_the_full_graph() -> None:
    summary = summarize_relationship_review(
        [
            {
                "db_id": "db-0",
                "candidate_edges": [EDGE, SECOND_EDGE],
                "confirmed_edges": [EDGE],
                "oracle_edges": [EDGE, SECOND_EDGE],
                "entity_decisions": ["id", "id", "reject_table"],
                "relationship_decisions": ["accept", "reject"],
                "elapsed_human_review_seconds": 12.5,
            }
        ]
    )
    assert summary["db-0"]["candidate_count"] == 2
    assert summary["db-0"]["graph_precision"] == 1.0
    assert summary["db-0"]["graph_recall"] == 0.5
    assert summary["db-0"]["accepted_entity_count"] == 2


def test_sanitized_rows_round_trip_is_strict_and_sorted(tmp_path: Path) -> None:
    rows = list(reversed(_complete_rows()))
    document = _document(rows)
    destination = tmp_path / "rows.json"
    write_sanitized_rows(destination, document)
    loaded = read_sanitized_rows(
        destination,
        expected_sample_ids={row.sample_id for row in rows},
        expected_requested_model=document.requested_model,
        expected_returned_model=document.returned_model,
        expected_input_hashes=HASHES,
        expected_log_sha256=LOG_HASH,
    )
    assert loaded.rows == sorted(rows, key=lambda row: row.sample_id.encode("utf-8"))

    payload = json.loads(destination.read_text(encoding="utf-8"))
    payload["rows"][0]["gold_sql"] = "SELECT forbidden"
    destination.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        read_sanitized_rows(
            destination,
            expected_sample_ids={row.sample_id for row in rows},
            expected_requested_model=document.requested_model,
            expected_returned_model=document.returned_model,
            expected_input_hashes=HASHES,
            expected_log_sha256=LOG_HASH,
        )


def test_report_never_contains_prompt_gold_sql_or_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "forbidden-secret")
    document = _document(_complete_rows())
    report = build_report(
        document,
        manual_scorer_agreement=1.0,
        expected_sample_ids={row.sample_id for row in document.rows},
        expected_input_hashes=HASHES,
        expected_log_sha256=LOG_HASH,
        draws=100,
        seed=20260725,
    )
    serialized = json.dumps(report, sort_keys=True)
    markdown = render_markdown(report)
    for forbidden in ("forbidden-secret", "gold_sql", "prompt", "SELECT"):
        assert forbidden not in serialized
        assert forbidden not in markdown
    assert "not a general product or marketing claim" in markdown
    assert report["protocol_gates"]["passed"] is True
    json_path = tmp_path / "report.json"
    markdown_path = tmp_path / "report.md"
    write_report(json_path, markdown_path, report)
    assert json.loads(json_path.read_text(encoding="utf-8")) == report
    assert markdown_path.read_text(encoding="utf-8") == markdown


def test_log_export_drops_raw_content_and_survives_log_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planned = _planned_samples(_complete_rows())
    completed = next(sample for sample in planned if sample.metadata["arm"] == "A")
    raw_sql = "SELECT raw_secret FROM hidden"
    logged = SimpleNamespace(
        id=completed.id,
        metadata=completed.metadata,
        error=None,
        total_time=2.5,
        model_usage={
            "openai-api/deepseek/deepseek-v4-flash": ModelUsage(
                input_tokens=100,
                input_tokens_cache_read=25,
                output_tokens=10,
            )
        },
        scores={
            "join_outcome_scorer": Score(
                value=1,
                answer=raw_sql,
                metadata={
                    "wrong_join": False,
                    "precision": 1.0,
                    "recall": 1.0,
                    "f1": 1.0,
                    "predicted_edges": [EDGE],
                    "matched_graph": [EDGE],
                    "error_code": None,
                    "submitted_sql": raw_sql,
                    "warning": "raw-warning-secret",
                },
            ),
            "mcp_trace_scorer": Score.unscored(metadata={"applicable": False}),
            "execution_scorer": Score(
                value=1,
                metadata={"executed": True, "equivalent": True, "error_code": None},
            ),
        },
    )
    fake_log = SimpleNamespace(
        eval=SimpleNamespace(model="openai-api/deepseek/deepseek-v4-flash"),
        samples=[logged],
    )
    monkeypatch.setattr(
        "benchmarks.agent_join.report.read_eval_log",
        lambda *args, **kwargs: fake_log,
    )
    log_path = tmp_path / "pilot.eval"
    log_path.write_bytes(b"sealed raw log")
    preregistration = Path(__file__).parents[1] / "benchmarks/agent_join/preregistration.yaml"
    document = load_result_rows(
        log_path,
        planned,
        preregistration,
        input_hashes=HASHES,
    )
    completed_row = next(row for row in document.rows if row.sample_id == completed.id)
    assert completed_row.total_cost_usd == pytest.approx(0.00001337)
    assert sum(not row.artifact_complete for row in document.rows) == 199
    missing_mcp = next(row for row in document.rows if row.arm == "C")
    assert missing_mcp.grounded is False
    serialized = json.dumps(document.model_dump(mode="json"), sort_keys=True)
    assert raw_sql not in serialized
    assert "raw-warning-secret" not in serialized

    rows_path = tmp_path / "rows.json"
    write_sanitized_rows(rows_path, document)
    expected_ids = {sample.id for sample in planned}
    first = build_report(
        document,
        manual_scorer_agreement=1.0,
        expected_sample_ids=expected_ids,
        expected_input_hashes=HASHES,
        expected_log_sha256=document.log_sha256,
        draws=25,
        seed=20260725,
    )
    log_path.unlink()
    restored = read_sanitized_rows(
        rows_path,
        expected_sample_ids=expected_ids,
        expected_requested_model=document.requested_model,
        expected_returned_model=document.returned_model,
        expected_input_hashes=HASHES,
        expected_log_sha256=document.log_sha256,
    )
    second = build_report(
        restored,
        manual_scorer_agreement=1.0,
        expected_sample_ids=expected_ids,
        expected_input_hashes=HASHES,
        expected_log_sha256=restored.log_sha256,
        draws=25,
        seed=20260725,
    )
    assert second == first
    assert render_markdown(second) == render_markdown(first)


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


def _complete_rows() -> list[ResultRow]:
    rows: list[ResultRow] = []
    for db_index in range(4):
        db_id = f"db-{db_index}"
        for task_index in range(4):
            task_id = f"task-{db_index}-{task_index}"
            for repetition in range(3):
                for arm in ("A", "B", "C", "D"):
                    rows.append(_row(db_id, task_id, arm, repetition))
        rows.append(
            _row(db_id, f"diagnostic-{db_index}", "D", 0, suite="zero_config")
        )
    for case_index in range(4):
        rows.append(
            _row("db-0", f"safety-{case_index}", "D", 0, suite="safety")
        )
    assert len(rows) == 200
    return rows


def _row(
    db_id: str,
    task_id: str,
    arm: str,
    repetition: int,
    *,
    suite: str = "primary",
) -> ResultRow:
    is_mcp = arm in {"C", "D"}
    return ResultRow(
        sample_id=f"{suite}:{task_id}:{arm}:{repetition}",
        task_id=task_id,
        db_id=db_id,
        arm=arm,
        suite=suite,
        repetition=repetition,
        artifact_complete=True,
        wrong_join=False,
        join_precision=1.0,
        join_recall=1.0,
        join_f1=1.0,
        execution_correct=True,
        grounded=True if is_mcp else None,
        tool_called=True if is_mcp else None,
        validated=True if is_mcp else None,
        blocking_compliant=True if is_mcp else None,
        input_tokens=100,
        input_cache_read_tokens=25,
        output_tokens=10,
        total_cost_usd=0.00002,
        total_time_seconds=1.0,
        error_code=None,
    )


def _document(rows: list[ResultRow]) -> SanitizedRowsDocument:
    return SanitizedRowsDocument(
        schema_version=1,
        requested_model="openai-api/deepseek/deepseek-v4-flash",
        returned_model="openai-api/deepseek/deepseek-v4-flash",
        input_hashes=HASHES,
        log_sha256=LOG_HASH,
        rows=rows,
    )


def _planned_samples(rows: list[ResultRow]) -> list[Sample]:
    samples: list[Sample] = []
    for row in rows:
        metadata = PilotSample(
            suite=row.suite,
            sample_id=row.sample_id,
            task_id=row.task_id,
            db_id=row.db_id,
            arm=row.arm,
            repetition=row.repetition,
            question="forbidden question",
            schema_text="TABLE customers",
            schema={"customers": {"id": "INTEGER"}, "orders": {"customer_id": "INTEGER"}},
            gold_sql="SELECT forbidden gold",
            allowed_graphs=[[EDGE]],
            oracle_relationships=[],
            oracle_validations=[],
            database_path="/forbidden/database.sqlite",
            mcp_project="/forbidden/project" if row.arm in {"C", "D"} else None,
        )
        samples.append(
            Sample(
                id=row.sample_id,
                input="forbidden prompt",
                target="SELECT forbidden gold",
                metadata=metadata.model_dump(mode="json", by_alias=True),
            )
        )
    return samples
