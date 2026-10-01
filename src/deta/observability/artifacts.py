import hashlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue

from deta.types import Data

logger = logging.getLogger(__name__)


class Artifacts:
    """管理请求、响应和工具结果的诊断正文采集。
    调用方显式传入目录、采集开关和脱敏函数，保存失败不会代替业务结果。
    """

    def __init__(
        self,
        root: Path,
        *,
        capture_body: bool = False,
        redact: Callable[[str], str] | None = None,
    ) -> None:
        """用 root、capture_body 和 redact 初始化正文采集器，并检查采集开关与脱敏函数的组合。
        这里只保存实例配置，创建对象不会写文件；构造完成后由 save 按需保存产物。
        """
        if capture_body and redact is None:
            raise ValueError("正文采集必须显式传入脱敏函数")
        # 该采集器写入诊断 JSON 文件的目录，实际保存时按需创建。
        self.root = root
        # 是否采集正文；为 False 时 save 直接返回 None，不写正文文件。
        self.capture_body = capture_body
        # 调用方提供的字符串脱敏函数；打开正文采集时必须显式提供。
        self.redact = redact
        self.saved = 0
        self.skipped = 0
        self.failed = 0
        self.redacted = 0

    def save(self, kind: str, payload: JsonValue) -> str | None:
        """接收产物类别 kind 和 JSON 数据 payload，按配置脱敏并保存为独立文件。
        请求或工具边界调用它取得文件路径；未开启采集或保存失败时返回 None。
        """
        if not self.capture_body:
            self.skipped += 1
            return None

        def clean(value: JsonValue) -> JsonValue:
            """递归处理待保存的 JSON 数据，对字符串值调用实例的 redact 函数。
            列表和字典保持原有层级，数字等值直接返回；结果交给 save 写入文件。
            """
            if isinstance(value, str):
                cleaned = self.redact(value) if self.redact is not None else value
                if cleaned != value:
                    self.redacted += 1
                return cleaned
            if isinstance(value, list):
                return [clean(item) for item in value]
            if isinstance(value, dict):
                return {key: clean(item) for key, item in value.items()}
            return value

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / f"{kind}-{uuid4().hex}.json"
            with path.open("x", encoding="utf-8") as output:
                json.dump(clean(payload), output, ensure_ascii=False, indent=2)
            self.saved += 1
            return str(path)
        except Exception as exc:
            self.failed += 1
            logger.warning("artifact unavailable: %s", type(exc).__name__)
            return None

    def metadata(self, name: str, payload: JsonValue) -> str | None:
        """保存调用方筛选后的非秘密运行清单；与正文采集开关分开，仍脱敏并隔离错误。"""
        writer = Artifacts(
            self.root.parent, capture_body=True, redact=self.redact or str
        )
        return writer.save(name, payload)


def source_version(package: Path) -> str:
    """记录实际加载包内 Python 源码的内容指纹，未提交改动同样会改变它。"""
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


class EvidenceRef(Data):
    """一条可以下钻的证据；path 可以引用请求、Span 导出或验收产物。"""

    path: str
    span_id: str | None = None
    note: str


class Badcase(Data):
    """记录现象与推断的边界；没有定位依据时允许根因为空。"""

    case_id: str
    run_id: str
    expected: str
    actual: str
    environment: dict[str, JsonValue]
    category: str
    root_cause: str | None = None
    evidence: tuple[EvidenceRef, ...]
    status: Literal["open", "diagnosed", "fixed", "verified"] = "open"
    fix_revision: str | None = None
    rerun_ids: tuple[str, ...] = ()
