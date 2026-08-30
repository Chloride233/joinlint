from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks.formal_eval import local_codex_runner as runner


def _completed_item(server: str, tool: str, arguments: dict[str, str]) -> dict[str, object]:
    return {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": server,
            "tool": tool,
            "arguments": arguments,
            "result": {"structured_content": {"status": "ok"}},
            "error": None,
            "status": "completed",
        },
    }


def _stdout(*, treatment: bool) -> str:
    sql = "SELECT child.id FROM child JOIN parent ON child.parent_id = parent.id"
    events: list[dict[str, object]] = [{"type": "turn.started"}]
    if treatment:
        events.extend(
            [
                _completed_item("JoinLint", "get_join_plan", {}),
                _completed_item("JoinLint", "validate_sql", {"sql": sql}),
            ]
        )
    events.extend(
        [
            _completed_item(
                "EvaluationDatabase", "submit_sql", {"sql": sql, "warning": ""}
            ),
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 40,
                    "output_tokens": 20,
                },
            },
        ]
    )
    return "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events)


def _write_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE parent(id INTEGER PRIMARY KEY);
            CREATE TABLE child(
                id INTEGER PRIMARY KEY,
                parent_id INTEGER NOT NULL REFERENCES parent(id)
            );
            INSERT INTO parent VALUES (1), (2);
            INSERT INTO child VALUES (10, 1), (20, 2);
            """
        )
        connection.commit()
    finally:
        connection.close()


def _task(database_id: str, task_id: str) -> dict[str, object]:
    return {
        "allowed_graphs": [[["child.parent_id", "parent.id"]]],
        "database_id": database_id,
        "database_path": f"databases/{database_id}.sqlite",
        "expected_entities": ["child", "parent"],
        "gold_sql": "SELECT child.id FROM child JOIN parent ON child.parent_id = parent.id",
        "oracle_has_safe_path": True,
        "question": f"List child IDs for {database_id} with their parent relationship.",
        "schema": {
            "child": {"id": "integer", "parent_id": "integer"},
            "parent": {"id": "integer"},
        },
        "schema_text": (
            "TABLE child (\n  id INTEGER PRIMARY KEY,\n  parent_id INTEGER\n)\n\n"
            "TABLE parent (\n  id INTEGER PRIMARY KEY\n)\n\n"
            "DECLARED RELATIONSHIPS\n  child.parent_id = parent.id"
        ),
        "sql_shape": "child.parent_id=parent.id",
        "task_id": task_id,
    }


@pytest.fixture
def frozen_tasks(tmp_path: Path) -> Path:
    sealed = tmp_path / "sealed"
    tasks_path = sealed / "suite" / "agent-tasks.json"
    tasks_path.parent.mkdir(parents=True)
    tasks: list[dict[str, object]] = []
    for database_id, task_id in runner.REVIEWED_TASK_IDS.items():
        _write_database(sealed / "databases" / f"{database_id}.sqlite")
        tasks.append(_task(database_id, task_id))
        for skipped_id in runner.REVIEWED_SKIPPED_TASKS:
            if f"-{database_id}-" not in skipped_id:
                continue
            skipped = _task(database_id, skipped_id)
            skipped["allowed_graphs"] = [
                [["child.parent_id", "parent.id"], ["child.id", "parent.id"]]
            ]
            tasks.append(skipped)
    for database_id in runner.REVIEWED_DATABASE_REJECTIONS:
        _write_database(sealed / "databases" / f"{database_id}.sqlite")
        tasks.append(_task(database_id, f"candidate-{database_id}"))
    tasks_path.write_text(json.dumps(tasks), encoding="utf-8")
    return tasks_path


class FakeCommands:
    def __init__(self) -> None:
        self.model_commands: list[list[str]] = []
        self.prompts: list[str] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(command, 0, "a" * 40 + "\n", "")
        if command[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "codex-cli 0.144.1\n", "")
        if command[-2:] == ["login", "status"]:
            return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT\n", "")
        self.model_commands.append(command)
        self.prompts.append(str(kwargs["input"]))
        treatment = any("mcp_servers.JoinLint.command=" in item for item in command)
        return subprocess.CompletedProcess(command, 0, _stdout(treatment=treatment), "")


def test_prompts_hide_all_oracle_relationship_inputs() -> None:
    task = _task("california_schools", "task-1")
    control = runner.render_prompt(task, "control")
    treatment = runner.render_prompt(task, "treatment")

    assert treatment == control + "\n\n" + runner.TREATMENT_GUIDANCE
    assert "required_entities" in control
    assert task["question"] in control
    assert runner.strip_declared_relationships(str(task["schema_text"])) in control
    for hidden in (
        str(task["gold_sql"]),
        str(task["database_path"]),
        "allowed_graphs",
        "child.parent_id",
        "parent.id",
        "DECLARED RELATIONSHIPS",
        "FOREIGN KEY",
        "REFERENCES",
    ):
        assert hidden.casefold() not in control.casefold()
        assert hidden.casefold() not in treatment.casefold()


def test_commands_pin_model_config_and_only_add_joinlint_to_treatment(tmp_path: Path) -> None:
    common = {
        "codex": "/fake/codex",
        "cwd": tmp_path / "cwd",
        "database": tmp_path / "data" / "database.sqlite",
        "project": tmp_path / "data",
    }
    control = runner.build_codex_command(condition="control", **common)
    treatment = runner.build_codex_command(condition="treatment", **common)

    for command in (control, treatment):
        assert "--strict-config" in command
        assert "--ignore-user-config" in command
        assert "--ignore-rules" in command
        assert "--ephemeral" in command
        assert command[command.index("--model") + 1] == "gpt-5.6-sol"
        assert "model_reasoning_effort=\"medium\"" in command
        for feature in runner.DISABLED_FEATURES:
            assert f"features.{feature}=false" in command
        assert not any("validation-ledger" in item for item in command)
    assert not any("mcp_servers.JoinLint" in item for item in control)
    assert any("mcp_servers.JoinLint" in item for item in treatment)


def test_trace_requires_exactly_one_successful_submit() -> None:
    parsed = runner.parse_codex_trace(_stdout(treatment=True))
    assert parsed["submission"]["sql"].startswith("SELECT child.id")
    assert parsed["successful_submit_count"] == 1
    assert parsed["plan_called"] is True
    assert parsed["validated_final_sql"] is True

    duplicated = _stdout(treatment=False) + json.dumps(
        _completed_item(
            "EvaluationDatabase", "submit_sql", {"sql": "SELECT 1", "warning": ""}
        )
    )
    parsed = runner.parse_codex_trace(duplicated)
    assert parsed["successful_submit_count"] == 2
    assert parsed["submission"] is None


def test_prepare_run_and_resume_without_real_model_calls(
    frozen_tasks: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "PYTHON_EXECUTABLE", Path(sys.executable).resolve())
    fake = FakeCommands()
    output = tmp_path / "results"

    runner.prepare_evaluation(
        output,
        tasks_path=frozen_tasks,
        codex="/fake/codex",
        runner=fake,
    )

    assert fake.model_commands == []
    preregistration = json.loads((output / "preregistration.json").read_text())
    input_lock = json.loads((output / "input-lock.json").read_text())
    assert preregistration["paired_unit_count"] == 11
    assert preregistration["run_count"] == 22
    assert preregistration["model_reasoning_effort"] == "medium"
    exclusion = next(
        decision
        for decision in input_lock["reviewer_decisions"]
        if decision["decision"] == "excluded_database"
    )
    assert exclusion["reason"] == "parallel declared player keys make exact-graph oracle incomplete"

    runner.run_evaluation(output, runner=fake)

    assert len(fake.model_commands) == 22
    assert len(list((output / "checkpoints").glob("*.json"))) == 22
    assert len(list((output / "raw").glob("*.process.json"))) == 22
    effect = json.loads((output / "effect.json").read_text())
    assert effect["primary_effect"]["control_successes"] == 11
    assert effect["primary_effect"]["treatment_successes"] == 11
    assert effect["primary_effect"]["exact_two_sided_mcnemar_p_value"] == 1.0
    assert effect["incremental_cash_spend_cny"] == 0
    assert effect["usage_tokens"]["input_tokens"] == 2_200

    checkpoint = next((output / "checkpoints").glob("*.json"))
    checkpoint.unlink()
    runner.run_evaluation(output, runner=fake)

    assert len(fake.model_commands) == 22
    recovered = [
        json.loads(path.read_text())
        for path in (output / "checkpoints").glob("*.json")
        if json.loads(path.read_text())["recovered_from_raw"]
    ]
    assert len(recovered) == 1

    checkpoint.unlink()
    (output / "raw" / f"{checkpoint.stem}.process.json").unlink()
    with pytest.raises(ValueError, match="partial raw cell"):
        runner.run_evaluation(output, runner=fake)
    assert len(fake.model_commands) == 22
