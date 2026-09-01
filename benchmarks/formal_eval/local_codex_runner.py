from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

from benchmarks.agent_join.execution import execute_readonly, execution_matches
from benchmarks.agent_join.sql_edges import extract_join_edges, score_join_graph
from benchmarks.formal_eval.pilot_stage import exact_two_sided_mcnemar_p_value
from joinlint.contracts import canonical_json

Condition = Literal["control", "treatment"]
CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = Path(__file__).resolve()
RESULTS_ROOT = REPOSITORY_ROOT / "benchmarks/formal_eval/results"
DEFAULT_TASKS_PATH = (
    REPOSITORY_ROOT
    / "benchmarks/formal_eval/sealed/bird-natural-schema-v2-v1/agent-tasks.json"
)
PYTHON_EXECUTABLE = REPOSITORY_ROOT / ".venv/bin/python"
MODEL_ID = "gpt-5.6-sol"
MODEL_REASONING_EFFORT = "medium"
CODEX_CLI_VERSION = "0.144.1"
EVALUATION_ID = "bird-natural-local-codex-paired-v1"
PRIMARY_OUTCOME = "final_join_graph_correct"
SQL_DEADLINE_SECONDS = 5.0
SQL_MAX_ROWS = 1_000
MODEL_TIMEOUT_SECONDS = 300
ALPHA = 0.05
CODEX_FLAGS = (
    "--strict-config",
    "--ignore-user-config",
    "--ignore-rules",
    "--ephemeral",
    "--skip-git-repo-check",
    "--dangerously-bypass-approvals-and-sandbox",
    "--json",
)
SCOPED_SOURCE_PATHS = (
    "src/joinlint",
    "benchmarks/formal_eval/database_mcp.py",
    "benchmarks/formal_eval/pilot_stage.py",
    "benchmarks/agent_join",
    "benchmarks/formal_eval/local_codex_runner.py",
)

REVIEWED_TASK_IDS = {
    "california_schools": "bird-dev-california_schools-00017",
    "card_games": "bird-dev-card_games-00429",
    "codebase_community": "bird-dev-codebase_community-00544",
    "debit_card_specializing": "bird-dev-debit_card_specializing-01478",
    "european_football_1": "bird-train-european_football_1-02774",
    "financial": "bird-dev-financial-00121",
    "formula_1": "bird-dev-formula_1-00919",
    "student_club": "bird-dev-student_club-01364",
    "superhero": "bird-dev-superhero-00740",
    "thrombosis_prediction": "bird-dev-thrombosis_prediction-01155",
    "toxicology": "bird-dev-toxicology-00325",
}
REVIEWED_DATABASE_REJECTIONS = {
    "european_football_2": "parallel declared player keys make exact-graph oracle incomplete"
}
REVIEWED_SKIPPED_TASKS = {
    "bird-dev-california_schools-00011": "redundant join and result exceeds 1000 rows",
    "bird-dev-card_games-00409": "multiple equally reasonable join topologies",
    "bird-dev-card_games-00362": "repeated ruling text gives unstable grain",
    "bird-dev-codebase_community-00635": "post-history actor is treated as author and votes set the grain",
    "bird-dev-codebase_community-00630": "post-history actor is treated as post author",
    "bird-dev-debit_card_specializing-01499": "gold computes an annualized average, not largest monthly consumption",
    "bird-dev-financial-00189": "gold graph contains a redundant account-to-district edge",
    "bird-dev-financial-00159": "account is a forced but bypassable bridge",
    "bird-dev-financial-00150": "gold counts rows rather than distinct accounts",
    "bird-dev-formula_1-00903": "gold sums cumulative standings wins rather than race wins",
    "bird-dev-formula_1-00938": "winner inference depends on a fragile time-string format",
    "bird-dev-student_club-01387": "no field identifies the budget-responsible member",
    "bird-dev-student_club-01404": "gold omits the requested expense-type grouping",
    "bird-dev-thrombosis_prediction-01297": "longitudinal-table multiplication inflates the count",
    "bird-dev-toxicology-00209": "two atom endpoints are equally declared",
    "bird-dev-toxicology-00281": "the requested tally grain is ambiguous",
    "bird-dev-toxicology-00302": "two atom endpoints are equally reasonable",
}
DISABLED_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "default_mode_request_user_input",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "memories",
    "multi_agent",
    "plugins",
    "shell_tool",
    "standalone_web_search",
    "tool_suggest",
    "unified_exec",
    "workspace_dependencies",
)
TOOL_SURFACE = {
    "control": ["EvaluationDatabase.execute_sql", "EvaluationDatabase.submit_sql"],
    "treatment": [
        "EvaluationDatabase.execute_sql",
        "EvaluationDatabase.submit_sql",
        "JoinLint.get_join_plan",
        "JoinLint.validate_sql",
    ],
}

