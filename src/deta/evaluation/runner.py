import asyncio
import json
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from langchain_openai import ChatOpenAI
from pydantic import Field, JsonValue, TypeAdapter

from deta.evaluation.graders import Acceptance, Grade, grade, snapshot
from deta.model import ModelConfig, open_model
from deta.observability.artifacts import Artifacts, source_version
from deta.observability.tracing import artifact_listener, local_tracing
from deta.runtime import AgentSession
from deta.session import Session
from deta.storage import SQLiteStore
from deta.types import Data, RunOptions


class EvalTask(Data):
    id: str
    prompt: str
    initial: str
    acceptance: Acceptance
    options: RunOptions = Field(default_factory=RunOptions)
    skills: tuple[str, ...] = ()


class Variant(Data):
    """本实现首先比较同一份源码上的提示词版本，模型配置由批次统一提供。"""

    id: str
    instructions: str


class Trial(Data):
    id: str
    task_id: str
    variant_id: str
    repeat: int


def plan_trials(
    tasks: Sequence[EvalTask], variants: Sequence[Variant], repeats: int
) -> tuple[Trial, ...]:
    if repeats < 1 or not tasks or not variants:
        raise ValueError("任务、版本和重复次数不能为空")
    if len({task.id for task in tasks}) != len(tasks) or len(
        {value.id for value in variants}
    ) != len(variants):
        raise ValueError("任务或版本 ID 重复")
    return tuple(
        Trial(id=uuid4().hex, task_id=task.id, variant_id=variant.id, repeat=index)
        for index in range(1, repeats + 1)
        for task in tasks
        for variant in (variants if index % 2 else tuple(reversed(variants)))
    )


def persist(path: Path, value: object) -> None:
    """评测结果是本工作流的交付物，保存失败必须显式传播。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(
            TypeAdapter(JsonValue).validate_python(value), ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


async def run_trial(
    trial: Trial,
    task: EvalTask,
    variant: Variant,
    row: dict[str, JsonValue],
    *,
    client: ChatOpenAI,
    config: ModelConfig,
    initial_root: Path,
    initial_version: dict[str, str],
    work: Path,
    run_root: Path,
    context_window: int,
    environment: Mapping[str, str],
    capture_body: bool,
    redact: Callable[[str], str] | None,
) -> None:
    """复制、运行和评分一条试验，逐步填充 row；外层负责保存及取消收尾记录。"""
    initial = (initial_root / task.initial).resolve(strict=True)
    if snapshot(initial) != initial_version:
        raise ValueError("初始项目已变化，不能沿用本批协议")
    shutil.copytree(initial, work, symlinks=True)
    # 复制前后都核对；拒绝复制中出现的链接或内容变化。
    if snapshot(work) != initial_version:
        raise ValueError("复制结果与已固定初始项目不一致")
    artifacts = Artifacts(
        run_root / "artifacts", capture_body=capture_body, redact=redact
    )
    with (
        closing(SQLiteStore(run_root / "session.sqlite3")) as store,
        local_tracing(run_root / "spans.jsonl") as tracer,
    ):
        recorded = Session(store, work)
        runtime = AgentSession(
            client,
            config,
            work,
            tracer,
            artifacts,
            session=recorded,
            context_window=context_window,
            instructions=variant.instructions,
            options=task.options,
            environment=environment,
            listeners=[artifact_listener(artifacts)],
        )
        for name in task.skills:
            runtime.use_skill(name)
        run_started = time.monotonic()
        try:
            result = await runtime.prompt(task.prompt, run_id=trial.id)
        finally:
            row["run_elapsed_seconds"] = time.monotonic() - run_started
        row.update(
            {
                "run_id": result.run_id,
                "session_id": recorded.id,
                "run_status": result.status,
                "reason": result.reason,
                "usage": dict(runtime.usage_stats),
                "trace": str(run_root / "spans.jsonl"),
            }
        )
    try:
        result_grade: Grade = await grade(
            work,
            initial_version,
            task.acceptance,
            run_root / "grader-output",
            environment,
        )
    except Exception as exc:
        row.update({"status": "grader_error", "error": type(exc).__name__})
    else:
        row["grade"] = result_grade.model_dump(mode="json")
        row["status"] = (
            "grader_error"
            if not result_grade.valid
            else "passed"
            if result.status == "completed" and result_grade.passed
            else "failed"
        )


async def run_batch(
    tasks: Sequence[EvalTask],
    variants: Sequence[Variant],
    *,
    repeats: int,
    initial_root: Path,
    batch_root: Path,
    config: ModelConfig,
    context_window: int,
    environment: Mapping[str, str],
    capture_body: bool = False,
    redact: Callable[[str], str] | None = None,
) -> list[dict[str, JsonValue]]:
    """先固定全部计划，再逐个复制、运行和评分；不复用 Session 或修改原始项目。"""
    trials = plan_trials(tasks, variants, repeats)
    initial_root = initial_root.resolve(strict=True)
    batch_root = batch_root.resolve()
    if batch_root.is_relative_to(initial_root) or initial_root.is_relative_to(
        batch_root
    ):
        raise ValueError("批次目录必须与初始项目目录分离")
    batch_root.mkdir(parents=True, exist_ok=False)
    task_map = {task.id: task for task in tasks}
    variant_map = {variant.id: variant for variant in variants}
    initial_versions: dict[str, dict[str, str]] = {}
    for task in tasks:
        initial = (initial_root / task.initial).resolve(strict=True)
        if not initial.is_relative_to(initial_root) or not initial.is_dir():
            raise ValueError("初始项目路径越界或不是目录")
        if not task.acceptance.files and not task.acceptance.checks:
            raise ValueError("每个任务至少有一项产物或已有检查验收")
        initial_versions[task.id] = snapshot(initial)
    protocol: dict[str, JsonValue] = {
        "version": 1,
        "model": config.model,
        "max_completion_tokens": config.max_completion_tokens,
        "context_window": context_window,
        "source_sha256": source_version(Path(__file__).parents[1]),
        "tasks": [task.model_dump(mode="json") for task in tasks],
        "variants": [variant.model_dump(mode="json") for variant in variants],
        "trials": [trial.model_dump(mode="json") for trial in trials],
        "initial_versions": {
            key: dict(value) for key, value in initial_versions.items()
        },
        "environment_keys": [name for name in sorted(environment)],
    }
    persist(batch_root / "protocol.json", protocol)
    rows: list[dict[str, JsonValue]] = [
        {**trial.model_dump(mode="json"), "status": "planned"} for trial in trials
    ]
    persist(batch_root / "observations.json", rows)
    async with open_model(config) as client:
        for position, trial in enumerate(trials):
            row = rows[position]
            task, variant = task_map[trial.task_id], variant_map[trial.variant_id]
            work = batch_root / "workspaces" / trial.id
            run_root = batch_root / "runs" / trial.id
            row["status"] = "running"
            persist(batch_root / "observations.json", rows)
            started = time.monotonic()
            try:
                await run_trial(
                    trial,
                    task,
                    variant,
                    row,
                    client=client,
                    config=config,
                    initial_root=initial_root,
                    initial_version=initial_versions[task.id],
                    work=work,
                    run_root=run_root,
                    context_window=context_window,
                    environment=environment,
                    capture_body=capture_body,
                    redact=redact,
                )
            except asyncio.CancelledError:
                row["status"] = "cancelled"
                raise
            except Exception as exc:
                row.update({"status": "runner_error", "error": type(exc).__name__})
            finally:
                row["elapsed_seconds"] = time.monotonic() - started
                persist(batch_root / "observations.json", rows)
    return rows
