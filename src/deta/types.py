from dataclasses import dataclass
from typing import Annotated, Any, Literal

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, UsageMetadata
from pydantic import BaseModel, ConfigDict, Field

# Any 仅用于 LangChain 的工具声明边界；实际工具参数仍由 Pydantic 校验。
type ToolSchema = dict[str, Any]


class Data(BaseModel):
    """Deta 业务数据的公共基类（消息直接使用 LangChain 类型），集中设置字段校验与冻结规则。
    子类负责声明具体业务字段，创建对象时由 Pydantic 校验这些字段。
    """

    # Pydantic 的类级配置：拒绝未声明字段，并禁止创建后重新赋值对象字段。
    model_config = ConfigDict(extra="forbid", frozen=True)


# 消息联合类型：用户输入、助手响应或工具结果，用于历史与请求边界的类型标注。
type AgentMessage = Annotated[
    HumanMessage | AIMessage | ToolMessage, Field(discriminator="type")
]


class RunOptions(Data):
    """保存一次 Agent 运行的资源限制，由入口组装后交给后续 Loop 使用。
    本类只定义限制数据，实际计数、超时和停止处理由运行控制代码完成。
    """

    # 一次 Run 的实际 SDK 尝试总额度，包括重试；在调用 SDK 前扣减。
    max_requests: int = Field(default=10, ge=1)
    # 提供方已报告用量的停止预算；缺失 usage 时不能证明额度合规。
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_repeated_failures: int = Field(default=3, ge=1)
    # 一次 Run 允许的工具调用总数；设为零表示不给工具执行额度。
    max_tool_calls: int = Field(default=20, ge=0)
    # 整次 Run 的总时间额度，单位为秒，与单次模型请求超时分别管理。
    timeout_seconds: float = Field(default=120, gt=0)
    # 单个逻辑请求最多额外重试几次，同时受 max_requests 总尝试额度约束。
    max_retries: int = Field(default=2, ge=0, le=5)
    # 指数退避的初始等待秒数，等待也计入总运行时限。
    retry_delay_seconds: float = Field(default=0.25, ge=0)


class RunResult(Data):
    """汇总一次 Agent 运行结束后的状态、原因、回答和消息。
    由后续运行层创建并返回给 Python 调用方或命令入口。
    """

    # Deta 为本次任务运行生成的业务编号，用于关联事件和诊断产物。
    run_id: str
    # 运行终态：完成、取消、达到限制或失败；与模型 stop_reason 分开。
    status: Literal["completed", "cancelled", "limited", "failed"]
    # 说明为什么以当前状态结束，正常完成时可以为空。
    reason: str = ""
    # 返回给用户的最终回答文本；未得到回答时可以为空。
    answer: str = ""
    # 随运行结果返回的消息元组，供调用方查看本次产生或使用的对话内容。
    messages: tuple[AgentMessage, ...] = ()


class RebuildRequest(Exception):
    """结构变化后回到本轮请求准备；不是新 Turn，也不是网络重试。"""


class RunLimitError(Exception):
    """表示 Run 额度耗尽，保留停止原因交给 Agent 构造 limited 结果。"""


@dataclass
class RunBudget:
    """保存当前 Run 的可变计数，由 Loop 创建并交给请求与工具边界共同使用。"""

    # 当前 Run 的不可变额度配置。
    options: RunOptions
    # 已进入 SDK 调用边界的尝试次数，重试也计数。
    request_attempts: int = 0
    # 已放行进入调度的工具调用数量，参数被拒绝也占用调度额度。
    tool_calls: int = 0
    # 每个逻辑助手请求最多一次提供方溢出恢复；Loop 在新请求开始时重置。
    overflow_recovery_used: bool = False
    known_tokens: int = 0
    unknown_usage_attempts: int = 0

    @property
    def token_stop_reason(self) -> str:
        limit = self.options.max_total_tokens
        if limit is None:
            return ""
        if self.unknown_usage_attempts:
            return "token_usage_unknown"
        return "token_budget" if self.known_tokens > limit else ""

    def observe_usage(self, usage: UsageMetadata | None) -> None:
        total = usage["total_tokens"] if usage is not None else None
        if total is None:
            self.unknown_usage_attempts += 1
        else:
            self.known_tokens += total

    def take_request(self) -> int:
        """在真正开始 SDK 尝试前检查额度并计数，返回该 Run 内的尝试编号。"""
        if self.token_stop_reason:
            raise RunLimitError(self.token_stop_reason)
        if (
            self.options.max_total_tokens is not None
            and self.known_tokens >= self.options.max_total_tokens
        ):
            raise RunLimitError("token_budget")
        if self.request_attempts >= self.options.max_requests:
            raise RunLimitError("实际模型请求尝试额度耗尽")
        self.request_attempts += 1
        return self.request_attempts

    def reserve_tools(self, count: int) -> None:
        """在开始本批工具调度前一次性预占全部额度，避免执行半批后才发现不够。"""
        if self.tool_calls + count > self.options.max_tool_calls:
            raise RunLimitError("工具调度额度耗尽")
        self.tool_calls += count
