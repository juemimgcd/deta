import json
import logging
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from pydantic import JsonValue

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

    def save(self, kind: str, payload: JsonValue) -> str | None:
        """接收产物类别 kind 和 JSON 数据 payload，按配置脱敏并保存为独立文件。
        请求或工具边界调用它取得文件路径；未开启采集或保存失败时返回 None。
        """
        if not self.capture_body:
            return None

        def clean(value: JsonValue) -> JsonValue:
            """递归处理待保存的 JSON 数据，对字符串值调用实例的 redact 函数。
            列表和字典保持原有层级，数字等值直接返回；结果交给 save 写入文件。
            """
            if isinstance(value, str):
                return self.redact(value) if self.redact is not None else value
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
            return str(path)
        except Exception as exc:
            logger.warning("artifact unavailable: %s", type(exc).__name__)
            return None