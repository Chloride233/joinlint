from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from inspect_ai import Task, eval
from inspect_ai.agent import AgentSubmit, as_solver, react
from inspect_ai.dataset import Sample
from inspect_ai.model import (
    ChatMessageAssistant,
    ChatMessageTool,
    Model,
    ModelOutput,
    get_model,
)
from inspect_ai.solver import Generate, Solver, TaskState, solver
from inspect_ai.tool import (
    MCPServer,
    Tool,
    ToolDef,
    ToolParams,
    ToolSource,
    ToolCall,
    mcp_server_stdio,
    tool,
)
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from benchmarks.agent_join.contracts import Arm, PilotSample, SelectedTask
from benchmarks.agent_join.prepare_spider import seeded_rank
from benchmarks.agent_join.scorers import (
    execution_scorer,
    join_outcome_scorer,
    mcp_trace_scorer,
)
from joinlint.contracts import canonical_json
from joinlint.model import load_model
from joinlint.services import get_data_model, validate_cached_edges


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = Path(__file__).resolve().parent
BASE_PROMPT = (PACKAGE_ROOT / "prompts" / "base.txt").read_text(encoding="utf-8").strip()
MCP_PROMPT = (PACKAGE_ROOT / "prompts" / "mcp-integration.txt").read_text(
    encoding="utf-8"
).strip()
AUTHORITATIVE_ORACLE = (
    "The following confirmed relationships and validation results are authoritative; "
    "base join conditions on them."
)
SEED = 20260725
MODEL_ID = "openai-api/deepseek/deepseek-v4-flash"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
TOOL_DESCRIPTIONS = {
    "get_data_model": "Return confirmed entities, grains, and relationships from the JoinLint model.",
    "find_join_path": "Find confirmed relationship paths between two named entities.",
    "validate_join": "Return cached validation evidence and findings for a returned path or edge IDs.",
}
SCORER_NAMES = {
    "join_outcome_scorer",
    "mcp_trace_scorer",
    "execution_scorer",
}


class DescribedMCPServer(MCPServer):
    def __init__(self, server: MCPServer) -> None:
        self._server = server

    async def __aenter__(self) -> DescribedMCPServer:
        await self._server.__aenter__()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> bool | None:
        return await self._server.__aexit__(exc_type, exc, traceback)  # type: ignore[arg-type]

    async def tools(self) -> list[Tool]:
        described: list[Tool] = []
        for candidate in await self._server.tools():
            registry = getattr(candidate, "__registry_info__", None)
            name = registry.name.split("/")[-1] if registry is not None else ""
            description = TOOL_DESCRIPTIONS.get(name)
            if description is None:
                raise ValueError(f"unexpected JoinLint MCP tool: {name}")
            tool_description = getattr(candidate, "__TOOL_DESCRIPTION__", None)
            parameters = getattr(tool_description, "parameters", None)
            if not isinstance(parameters, ToolParams):
                raise ValueError(f"JoinLint MCP tool has no parameter schema: {name}")
            described.append(
                _wrap_described_tool(candidate, name, description, parameters)
            )
        return described


def _wrap_described_tool(
    candidate: Tool,
    name: str,
    description: str,
    parameters: ToolParams,
) -> Tool:
    async def execute(**kwargs: Any) -> Any:
        return await candidate(**kwargs)

    return ToolDef(
        execute,
        name=name,
        description=description,
        parameters=parameters,
    ).as_tool()