BASE_PROMPT = """You are evaluating one SQLite question. The authoritative required_entities list is the complete physical table set for the requested answer: use every item exactly once and do not add, drop, or substitute a table. The visible schema intentionally contains no relationship declarations. You may use EvaluationDatabase execute_sql at most once to inspect data. Submit exactly once with EvaluationDatabase submit_sql as your final tool call. Submit one read-only SELECT with an empty warning, or submit empty SQL with a concise warning only when no evidence-backed join can be found. Do not put the answer only in prose."""
TREATMENT_GUIDANCE = """JoinLint standard integration: call get_join_plan before writing SQL with one {\"ref\": entity, \"entity\": entity} item for every required_entities entry. Choose start_ref and expected_grain_ref from the intended pre-aggregation grain, and use only join predicates returned by the proof. Call validate_sql on the final SQL with its plan_id. Execute and submit that exact SQL only after validation returns status ok; otherwise abstain with the stable blocking code."""
FORBIDDEN_SCHEMA = (
    re.compile(r"DECLARED\s+RELATIONSHIPS", re.I),
    re.compile(r"\bFOREIGN\s+KEY\b", re.I),
    re.compile(r"\bREFERENCES\b", re.I),
)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as destination:
        destination.write(content)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary, path)


def _write_json(path: Path, value: object) -> None:
    _atomic(path, canonical_json(value) + b"\n")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def strip_declared_relationships(schema_text: str) -> str:
    marker = re.search(r"(?:\r?\n)+DECLARED\s+RELATIONSHIPS\b", schema_text, re.I)
    visible = schema_text[: marker.start()].rstrip() if marker else schema_text.rstrip()
    if not visible or any(pattern.search(visible) for pattern in FORBIDDEN_SCHEMA):
        raise ValueError("visible schema is empty or leaks relationship metadata")
    return visible


def _assert_blind_prompt(task: Mapping[str, object], prompt: str, condition: Condition) -> None:
    lowered = prompt.casefold()
    hidden = [task["gold_sql"], task["database_path"], "allowed_graphs", "gold_sql"]
    hidden.extend(endpoint for graph in task["allowed_graphs"] for edge in graph for endpoint in edge)
    if any(str(value).casefold() in lowered for value in hidden):
        raise ValueError("prompt leaks hidden oracle input")
    if any(pattern.search(prompt) for pattern in FORBIDDEN_SCHEMA):
        raise ValueError("prompt leaks relationship metadata")
    if condition == "control" and TREATMENT_GUIDANCE in prompt:
        raise ValueError("control prompt contains treatment guidance")


def render_prompt(task: Mapping[str, object], condition: Condition) -> str:
    common = (
        BASE_PROMPT
        + f"\n\nQuestion:\n{task['question']}"
        + "\n\nAuthoritative required_entities:\n"
        + json.dumps(task["expected_entities"], ensure_ascii=False, separators=(",", ":"))
        + f"\n\nVisible schema:\n{strip_declared_relationships(str(task['schema_text']))}"
    )
    prompt = common if condition == "control" else f"{common}\n\n{TREATMENT_GUIDANCE}"
    _assert_blind_prompt(task, prompt, condition)
    return prompt


def _join_depth(task: Mapping[str, object]) -> int:
    return max(len(graph) for graph in task["allowed_graphs"])


def rank_tasks(tasks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        tasks,
        key=lambda task: (
            -_join_depth(task),
            hashlib.sha256(task["task_id"].encode()).digest(),
        ),
    )


