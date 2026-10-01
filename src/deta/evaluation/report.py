import json
from collections import Counter
from collections.abc import Sequence
from statistics import median
from typing import Any

from deta.evaluation.runner import Trial


def index_results(
    trials: Sequence[Trial], rows: Sequence[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """验证计划与结果身份，建立供汇总和配对共同使用的索引。"""
    planned = {trial.id: trial for trial in trials}
    identities = {(trial.task_id, trial.variant_id, trial.repeat) for trial in trials}
    if len(planned) != len(trials) or len(identities) != len(trials):
        raise ValueError("计划包含重复 Trial 或配对位置")
    actual: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["id"] not in planned or row["id"] in actual:
            raise ValueError("结果包含未计划或重复试验")
        trial = planned[row["id"]]
        if (row.get("task_id"), row.get("variant_id"), row.get("repeat")) != (
            trial.task_id,
            trial.variant_id,
            trial.repeat,
        ):
            raise ValueError("结果的任务、版本或重复编号与计划不一致")
        actual[row["id"]] = row
    return actual


def summarize(
    trials: Sequence[Trial], rows: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """验证结果并按预先计划汇总；缺行、未开始和异常都保留在分母中。"""
    return summarize_indexed(trials, index_results(trials, rows))


def summarize_indexed(
    trials: Sequence[Trial], actual: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """汇总已验证索引，不重复构建或校验结果表。"""
    result: list[dict[str, Any]] = []
    for variant in sorted({trial.variant_id for trial in trials}):
        selected = [trial for trial in trials if trial.variant_id == variant]
        values = [actual.get(trial.id, {"status": "missing"}) for trial in selected]
        counts = Counter(value["status"] for value in values)
        elapsed = [
            value["run_elapsed_seconds"]
            for value in values
            if "run_elapsed_seconds" in value
        ]
        complete_usage = [
            value["usage"]["known_tokens"]
            for value in values
            if value.get("usage", {}).get("request_attempts", 0) > 0
            and value["usage"].get("unknown_usage_attempts") == 0
        ]
        result.append(
            {
                "variant": variant,
                "planned": len(selected),
                "states": dict(counts),
                "passed": counts["passed"],
                "pass_rate_over_planned": counts["passed"] / len(selected),
                "run_seconds_median": median(elapsed) if elapsed else None,
                "elapsed_samples": len(elapsed),
                "tokens_known_subset": sum(complete_usage) if complete_usage else None,
                "by_repeat": {
                    index: {
                        "planned": sum(trial.repeat == index for trial in selected),
                        "passed": sum(
                            trial.repeat == index
                            and actual.get(trial.id, {}).get("status") == "passed"
                            for trial in selected
                        ),
                    }
                    for index in sorted({trial.repeat for trial in selected})
                },
                "trials_with_complete_usage": len(complete_usage),
                "trials_without_complete_usage": len(selected) - len(complete_usage),
            }
        )
    return result


def compare(
    trials: Sequence[Trial],
    rows: Sequence[dict[str, Any]],
    control: str,
    candidate: str,
) -> dict[str, Any]:
    """按任务和重复编号配对；有未完成配对时保留证据并暂不发布整体提升。"""
    return compare_indexed(trials, index_results(trials, rows), control, candidate)


def compare_indexed(
    trials: Sequence[Trial],
    actual: dict[str, dict[str, Any]],
    control: str,
    candidate: str,
) -> dict[str, Any]:
    """在已验证的结果索引上计算版本配对。"""
    variants = {trial.variant_id for trial in trials}
    if control == candidate or control not in variants or candidate not in variants:
        raise ValueError("对照必须选择计划中两个不同的版本")
    arms: dict[tuple[str, int], dict[str, str]] = {}
    for trial in trials:
        if trial.variant_id in {control, candidate}:
            key = (trial.task_id, trial.repeat)
            if trial.variant_id in arms.setdefault(key, {}):
                raise ValueError("同一个配对位置重复")
            arms[key][trial.variant_id] = actual.get(trial.id, {}).get(
                "status", "missing"
            )
    improved = regressed = blocked = 0
    for pair in arms.values():
        a, b = pair.get(control), pair.get(candidate)
        if a not in {"passed", "failed"} or b not in {"passed", "failed"}:
            blocked += 1
        else:
            improved += a == "failed" and b == "passed"
            regressed += a == "passed" and b == "failed"
    return {
        "pairs": len(arms),
        "blocked_pairs": blocked,
        "improved_pairs": improved,
        "regressed_pairs": regressed,
        "headline_delta": (improved - regressed) / len(arms)
        if arms and not blocked
        else None,
    }


def render_report(trials: Sequence[Trial], rows: Sequence[dict[str, Any]]) -> str:
    """生成可保存的 Markdown；详细证据仍在 protocol 和 observations 中。"""
    actual = index_results(trials, rows)
    lines = [
        "# Deta 评测报告",
        "",
        "数值来自本批计划与结果。planned/running/missing 不等于模型已经失败。",
        "",
    ]
    for value in summarize_indexed(trials, actual):
        lines.extend(
            [
                f"## 版本 {value['variant']}",
                "",
                "```json",
                json.dumps(value, ensure_ascii=False, indent=2),
                "```",
                "",
            ]
        )
    variants = sorted({trial.variant_id for trial in trials})
    if len(variants) == 2:
        lines.extend(
            [
                f"## 版本配对：{variants[0]} → {variants[1]}",
                "",
                "```json",
                json.dumps(
                    compare_indexed(trials, actual, *variants),
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
                "",
            ]
        )
    lines.extend(
        [
            "## 解释范围",
            "",
            "耗时只汇总有记录的样本；token 总量仅覆盖 usage 完整的试验。货币成本未计算。",
            "单次重复不能说明稳定性。结合 by_repeat 和逐任务记录解释波动；小样本不外推总体能力。",
            "初始项目和评分器需预先校准。具体限制及真实失败案例另见关联验收记录。",
            "",
        ]
    )
    return "\n".join(lines)
