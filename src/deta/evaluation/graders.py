import fnmatch
import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from deta.builtin_tools import ToolContext
from deta.builtin_tools.bash import BashArgs, run_bash
from deta.types import Data


class FileRule(Data):
    """可信评分配置，只描述真实任务的最终文件约束。"""

    path: str
    contains: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    sha256: str | None = None


class Acceptance(Data):
    allowed_changes: tuple[str, ...]
    files: tuple[FileRule, ...]
    # 人工确认的已有命令，原样交给 shell；不从模型回答中提取命令。
    checks: tuple[str, ...] = ()
    # 仅供检查命令生成缓存或构建输出；不能用它豁免修改待评分源码。
    check_outputs: tuple[str, ...] = ()


class Grade(Data):
    passed: bool
    reasons: tuple[str, ...]
    evidence: tuple[str, ...]
    # False 表示检查过程改写了不应改写的产物，不能给任务通过/失败结论。
    valid: bool = True
    # 评分命令开始前固定的 Agent 产物；无法完整读取时为 None。
    candidate_files: dict[str, str] | None = None


def snapshot(root: Path) -> dict[str, str]:
    """完整读取普通文件树；拒绝链接，不把扫描权限或 I/O 失败当成文件不存在。"""
    result: dict[str, str] = {}

    def scan_error(error: OSError) -> None:
        raise error

    for directory, folders, files in os.walk(
        root, followlinks=False, onerror=scan_error
    ):
        for name in sorted((*folders, *files)):
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError(f"工作区包含符号链接：{path.relative_to(root)}")
            if stat.S_ISREG(mode):
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                result[path.relative_to(root).as_posix()] = digest.hexdigest()
            elif not stat.S_ISDIR(mode):
                raise ValueError("工作区仅支持普通文件与目录")
    return result


async def grade(
    workspace: Path,
    before: dict[str, str],
    acceptance: Acceptance,
    output_dir: Path,
    environment: Mapping[str, str],
) -> Grade:
    """先固定 Agent 产物，再执行检查；检查自身不能替 Agent 修正答案。"""
    reasons: list[str] = []
    evidence: list[str] = []
    try:
        candidate = snapshot(workspace)
    except ValueError as exc:
        return Grade(passed=False, reasons=(str(exc),), evidence=())
    changed = sorted(
        path
        for path in before.keys() | candidate.keys()
        if before.get(path) != candidate.get(path)
    )
    forbidden = [
        path
        for path in changed
        if not any(
            fnmatch.fnmatchcase(path, allowed) for allowed in acceptance.allowed_changes
        )
    ]
    if forbidden:
        reasons.append("修改越界：" + ", ".join(forbidden))
    for rule in acceptance.files:
        path = workspace / rule.path
        if Path(rule.path).is_absolute() or not path.resolve().is_relative_to(
            workspace.resolve()
        ):
            raise ValueError("评分路径必须位于试验工作区内")
        if rule.path not in candidate:
            reasons.append(f"缺少验收文件：{rule.path}")
            continue
        if rule.sha256 is not None and candidate[rule.path] != rule.sha256:
            reasons.append(f"文件指纹不匹配：{rule.path}")
        if rule.contains or rule.excludes:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                reasons.append(f"要求 UTF-8 的文件编码无效：{rule.path}")
                continue
            if any(value not in text for value in rule.contains) or any(
                value in text for value in rule.excludes
            ):
                reasons.append(f"文件内容约束未满足：{rule.path}")
    for command in acceptance.checks:
        output = await run_bash(
            BashArgs(command=command),
            ToolContext(
                workspace, output_dir, "/bin/zsh", environment, lambda text: None
            ),
        )
        evidence.append(output.content)
        try:
            after_check = snapshot(workspace)
        except ValueError as exc:
            return Grade(
                passed=False,
                valid=False,
                reasons=(*reasons, str(exc)),
                evidence=tuple(evidence),
                candidate_files=candidate,
            )
        checker_changes = sorted(
            path
            for path in candidate.keys() | after_check.keys()
            if candidate.get(path) != after_check.get(path)
            and not any(
                fnmatch.fnmatchcase(path, pattern)
                for pattern in acceptance.check_outputs
            )
        )
        if checker_changes:
            return Grade(
                passed=False,
                valid=False,
                reasons=(
                    *reasons,
                    "检查命令改写了待评产物：" + ", ".join(checker_changes),
                ),
                evidence=tuple(evidence),
                candidate_files=candidate,
            )
        if output.error_code:
            reasons.append(f"已有检查失败：{command} ({output.error_code})")
    return Grade(
        passed=not reasons,
        reasons=tuple(reasons),
        evidence=tuple(evidence),
        candidate_files=candidate,
    )