@tool(name="submit_sql")
def submit_sql() -> Tool:
    async def execute(sql: str, warning: str) -> str:
        """Submit one SQLite SELECT and any safety warning.

        Args:
            sql: One read-only SQLite SELECT, or an empty string when no safe query exists.
            warning: Empty when safe; otherwise the reason no safe query is available.
        """
        return json.dumps(
            {"sql": sql, "warning": warning},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    return execute


def sanitized_mcp_environment() -> dict[str, str]:
    allowed = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR")
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment["PYTHONPATH"] = str(REPOSITORY_ROOT / "src")
    key = os.environ.get("DEEPSEEK_API_KEY")
    if key and any(key in value for value in environment.values()):
        raise ValueError("an allowlisted MCP environment value contains the provider key")
    return environment


@solver
def condition_solver() -> Solver:
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sample = PilotSample.model_validate_json(
            json.dumps(state.metadata, ensure_ascii=False)
        )
        tools: list[ToolSource] = []
        prompt = BASE_PROMPT
        if sample.arm in {"C", "D"}:
            if sample.suite != "zero_config":
                prompt += "\n\n" + MCP_PROMPT
            tools.append(
                DescribedMCPServer(
                    mcp_server_stdio(
                        name="JoinLint",
                        command=sys.executable,
                        args=[
                            "-m",
                            "joinlint",
                            "serve-mcp-legacy",
                            "--project",
                            sample.mcp_project or "",
                        ],
                        cwd=REPOSITORY_ROOT,
                        env=sanitized_mcp_environment(),
                    )
                )
            )
        agent = react(
            prompt=prompt,
            tools=tools,
            attempts=1,
            submit=AgentSubmit(
                tool=submit_sql(),
                name="submit_sql",
                keep_in_messages=True,
            ),
        )
        return await as_solver(agent)(state, generate)

    return solve


def build_samples(inputs_root: Path) -> list[Sample]:
    tasks = _load_selected_tasks(inputs_root / "sealed" / "spider-pilot.json")
    if len(tasks) != 16 or len({task.db_id for task in tasks}) != 4:
        raise ValueError("sealed pilot must contain 16 tasks from four databases")

    by_database: dict[str, list[SelectedTask]] = defaultdict(list)
    for task in tasks:
        by_database[task.db_id].append(task)

    groups: list[tuple[str, list[Sample]]] = []
    for db_id in sorted(by_database, key=_utf8_key):
        oracle_project = inputs_root / "projects" / "oracle" / db_id
        joinlint_project = inputs_root / "projects" / "joinlint" / db_id
        oracle_relationships, oracle_validations = _oracle_payload(oracle_project)
        units = [
            (task, repetition)
            for task in by_database[db_id]
            for repetition in range(3)
        ]
        units.sort(key=lambda item: seeded_rank(SEED, db_id, item[0].task_id, str(item[1])))
        base_order = sorted(
            ["A", "B", "C", "D"],
            key=lambda arm: seeded_rank(SEED, db_id, "arm", arm),
        )
        for unit_index, (task, repetition) in enumerate(units):
            rotation = unit_index % 4
            arm_order = base_order[rotation:] + base_order[:rotation]
            samples = [
                _primary_sample(
                    task,
                    arm=arm,
                    repetition=repetition,
                    oracle_project=oracle_project,
                    joinlint_project=joinlint_project,
                    oracle_relationships=oracle_relationships,
                    oracle_validations=oracle_validations,
                )
                for arm in arm_order
            ]
            group_id = f"primary:{db_id}:{task.task_id}:{repetition}"
            groups.append((group_id, samples))

        diagnostic_task = min(
            by_database[db_id],
            key=lambda task: seeded_rank(SEED, db_id, task.task_id),
        )
        diagnostic = _sample(
            diagnostic_task,
            suite="zero_config",
            sample_id=f"zero_config:{diagnostic_task.task_id}",
            arm="D",
            repetition=0,
            mcp_project=joinlint_project,
            oracle_relationships=oracle_relationships,
            oracle_validations=oracle_validations,
        )
        groups.append((f"zero_config:{db_id}", [diagnostic]))

    safety_manifest = _load_safety_manifest()
    template = min(tasks, key=lambda task: seeded_rank(SEED, task.task_id))
    template_oracle, template_validations = _oracle_payload(
        inputs_root / "projects" / "oracle" / template.db_id
    )
    for case in safety_manifest:
        case_id = str(case["id"])
        safety = _sample(
            template,
            suite="safety",
            sample_id=f"safety:{case_id}",
            arm="D",
            repetition=0,
            mcp_project=inputs_root / "projects" / "safety" / case_id,
            oracle_relationships=template_oracle,
            oracle_validations=template_validations,
            question=f"Exercise the JoinLint safety case {case_id} and submit a safe query.",
        )
        groups.append((f"safety:{case_id}", [safety]))

    groups.sort(key=lambda group: seeded_rank(SEED, "group", group[0]))
    samples = [sample for _, group_samples in groups for sample in group_samples]
    if len(samples) != 200 or len({sample.id for sample in samples}) != 200:
        raise ValueError("sample construction must produce exactly 200 unique IDs")
    return samples


def _primary_sample(
    task: SelectedTask,
    *,
    arm: str,
    repetition: int,
    oracle_project: Path,
    joinlint_project: Path,
    oracle_relationships: list[dict[str, object]],
    oracle_validations: list[dict[str, object]],
) -> Sample:
    typed_arm: Arm = arm  # type: ignore[assignment]
    project = oracle_project if arm == "C" else joinlint_project if arm == "D" else None
    return _sample(
        task,
        suite="primary",
        sample_id=f"primary:{task.task_id}:{arm}:{repetition}",
        arm=typed_arm,
        repetition=repetition,
        mcp_project=project,
        oracle_relationships=oracle_relationships,
        oracle_validations=oracle_validations,
    )


def _sample(
    task: SelectedTask,
    *,
    suite: str,
    sample_id: str,
    arm: Arm,
    repetition: int,
    mcp_project: Path | None,
    oracle_relationships: list[dict[str, object]],
    oracle_validations: list[dict[str, object]],
    question: str | None = None,
) -> Sample:
    visible_question = question or task.question
    body = f"Question:\n{visible_question}\n\nSchema:\n{task.schema_text}"
    if arm == "B":
        payload = canonical_json(
            {
                "relationships": oracle_relationships,
                "validations": oracle_validations,
            }
        ).decode("utf-8")
        body += f"\n\n{AUTHORITATIVE_ORACLE}\n{payload}"
    metadata = PilotSample(
        suite=suite,  # type: ignore[arg-type]
        sample_id=sample_id,
        task_id=task.task_id,
        db_id=task.db_id,
        arm=arm,
        repetition=repetition,
        question=visible_question,
        schema_text=task.schema_text,
        schema=task.schema_map,
        gold_sql=task.gold_sql,
        allowed_graphs=task.allowed_graphs,
        oracle_relationships=oracle_relationships,
        oracle_validations=oracle_validations,
        database_path=task.database_path,
        mcp_project=str(mcp_project.resolve()) if mcp_project is not None else None,
    )
    return Sample(
        id=sample_id,
        input=body,
        target=task.gold_sql,
        metadata=metadata.model_dump(mode="json", by_alias=True),
    )


def spider_pilot_task(inputs_root: Path, samples: Sequence[Sample] | None = None) -> Task:
    return Task(
        dataset=list(samples) if samples is not None else build_samples(inputs_root),
        solver=condition_solver(),
        scorer=[join_outcome_scorer(), mcp_trace_scorer(), execution_scorer()],
        name="joinlint_spider_pilot",
    )


def run_eval(
    inputs_root: Path,
    log_dir: Path,
    *,
    model: str | Model = MODEL_ID,
    samples: Sequence[Sample] | None = None,
) -> list[Any]:
    if isinstance(model, str) and model != "mockllm/model":
        if model != MODEL_ID:
            raise ValueError("the paid pilot permits only the frozen DeepSeek model")
        if os.environ.get("DEEPSEEK_BASE_URL") != DEEPSEEK_BASE_URL:
            raise ValueError("DEEPSEEK_BASE_URL does not match the frozen endpoint")
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise ValueError("DEEPSEEK_API_KEY is required")
        resolved_model = get_model(model, strict_tools=False, memoize=False)
        resolved_model.api.should_retry = _deepseek_should_retry  # type: ignore[method-assign]
    else:
        resolved_model = model
    log_dir.mkdir(parents=True, exist_ok=True)
    prior_trace_file = os.environ.get("INSPECT_TRACE_FILE")
    os.environ["INSPECT_TRACE_FILE"] = str(log_dir / "trace.log")
    try:
        return eval(
            spider_pilot_task(inputs_root, samples),
            model=resolved_model,
            log_dir=str(log_dir),
            log_format="eval",
            log_realtime=False,
            display="none",
            fail_on_error=False,
            retry_on_error=0,
            score_on_error=True,
            max_samples=4,
            max_retries=2,
            timeout=120,
            time_limit=120,
            turn_limit=5,
            max_tokens=4096,
            temperature=0,
            parallel_tool_calls=False,
            cache=False,
            extra_body={"thinking": {"type": "disabled"}},
        )
    finally:
        if prior_trace_file is None:
            os.environ.pop("INSPECT_TRACE_FILE", None)
        else:
            os.environ["INSPECT_TRACE_FILE"] = prior_trace_file


def run_dry_eval(
    inputs_root: Path,
    log_dir: Path,
    *,
    samples: Sequence[Sample] | None = None,
) -> list[Any]:
    selected = list(samples) if samples is not None else build_samples(inputs_root)

    def outputs(
        messages: list[Any],
        tools: list[Any],
        tool_choice: object,
        config: object,
    ) -> ModelOutput:
        del tool_choice, config
        names = {candidate.name for candidate in tools}
        tool_messages = [
            message for message in messages if isinstance(message, ChatMessageTool)
        ]
        called = {message.function or "" for message in tool_messages}
        visible_text = "\n".join(str(message.content) for message in messages)
        if not any(name.endswith("get_data_model") for name in called) and any(
            name.endswith("get_data_model") for name in names
        ):
            function = next(name for name in names if name.endswith("get_data_model"))
            arguments: dict[str, object] = {}
        elif "compound_fanout" in visible_text and not any(
            name.endswith("find_join_path") for name in called
        ):
            function = next(name for name in names if name.endswith("find_join_path"))
            arguments = {
                "source_entity": "children",
                "target_entity": "grands",
                "max_depth": 4,
            }
        elif any(name.endswith("get_data_model") for name in called) and not any(
            name.endswith("validate_join") for name in called
        ):
            function = next(name for name in names if name.endswith("validate_join"))
            arguments = _dry_validation_arguments(tool_messages, visible_text)
        else:
            function = next(name for name in names if name.endswith("submit_sql"))
            arguments = {"sql": "SELECT 1", "warning": ""}
        message = ChatMessageAssistant(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"dry-run-{len(messages)}",
                    function=function,
                    arguments=arguments,
                )
            ],
        )
        return ModelOutput.from_message(message, stop_reason="tool_calls")

    logs = run_eval(
        inputs_root,
        log_dir,
        model=get_model(
            "mockllm/model",
            custom_outputs=outputs,
            memoize=False,
        ),
        samples=selected,
    )
    logged_samples = [sample for log in logs for sample in log.samples or []]
    if len(logged_samples) != len(selected) or {
        str(sample.id) for sample in logged_samples
    } != {str(sample.id) for sample in selected}:
        raise RuntimeError("dry run did not produce every planned sample exactly once")
    if any(
        sample.error is not None or set(sample.scores or {}) != set(SCORER_NAMES)
        for sample in logged_samples
    ):
        raise RuntimeError("dry run produced an error or missing scorer artifact")
    return logs


