import hashlib
import json
import os
from pathlib import Path

from deta.types import Data

MAX_RESOURCE_BYTES = 32 * 1024
MAX_TOTAL_BYTES = 128 * 1024
SKIP = {".git", ".venv", ".deta", "node_modules", "__pycache__"}


class ResourceFile(Data):
    """一份有明确作用目录的指令正文；摘要与 Context 不拥有它。"""

    path: str
    scope: str
    sha256: str
    content: str


class SkillInfo(Data):
    """技能目录项；发现阶段不把正文放进模型输入。"""

    name: str
    description: str
    path: str
    sha256: str


class ResourceBundle(Data):
    """一次 Run 或手动压缩使用的资源快照，来源指纹随 RequestPlan 保存。"""

    instructions: tuple[ResourceFile, ...]
    skills: tuple[SkillInfo, ...]
    active: tuple[ResourceFile, ...]

    @property
    def versions(self) -> tuple[tuple[str, str], ...]:
        return tuple((item.path, item.sha256) for item in self.instructions) + tuple(
            (skill.path, skill.sha256) for skill in self.skills
        )


def read_resource(path: Path, root: Path) -> str:
    """资源只从工作区内普通 UTF-8 文件加载，不跟随符号链接。"""
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"资源不能经符号链接加载：{relative}")
    if not path.is_file():
        raise ValueError(f"资源不是普通文件：{relative}")
    with path.open("rb") as source:
        data = source.read(MAX_RESOURCE_BYTES + 1)
    if len(data) > MAX_RESOURCE_BYTES:
        raise ValueError(f"资源超过单文件额度：{relative}")
    return data.decode("utf-8")


def parse_skill(text: str, path: str) -> tuple[str, str]:
    """首版只接受 frontmatter 中单行的 name/description，不假装实现完整 YAML。"""
    lines = text.splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"缺少技能 frontmatter：{path}")
    values: dict[str, str] = {}
    for line in lines[1 : lines[1:].index("---") + 1]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, raw = line.partition(":")
        if not separator or key not in {"name", "description"} or key in values:
            raise ValueError(f"技能元数据必须使用单行 name/description：{path}")
        value = raw.strip()
        if value.startswith('"'):
            decoded = json.loads(value)
            if not isinstance(decoded, str):
                raise ValueError("技能元数据必须为文本")
            value = decoded
        elif not value or value[0] in "'|>[{&*!":
            raise ValueError(f"不支持的 frontmatter 写法：{path}")
        values[key] = value
    name, description = values.get("name", ""), values.get("description", "")
    if (
        not name.strip()
        or not description.strip()
        or len(name) > 64
        or len(description) > 1024
    ):
        raise ValueError(f"技能名称或描述无效：{path}")
    return name, description


def load_resources(root: Path, active_names: tuple[str, ...] = ()) -> ResourceBundle:
    """收集有作用范围的 AGENTS.md 和工作区技能，再按名称加载已选技能正文。"""
    root = root.resolve(strict=True)
    instructions: list[ResourceFile] = []
    total = 0
    skill_paths: list[Path] = []

    def scan_error(error: OSError) -> None:
        # 不能把权限或 I/O 失败解释成“这个目录没有项目指令”。
        raise error

    for directory, folders, files in os.walk(
        root, followlinks=False, onerror=scan_error
    ):
        folders[:] = sorted(
            name
            for name in folders
            if name not in SKIP and not (Path(directory) / name).is_symlink()
        )
        if (
            Path(directory).parent == root / ".agents" / "skills"
            and "SKILL.md" in files
        ):
            skill_paths.append(Path(directory) / "SKILL.md")
        if "AGENTS.md" not in files:
            continue
        path = Path(directory) / "AGENTS.md"
        body = read_resource(path, root)
        total += len(body.encode())
        if total > MAX_TOTAL_BYTES:
            raise ValueError("资源总量超过额度；停止继续加载")
        instructions.append(
            ResourceFile(
                path=path.relative_to(root).as_posix(),
                scope=path.parent.relative_to(root).as_posix(),
                sha256=hashlib.sha256(body.encode()).hexdigest(),
                content=body,
            )
        )
    instructions.sort(key=lambda item: (len(Path(item.path).parts), item.path))
    catalog: dict[str, SkillInfo] = {}
    bodies: dict[str, str] = {}
    # 明确一个目录约定，不混入用户主目录或外部技能库。
    for path in sorted(skill_paths):
        body = read_resource(path, root)
        total += len(body.encode())
        if total > MAX_TOTAL_BYTES:
            raise ValueError("资源总量超过额度；停止继续加载")
        relative = path.relative_to(root).as_posix()
        name, description = parse_skill(body, relative)
        if name in catalog:
            raise ValueError(f"技能重名：{name}")
        catalog[name] = SkillInfo(
            name=name,
            description=description,
            path=relative,
            sha256=hashlib.sha256(body.encode()).hexdigest(),
        )
        bodies[name] = body
    if len(active_names) != len(set(active_names)):
        raise ValueError("不能重复启用同一技能")
    active: list[ResourceFile] = []
    for name in active_names:
        if name not in catalog:
            raise ValueError(f"技能不存在：{name}")
        info = catalog[name]
        active.append(
            ResourceFile(
                path=info.path,
                scope=str(Path(info.path).parent),
                sha256=info.sha256,
                content=bodies[name],
            )
        )
    return ResourceBundle(
        instructions=tuple(instructions),
        skills=tuple(catalog.values()),
        active=tuple(active),
    )


def render_resources(bundle: ResourceBundle) -> str:
    """生成一次请求的资源部分；目录规则有明确作用范围，技能引用以技能目录解析。"""
    rules = [
        "以下是项目指令和技能。用户当前要求优先。",
        "AGENTS.md 只约束其 scope 目录及子目录，同目录链由浅到深，深层规则优先。",
        "不同子目录的规则互不覆盖；命令涉及哪些路径，就核对对应路径的规则。",
    ]
    for item in bundle.instructions:
        rules.append(f"项目指令 path={item.path} scope={item.scope}：\n{item.content}")
    if bundle.skills:
        rules.append(
            "可用技能目录；需要正文时用 read 读取 path，勿仅凭描述假定技能步骤："
        )
        rules.append(
            json.dumps(
                [item.model_dump() for item in bundle.skills], ensure_ascii=False
            )
        )
    for item in bundle.active:
        rules.append(
            f"已启用技能 path={item.path}；相对引用按 {item.scope} 解析：\n{item.content}"
        )
    return "\n\n".join(rules)