def deterministic_run_order(selected: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    ordered = sorted(selected, key=lambda task: hashlib.sha256(task["task_id"].encode()).digest())
    result: list[dict[str, str]] = []
    for index, task in enumerate(ordered):
        conditions = ("control", "treatment") if index % 2 == 0 else ("treatment", "control")
        result.extend(
            {
                "database_id": task["database_id"],
                "task_id": task["task_id"],
                "condition": condition,
            }
            for condition in conditions
        )
    return result


def _load_tasks(path: Path) -> list[dict[str, Any]]:
    tasks = json.loads(path.read_text(encoding="utf-8"))
    if path.is_symlink() or not isinstance(tasks, list):
        raise ValueError("agent task input must be a regular JSON array")
    return tasks


def _database(tasks_path: Path, relative: str) -> Path:
    root = tasks_path.resolve().parent.parent
    candidate = root / relative
    if candidate.is_symlink():
        raise ValueError("sealed database cannot be a symlink")
    resolved = candidate.resolve(strict=True)
    resolved.relative_to(root)
    if not resolved.is_file():
        raise ValueError("sealed database must be a regular file")
    return resolved


def _snapshot(source: Path, destination: Path) -> None:
    uri = f"file:{quote(source.as_posix(), safe='/')}?mode=ro"
    source_db = sqlite3.connect(uri, uri=True)
    snapshot_db = sqlite3.connect(destination)
    try:
        source_db.backup(snapshot_db)
    finally:
        snapshot_db.close()
        source_db.close()


def _command_output(command: list[str], runner: CommandRunner, **kwargs: object) -> str:
    result = runner(command, capture_output=True, text=True, check=False, timeout=10, **kwargs)
    if result.returncode:
        raise RuntimeError(f"command failed: {command[0]}")
    return result.stdout.strip()


def _codex_version(codex: str, runner: CommandRunner) -> str:
    output = _command_output([codex, "--version"], runner)
    if output != f"codex-cli {CODEX_CLI_VERSION}":
        raise RuntimeError(f"Codex CLI version drift: {output}")
    return CODEX_CLI_VERSION


def _codex_authentication(codex: str, runner: CommandRunner) -> str:
    result = runner(
        [codex, "login", "status"], capture_output=True, text=True, check=False, timeout=10
    )
    lines = (result.stdout + result.stderr).splitlines()
    if result.returncode or "Logged in using ChatGPT" not in lines:
        raise RuntimeError("Codex must use the existing ChatGPT login")
    return "Logged in using ChatGPT"


def _assert_clean_sources(runner: CommandRunner) -> None:
    status = _command_output(
        ["git", "status", "--porcelain", "--untracked-files=no", "--", *SCOPED_SOURCE_PATHS],
        runner,
        cwd=REPOSITORY_ROOT,
    )
    if status:
        raise RuntimeError("evaluation source paths have tracked worktree changes")


def _surfaces() -> dict[str, str]:
    return {
        "prompt_surface_sha256": _digest([BASE_PROMPT, TREATMENT_GUIDANCE]),
        "tool_surface_sha256": _digest(
            [
                MODEL_ID,
                MODEL_REASONING_EFFORT,
                CODEX_CLI_VERSION,
                CODEX_FLAGS,
                DISABLED_FEATURES,
                TOOL_SURFACE,
                "unguarded_submit",
                SQL_DEADLINE_SECONDS,
                SQL_MAX_ROWS,
                MODEL_TIMEOUT_SECONDS,
                ALPHA,
            ]
        ),
    }


def _task_lock(task: Mapping[str, object], snapshot_sha256: str) -> dict[str, object]:
    return {
        "database_id": task["database_id"],
        "task_id": task["task_id"],
        "database_path": task["database_path"],
        "join_depth": _join_depth(task),
        "selection_tiebreak_sha256": hashlib.sha256(task["task_id"].encode()).hexdigest(),
        "question_sha256": _digest(task["question"]),
        "visible_schema_sha256": _digest(strip_declared_relationships(str(task["schema_text"]))),
        "required_entities": task["expected_entities"],
        "allowed_graphs_sha256": _digest(task["allowed_graphs"]),
        "gold_sql_sha256": _digest(task["gold_sql"]),
        "control_prompt_sha256": _digest(render_prompt(task, "control")),
        "treatment_prompt_sha256": _digest(render_prompt(task, "treatment")),
        "database_snapshot_sha256": snapshot_sha256,
    }


def prepare_evaluation(
    output_dir: Path,
    *,
    tasks_path: Path = DEFAULT_TASKS_PATH,
    codex: str | None = None,
    runner: CommandRunner | None = None,
) -> Path:
    run = runner or subprocess.run
    codex_path = codex or shutil.which("codex")
    if not codex_path or not PYTHON_EXECUTABLE.is_file():
        raise RuntimeError("local Codex or project Python is unavailable")
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError("prepare output directory must be empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    _codex_version(codex_path, run)
    authentication = _codex_authentication(codex_path, run)
    _assert_clean_sources(run)

    tasks = _load_tasks(tasks_path)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        grouped[task["database_id"]].append(task)
    expected_databases = set(REVIEWED_TASK_IDS) | set(REVIEWED_DATABASE_REJECTIONS)
    if set(grouped) != expected_databases:
        raise ValueError("source database set drifted from the blind review")

    selected: list[dict[str, Any]] = []
    locks: list[dict[str, object]] = []
    database_locks: dict[str, dict[str, str]] = {}
    decisions: list[dict[str, str | None]] = []
    candidate_order = {
        database_id: [task["task_id"] for task in rank_tasks(candidates)]
        for database_id, candidates in sorted(grouped.items())
    }
    skipped_before_selected = {
        task_id
        for database_id, selected_id in REVIEWED_TASK_IDS.items()
        for task_id in candidate_order[database_id][
            : candidate_order[database_id].index(selected_id)
        ]
    }
    if skipped_before_selected != set(REVIEWED_SKIPPED_TASKS):
        raise ValueError("blind-review candidate ordering drifted")
    with tempfile.TemporaryDirectory(prefix="joinlint-local-prepare-") as temporary:
        for database_id, reviewed_id in REVIEWED_TASK_IDS.items():
            task = next(task for task in grouped[database_id] if task["task_id"] == reviewed_id)
            source = _database(tasks_path, task["database_path"])
            snapshot = Path(temporary) / f"{database_id}.sqlite"
            _snapshot(source, snapshot)
            snapshot_sha = _file_digest(snapshot)
            eligibility = execute_readonly(
                snapshot,
                task["gold_sql"],
                deadline_seconds=SQL_DEADLINE_SECONDS,
                max_rows=SQL_MAX_ROWS,
            )
            if not eligibility.executed:
                raise ValueError(f"reviewed gold is not evaluable: {reviewed_id}:{eligibility.error_code}")
            selected.append(task)
            locks.append(_task_lock(task, snapshot_sha))
            database_locks[database_id] = {
                "database_path": task["database_path"],
                "logical_snapshot_sha256": snapshot_sha,
            }
            decisions.append(
                {
                    "database_id": database_id,
                    "selected_task_id": reviewed_id,
                    "decision": "accepted",
                    "reason": "blind review accepted a complete exact-graph oracle",
                }
            )
    decisions.extend(
        {
            "database_id": database_id,
            "selected_task_id": None,
            "decision": "excluded_database",
            "reason": reason,
        }
        for database_id, reason in REVIEWED_DATABASE_REJECTIONS.items()
    )

    input_lock = {
        "schema_version": 1,
        "evaluation_id": EVALUATION_ID,
        "tasks_path": str(tasks_path.resolve()),
        "tasks_sha256": _file_digest(tasks_path),
        "selection_rule": (
            "fixed pre-model blind-reviewed allowlist; (-join_depth, sha256(task_id)) "
            "candidate order is audit-only; no post-run replacement"
        ),
        "candidate_order": candidate_order,
        "reviewed_skipped_tasks": REVIEWED_SKIPPED_TASKS,
        "reviewer_decisions": decisions,
        "selected_tasks": locks,
        "database_locks": database_locks,
        **_surfaces(),
    }
    input_lock_path = output_dir / "input-lock.json"
    _write_json(input_lock_path, input_lock)
    run_order = deterministic_run_order(selected)
    preregistration = {
        "schema_version": 1,
        "evaluation_id": EVALUATION_ID,
        "created_at": datetime.now(UTC).isoformat(),
        "estimand": "JoinLint plus standard integration guidance",
        "claim_scope": "11-database single-run local paired replication (10 dev, 1 train)",
        "power_boundary": (
            "With zero control-only wins, at least 6 treatment-only wins are required for "
            "two-sided p<0.05; this run can detect only large effects and is not "
            "population-level proof."
        ),
        "primary_outcome": PRIMARY_OUTCOME,
        "secondary_outcome": "execution_equivalent",
        "statistical_test": "exact_two_sided_mcnemar",
        "alpha": ALPHA,
        "paired_unit_count": len(selected),
        "run_count": len(run_order),
        "model_id": MODEL_ID,
        "model_reasoning_effort": MODEL_REASONING_EFFORT,
        "codex_cli_version": CODEX_CLI_VERSION,
        "codex_executable": str(Path(codex_path).resolve()),
        "runner_sha256": _file_digest(RUNNER_PATH),
        "joinlint_commit": _command_output(
            ["git", "rev-parse", "HEAD"], run, cwd=REPOSITORY_ROOT
        ),
        "input_lock_sha256": _file_digest(input_lock_path),
        **_surfaces(),
        "tool_surface": TOOL_SURFACE,
        "disabled_features": list(DISABLED_FEATURES),
        "database_submission_guard": "none in both conditions",
        "run_order": run_order,
        "cost_provenance": {
            "authentication": authentication,
            "environment_policy": "fixed non-secret runtime allowlist",
            "incremental_cash_spend_cny": 0,
            "note": "ChatGPT quota is consumed; token usage has no CNY unit price.",
        },
    }
    if len(selected) != 11:
        raise ValueError("blind-reviewed allowlist must contain 11 tasks")
    _write_json(output_dir / "preregistration.json", preregistration)
    return output_dir


def build_codex_command(
    *, codex: str, condition: Condition, cwd: Path, database: Path, project: Path
) -> list[str]:
    command = [
        codex,
        "exec",
        *CODEX_FLAGS,
        "--model",
        MODEL_ID,
        "-C",
        str(cwd),
        "-c",
        f'model_reasoning_effort="{MODEL_REASONING_EFFORT}"',
    ]
    for feature in DISABLED_FEATURES:
        command += ["-c", f"features.{feature}=false"]
    database_args = ["-m", "benchmarks.formal_eval.database_mcp", "--database", str(database)]
    command += [
        "-c",
        f"mcp_servers.EvaluationDatabase.command={json.dumps(str(PYTHON_EXECUTABLE))}",
        "-c",
        f"mcp_servers.EvaluationDatabase.args={json.dumps(database_args, separators=(',', ':'))}",
        "-c",
        f"mcp_servers.EvaluationDatabase.cwd={json.dumps(str(REPOSITORY_ROOT))}",
    ]
    if condition == "treatment":
        args = ["-m", "joinlint", "serve-mcp", "--auto", "--project", str(project)]
        command += [
            "-c",
            f"mcp_servers.JoinLint.command={json.dumps(str(PYTHON_EXECUTABLE))}",
            "-c",
            f"mcp_servers.JoinLint.args={json.dumps(args, separators=(',', ':'))}",
            "-c",
            f"mcp_servers.JoinLint.cwd={json.dumps(str(REPOSITORY_ROOT))}",
        ]
    return command + ["-"]


def _successful(item: Mapping[str, object]) -> bool:
    result = item.get("result")
    structured = result.get("structured_content") if isinstance(result, dict) else None
    return bool(
        item.get("status") == "completed"
        and item.get("error") is None
        and isinstance(structured, dict)
        and structured.get("status") == "ok"
    )


def parse_codex_trace(stdout: str) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    invalid = 0
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid += 1
            continue
        if isinstance(event, dict):
            events.append(event)
    tools = [
        event["item"]
        for event in events
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "mcp_tool_call"
    ]
    successful = [item for item in tools if _successful(item)]
    submits = [
        item
        for item in successful
        if item.get("server") == "EvaluationDatabase" and item.get("tool") == "submit_sql"
    ]
    submission = None
    if len(submits) == 1:
        arguments = submits[0].get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = None
        if isinstance(arguments, dict) and set(arguments) == {"sql", "warning"}:
            if all(isinstance(arguments[key], str) for key in ("sql", "warning")):
                submission = {"sql": arguments["sql"].strip(), "warning": arguments["warning"]}
    usage: dict[str, int] = defaultdict(int)
    completed = False
    for event in events:
        if event.get("type") == "turn.completed":
            completed = True
            for key, value in (event.get("usage") or {}).items():
                if isinstance(value, int):
                    usage[key] += value
    validations = [
        item
        for item in successful
        if item.get("server") == "JoinLint" and item.get("tool") == "validate_sql"
    ]
    return {
        "invalid_json_lines": invalid,
        "turn_completed": completed,
        "usage": dict(usage),
        "successful_submit_count": len(submits),
        "submission": submission,
        "tool_calls": [f"{item.get('server')}.{item.get('tool')}" for item in tools],
        "plan_called": any(
            item.get("server") == "JoinLint" and item.get("tool") == "get_join_plan"
            for item in successful
        ),
        "validate_called": bool(validations),
        "validated_final_sql": bool(
            submission
            and any(
                isinstance(item.get("arguments"), dict)
                and item["arguments"].get("sql") == submission["sql"]
                for item in validations
            )
        ),
    }


def _score(
    task: Mapping[str, Any],
    condition: Condition,
    stdout: str,
    returncode: int,
    database: Path,
    input_lock_sha256: str,
    command_sha256: str | None,
    recovered: bool,
) -> dict[str, Any]:
    trace = parse_codex_trace(stdout)
    infrastructure_ok = bool(
        returncode == 0 and trace["turn_completed"] and not trace["invalid_json_lines"]
    )
    submission = trace["submission"]
    join_score = None
    join_error = None
    if submission and submission["sql"]:
        try:
            edges = extract_join_edges(submission["sql"], task["schema"])
            join_score = score_join_graph(edges, task["allowed_graphs"]).model_dump(mode="json")
        except ValueError as error:
            join_error = str(error)
    execution_equivalent = False
    execution_error = None
    if infrastructure_ok and submission and submission["sql"]:
        execution = execution_matches(
            database,
            task["task_id"],
            task["gold_sql"],
            submission["sql"],
            deadline_seconds=SQL_DEADLINE_SECONDS,
            max_rows=SQL_MAX_ROWS,
        )
        execution_equivalent = execution.equivalent is True
        execution_error = execution.error_code
    return {
        "schema_version": 1,
        "evaluation_id": EVALUATION_ID,
        "input_lock_sha256": input_lock_sha256,
        "task_id": task["task_id"],
        "database_id": task["database_id"],
        "condition": condition,
        "returncode": returncode,
        "infrastructure_ok": infrastructure_ok,
        "prompt_sha256": _digest(render_prompt(task, condition)),
        "command_sha256": command_sha256,
        "successful_submit_count": trace["successful_submit_count"],
        "submitted_sql": submission["sql"] if submission else None,
        "submitted_warning": submission["warning"] if submission else None,
        "join_score": join_score,
        "join_error": join_error,
        PRIMARY_OUTCOME: bool(
            infrastructure_ok and join_score and join_score["wrong_join"] is False
        ),
        "execution_equivalent": execution_equivalent,
        "execution_error": execution_error,
        "tool_calls": trace["tool_calls"],
        "plan_called": trace["plan_called"],
        "validate_called": trace["validate_called"],
        "validated_final_sql": trace["validated_final_sql"],
        "usage": trace["usage"],
        "recovered_from_raw": recovered,
    }


def _stem(task_id: str, condition: Condition) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", task_id):
        raise ValueError("unsafe task ID")
    return f"{task_id}--{condition}"


def _checkpoint(output_dir: Path, task_id: str, condition: Condition) -> Path:
    return output_dir / "checkpoints" / f"{_stem(task_id, condition)}.json"


def _codex_environment() -> dict[str, str]:
    allowed = {
        "CODEX_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "XDG_CACHE_HOME",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _run_cell(
    output_dir: Path,
    task: Mapping[str, Any],
    condition: Condition,
    snapshot: Path,
    codex: str,
    runner: CommandRunner,
    input_lock_sha256: str,
) -> dict[str, Any]:
    checkpoint = _checkpoint(output_dir, task["task_id"], condition)
    if checkpoint.exists():
        row = _read_json(checkpoint)
        if (
            row["input_lock_sha256"] != input_lock_sha256
            or row["task_id"] != task["task_id"]
            or row["condition"] != condition
            or row["evaluation_id"] != EVALUATION_ID
            or row["prompt_sha256"] != _digest(render_prompt(task, condition))
        ):
            raise ValueError("checkpoint lineage mismatch")
        return row
    stem = _stem(task["task_id"], condition)
    raw = output_dir / "raw"
    stdout_path = raw / f"{stem}.stdout.jsonl"
    stderr_path = raw / f"{stem}.stderr.txt"
    process_path = raw / f"{stem}.process.json"
    raw_paths = (stdout_path, stderr_path, process_path)
    present = [path.exists() for path in raw_paths]
    if any(present) and not all(present):
        raise ValueError("partial raw cell exists; refusing to rerun")
    recovered = all(present)
    prompt = render_prompt(task, condition)
    with tempfile.TemporaryDirectory(prefix=f"joinlint-{condition}-") as temporary:
        task_root = Path(temporary)
        cwd, data = task_root / "cwd", task_root / "data"
        cwd.mkdir()
        data.mkdir()
        database = data / "database.sqlite"
        shutil.copy2(snapshot, database)
        command = build_codex_command(
            codex=codex, condition=condition, cwd=cwd, database=database, project=data
        )
        command_sha = _digest(command)
        normalized_command = command.copy()
        for index, item in enumerate(normalized_command):
            for value, label in (
                (str(PYTHON_EXECUTABLE), "<python>"),
                (str(database), "<database>"),
                (str(data), "<project>"),
                (str(cwd), "<cwd>"),
                (str(REPOSITORY_ROOT), "<repo>"),
            ):
                item = item.replace(value, label)
            normalized_command[index] = item
        process_manifest = {
            "task_id": task["task_id"],
            "condition": condition,
            "input_lock_sha256": input_lock_sha256,
            "prompt_sha256": _digest(prompt),
            "normalized_command": normalized_command,
            "normalized_command_sha256": _digest(normalized_command),
        }
        if recovered:
            process = _read_json(process_path)
            if any(process.get(key) != value for key, value in process_manifest.items()):
                raise ValueError("raw cell lineage mismatch")
            if process.get("state") != "completed" or not isinstance(process.get("returncode"), int):
                raise ValueError("raw cell process is incomplete")
            if process.get("stdout_sha256") != _file_digest(stdout_path) or process.get(
                "stderr_sha256"
            ) != _file_digest(stderr_path):
                raise ValueError("raw cell content digest mismatch")
            stdout = stdout_path.read_text(encoding="utf-8")
            returncode = process["returncode"]
            command_sha = process["command_sha256"]
        else:
            _write_json(process_path, {**process_manifest, "state": "started"})
            started = time.monotonic()
            try:
                result = runner(
                    command,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=MODEL_TIMEOUT_SECONDS,
                    env=_codex_environment(),
                )
                stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
            except subprocess.TimeoutExpired as error:
                stdout = error.stdout if isinstance(error.stdout, str) else ""
                stderr = error.stderr if isinstance(error.stderr, str) else ""
                returncode = 124
            _atomic(stdout_path, stdout.encode())
            _atomic(stderr_path, stderr.encode())
            _write_json(
                process_path,
                {
                    **process_manifest,
                    "state": "completed",
                    "returncode": returncode,
                    "elapsed_seconds": time.monotonic() - started,
                    "command_sha256": command_sha,
                    "stdout_sha256": _file_digest(stdout_path),
                    "stderr_sha256": _file_digest(stderr_path),
                },
            )
        row = _score(
            task,
            condition,
            stdout,
            returncode,
            database,
            input_lock_sha256,
            command_sha,
            recovered,
        )
    _write_json(checkpoint, row)
    return row


def _effect(rows: list[dict[str, Any]], outcome: str) -> dict[str, object]:
    pairs: dict[str, dict[str, bool]] = defaultdict(dict)
    for row in rows:
        pairs[row["task_id"]][row["condition"]] = bool(row[outcome])
    if any(set(pair) != {"control", "treatment"} for pair in pairs.values()):
        raise ValueError("effect requires complete pairs")
    values = [(pair["control"], pair["treatment"]) for pair in pairs.values()]
    treatment_wins = sum(not control and treatment for control, treatment in values)
    control_wins = sum(control and not treatment for control, treatment in values)
    both_success = sum(control and treatment for control, treatment in values)
    control_successes = sum(control for control, _ in values)
    treatment_successes = sum(treatment for _, treatment in values)
    p_value = exact_two_sided_mcnemar_p_value(
        treatment_wins=treatment_wins, control_wins=control_wins
    )
    improvement = (treatment_successes - control_successes) / len(values)
    return {
        "paired_unit_count": len(values),
        "control_successes": control_successes,
        "treatment_successes": treatment_successes,
        "treatment_wins": treatment_wins,
        "control_wins": control_wins,
        "both_success": both_success,
        "both_failure": len(values) - treatment_wins - control_wins - both_success,
        "absolute_improvement": improvement,
        "exact_two_sided_mcnemar_p_value": p_value,
        "significant_positive_improvement": improvement > 0 and p_value < ALPHA,
    }


def run_evaluation(output_dir: Path, *, runner: CommandRunner | None = None) -> Path:
    run = runner or subprocess.run
    prereg = _read_json(output_dir / "preregistration.json")
    lock_path = output_dir / "input-lock.json"
    lock = _read_json(lock_path)
    lock_sha = _file_digest(lock_path)
    if prereg["input_lock_sha256"] != lock_sha or any(
        prereg[key] != value or lock[key] != value
        for key, value in _surfaces().items()
    ):
        raise ValueError("preregistration lineage or surface drift")
    if _file_digest(RUNNER_PATH) != prereg["runner_sha256"]:
        raise ValueError("runner drifted after preregistration")
    if _command_output(["git", "rev-parse", "HEAD"], run, cwd=REPOSITORY_ROOT) != prereg[
        "joinlint_commit"
    ]:
        raise ValueError("JoinLint commit drifted after preregistration")
    codex = prereg["codex_executable"]
    _codex_version(codex, run)
    _codex_authentication(codex, run)
    _assert_clean_sources(run)
    tasks_path = Path(lock["tasks_path"])
    if _file_digest(tasks_path) != lock["tasks_sha256"]:
        raise ValueError("sealed tasks drifted")
    all_tasks = {task["task_id"]: task for task in _load_tasks(tasks_path)}
    selected = {entry["task_id"]: all_tasks[entry["task_id"]] for entry in lock["selected_tasks"]}
    expected_order = deterministic_run_order(selected.values())
    if (
        list(selected) != list(REVIEWED_TASK_IDS.values())
        or prereg["run_order"] != expected_order
        or prereg["paired_unit_count"] != 11
        or prereg["run_count"] != 22
    ):
        raise ValueError("frozen paired run order drifted")
    cells_by_task: dict[str, list[dict[str, str]]] = defaultdict(list)
    for cell in expected_order:
        cells_by_task[cell["task_id"]].append(cell)

    rows: dict[tuple[str, str], dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="joinlint-local-run-") as temporary:
        snapshots: dict[str, Path] = {}
        for task_id, cells in cells_by_task.items():
            if all(_checkpoint(output_dir, task_id, cell["condition"]).exists() for cell in cells):
                continue
            task = selected[task_id]
            snapshot = Path(temporary) / f"{task['database_id']}.sqlite"
            _snapshot(_database(tasks_path, task["database_path"]), snapshot)
            expected = lock["database_locks"][task["database_id"]]["logical_snapshot_sha256"]
            if _file_digest(snapshot) != expected:
                raise ValueError(f"database drifted: {task['database_id']}")
            if _task_lock(task, expected) not in lock["selected_tasks"]:
                raise ValueError(f"selected task drifted: {task_id}")
            snapshots[task_id] = snapshot
        for task_id, cells in cells_by_task.items():
            for cell in cells:
                condition = cell["condition"]
                row = _run_cell(
                    output_dir,
                    selected[task_id],
                    condition,
                    snapshots.get(task_id, Path()),
                    codex,
                    run,
                    lock_sha,
                )
                rows[(task_id, condition)] = row

    ordered = [rows[(cell["task_id"], cell["condition"])] for cell in prereg["run_order"]]
    _atomic(output_dir / "results.jsonl", b"".join(canonical_json(row) + b"\n" for row in ordered))
    usage: dict[str, int] = defaultdict(int)
    for row in ordered:
        for key, value in row["usage"].items():
            usage[key] += value
    effect = {
        "schema_version": 1,
        "evaluation_id": EVALUATION_ID,
        "input_lock_sha256": lock_sha,
        "estimand": prereg["estimand"],
        "primary_outcome": PRIMARY_OUTCOME,
        "alpha": ALPHA,
        "primary_effect": _effect(ordered, PRIMARY_OUTCOME),
        "secondary_execution_effect": _effect(ordered, "execution_equivalent"),
        "mechanism": {
            "treatment_plan_called": sum(
                row["condition"] == "treatment" and row["plan_called"] for row in ordered
            ),
            "treatment_validate_called": sum(
                row["condition"] == "treatment" and row["validate_called"] for row in ordered
            ),
            "treatment_final_sql_validated": sum(
                row["condition"] == "treatment" and row["validated_final_sql"] for row in ordered
            ),
        },
        "usage_tokens": dict(usage),
        "chatgpt_quota_consumption": {
            "completed_turns": sum(row["infrastructure_ok"] for row in ordered),
            "cny_unit_price_available": False,
        },
        "incremental_cash_spend_cny": 0,
        "cost_note": "Existing ChatGPT login consumed quota; zero CNY is not zero compute.",
    }
    _write_json(output_dir / "effect.json", effect)
    return output_dir


def _result_dir(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise ValueError("unsafe run ID")
    return RESULTS_ROOT / run_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local paired Codex evaluation")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("--run-id", required=True)
    arguments = parser.parse_args(argv)
    output = _result_dir(arguments.run_id)
    if arguments.command == "prepare":
        prepare_evaluation(output)
        print(f"prepared={output}")
    else:
        run_evaluation(output)
        print(f"completed={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