def _dry_validation_arguments(
    messages: Sequence[ChatMessageTool],
    visible_text: str,
) -> dict[str, object]:
    if "compound_fanout" in visible_text:
        payload = _last_tool_payload(messages, "find_join_path")
        data = payload.get("data")
        paths = data.get("paths", []) if isinstance(data, dict) else []
        if not isinstance(paths, list) or not paths or not isinstance(paths[0], list):
            return {"edge_ids": ["missing"]}
        return {"path": paths[0]}
    payload = _last_tool_payload(messages, "get_data_model")
    data = payload.get("data")
    relationships = data.get("relationships", []) if isinstance(data, dict) else []
    edge_ids = [
        str(relationship["id"])
        for relationship in relationships
        if isinstance(relationship, dict) and isinstance(relationship.get("id"), str)
    ]
    return {"edge_ids": edge_ids[:1] or ["missing"]}


def _last_tool_payload(
    messages: Sequence[ChatMessageTool],
    suffix: str,
) -> dict[str, Any]:
    for message in reversed(messages):
        if (message.function or "").endswith(suffix) and isinstance(message.content, str):
            try:
                payload = json.loads(message.content)
            except json.JSONDecodeError:
                return {}
            return payload if isinstance(payload, dict) else {}
    return {}


def _deepseek_should_retry(error: BaseException) -> bool:
    if isinstance(error, APITimeoutError):
        return False
    if isinstance(error, RateLimitError):
        return True
    if isinstance(error, APIStatusError):
        return error.status_code in {429, 500, 502, 503, 504}
    return isinstance(error, APIConnectionError)


