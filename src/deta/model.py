# ruff: noqa: F401  # 为练习体预留的导入。
import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from openai import AsyncOpenAI, omit
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from opentelemetry.trace import Status, StatusCode, Tracer
from pydantic import Field, JsonValue, SecretStr, TypeAdapter

from deta.events import Listener, ModelDone, TextDelta, ToolCallDelta, emit
from deta.types import (
    AgentMessage,
    AssistantMessage,
    Data,
    ToolCall,
    ToolResult,
    Usage,
    UserMessage,
)
from src.deta.observability.artifacts import Artifacts


class ModelConfig(Data):
    """保存单次模型请求所需的模型标识、凭据、时间和输出额度。
    入口读取环境后创建该对象，再显式传给模型接入函数。
    """

    # 本次请求使用的模型标识，由入口从 OPENAI_MODEL 读取后传入。
    model: str = Field(min_length=1)
    # SDK 鉴权使用的凭据；SecretStr 在普通显示时隐藏原文，不负责加密存储。
    api_key: SecretStr
    # 单次流式请求的总超时秒数，覆盖发送和持续读取响应的过程。
    timeout_seconds: float = Field(default=60, gt=0)
    # 发送给提供方的输出 token 上限，用于约束本次响应的输出额度。
    max_completion_tokens: int = Field(default=2048, ge=1)


class ModelProtocolError(Exception):
    """表示提供方响应缺少必要终态或工具调用结构无法可靠处理。
    模型接入层抛出它，由调用方处理失败；本类没有额外属性，异常说明由父类保存。
    """


@dataclass
class PartialCall:
    """在一次流式响应内部暂存同一工具调用的累计字段。
    model.py 按 index 找到该对象并追加片段，流结束后再转换成 ToolCall。
    """

    # 累计收到的调用编号片段，最终作为 ToolCall.id 使用。
    id: str = ""
    # 累计收到的函数名片段，最终用于查找 TOOLS 中的工具。
    name: str = ""
    # 累计收到的参数字符串，尚未执行 JSON 解析或参数校验。
    arguments: str = ""


def to_provider_messages(
        instructions: str,
        messages: Sequence[AgentMessage],
) -> list[ChatCompletionMessageParam]:
    """接收系统指令与 Deta 消息序列，转换成 SDK 接受的消息字典列表。
    由 stream_once 调用，返回值同时用于请求快照和 SDK 请求，输入消息保持原样。

    TODO：保留角色、文本、拒绝内容、工具调用与调用 ID；返回 SDK 消息列表，不修改输入。
    """
    raise NotImplementedError("请完成 to_provider_messages")


async def stream_once(
        client: AsyncOpenAI,
        config: ModelConfig,
        instructions: str,
        messages: Sequence[AgentMessage],
        tools: Sequence[ChatCompletionToolParam],
        *,
        tracer: Tracer,
        artifacts: Artifacts,
        listeners: Sequence[Listener] = (),
) -> AssistantMessage:
    """接收 SDK 客户端、请求配置、指令、历史消息、工具声明及观测依赖，完成一次流式请求。
    函数向监听器发送增量，将完整 AssistantMessage 返回给 CLI 或后续 Loop；错误与取消向外传播。

    TODO：校验关闭 SDK 重试；快照实际参数；创建 request/attempt Span；拼接文本与按 index 分组的工具调用；读完 usage；校验最终响应并返回；异常和取消结束 Span 后传播。
    """
    raise NotImplementedError("请完成 stream_once")