def _load_selected_tasks(path: Path) -> list[SelectedTask]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, list):
        raise ValueError("sealed pilot must be an array")
    return [
        SelectedTask.model_validate_json(json.dumps(item, ensure_ascii=False))
        for item in document
    ]


def _oracle_payload(project: Path) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    envelope = get_data_model(project)
    relationships = envelope.data["relationships"]  # type: ignore[index]
    if not isinstance(relationships, list):
        raise ValueError("oracle model response has invalid relationships")
    model = load_model(project)
    if model.relationships:
        validation = validate_cached_edges(
            project,
            [relationship.id for relationship in model.relationships],
        )
        validations = validation.data["relationships"]  # type: ignore[index]
    else:
        validations = []
    if not isinstance(validations, list):
        raise ValueError("oracle validation response is invalid")
    return relationships, validations


def _load_safety_manifest() -> list[dict[str, object]]:
    path = PACKAGE_ROOT / "fixtures" / "mcp-safety" / "manifest.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    cases = document.get("cases") if isinstance(document, dict) else None
    if not isinstance(cases, list) or len(cases) != 4 or not all(
        isinstance(case, dict) for case in cases
    ):
        raise ValueError("safety manifest must contain exactly four cases")
    return cases


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the frozen JoinLint Inspect evaluation")
    parser.add_argument("command", choices=("run", "dry-run"))
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.command == "dry-run":
        run_dry_eval(args.work_dir, args.log_dir)
    else:
        run_eval(args.work_dir, args.log_dir)


if __name__ == "__main__":
    main()
