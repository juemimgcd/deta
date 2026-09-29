from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Data(BaseModel):
    """所有 Deta 数据对象的公共基类，集中设置字段校验与冻结规则。
    子类负责声明具体业务字段，创建对象时由 Pydantic 校验这些字段。
    """

    # Pydantic 的类级配置：拒绝未声明字段，并禁止创建后重新赋值对象字段。
    model_config = ConfigDict(extra="forbid", frozen=True)


class Usage(Data):
    """保存一次模型响应报告的 token 用量，供终端显示、Trace 和后续预算使用。
    未获得的用量保留为 None，以便与提供方明确报告的零区分。
    """

    # 提供方报告的输入 token 数；None 表示未提供，数值必须非负。
    input_tokens: int | None = Field(default=None, ge=0)
    # 提供方报告的输出 token 数；None 表示未知，不自动补成零。
    output_tokens: int | None = Field(default=None, ge=0)
    # 提供方报告的总 token 数；未报告时保留 None，不用猜测值填充。
    total_tokens: int | None = Field(default=None, ge=0)


class ToolCall(Data):
    """保存模型提出的一次工具调用，由模型接入层在流读取完成后创建。
    执行器根据名称选择工具，并在执行前验证这里保存的原始参数。
    """

    # 模型给出的调用编号；后续 ToolResult.tool_call_id 必须使用同一个值。
    id: str = Field(min_length=1)
    # 工具名称，也是显式 TOOLS 字典中用于选择执行器的键。
    name: str = Field(min_length=1)
    # 模型返回的原始参数 JSON 字符串；此处保留原文，执行前再解析和校验。
    arguments_json: str


class UserMessage(Data):
    """表示一条用户输入，由入口或后续会话层创建。
    它进入消息历史后，会在请求边界转换成提供方的 user 消息。
    """

    # 固定的消息角色，用于将这条消息识别为用户输入。
    role: Literal["user"] = "user"
    # 用户输入的正文；字段要求至少一个字符，入口另行拒绝纯空白输入。
    content: str = Field(min_length=1)


class AssistantMessage(Data):
    """表示一次模型请求的完整响应，由 model.py 拼接流式内容后创建。
    调用者据此提交消息、判断停止原因，并决定是否进入后续工具调度。
    """

    # 固定的助手角色，在转换请求和恢复消息时标识消息来源。
    role: Literal["assistant"] = "assistant"
    # 所有正文分片拼接后的完整文本；仅调用工具时可以为空。
    content: str = ""
    # 提供方返回的拒绝内容；没有拒绝信息时为 None。
    refusal: str | None = None
    # 本次响应提出的工具调用元组；没有调用时为空，不代表这些工具已经执行。
    tool_calls: tuple[ToolCall, ...] = ()
    # 提供方结束响应的原因：自然停止、工具调用、输出额度耗尽或内容过滤。
    stop_reason: Literal["stop", "tool_calls", "length", "content_filter"]
    # 本次响应的用量对象；default_factory 为每条响应创建独立的 Usage。
    usage: Usage = Field(default_factory=Usage)
    # 提供方给出的响应编号，用于关联诊断记录；它与工具调用 ID 不同。
    provider_response_id: str | None = None


class ToolResult(Data):
    """保存一次工具处理的成功内容或可预期错误，由工具执行器构造。
    调用者通过调用 ID 将它与助手提出的 ToolCall 配对，再把内容送回模型。
    """

    # 固定的工具角色，转换请求时生成提供方的 tool 消息。
    role: Literal["tool"] = "tool"
    # 对应 ToolCall.id，保证模型能确定这份结果属于哪一次调用。
    tool_call_id: str = Field(min_length=1)
    # 对应的工具名称，用于内部记录和诊断。
    name: str = Field(min_length=1)
    # 送回模型的成功输出或错误说明，不能仅依靠内部错误码表达失败。
    content: str
    # None 表示成功；字符串标识失败类别，具体错误码由产生结果的模块定义。
    error_code: str | None = None

    @property
    def is_error(self) -> bool:
        """根据 error_code 计算当前结果是否失败，返回布尔值给调用者。
        这是只读属性，使用 result.is_error 访问；每次读取都根据当前错误码计算。
        """
        return self.error_code is not None


# 消息联合类型：用户输入、助手响应或工具结果，用于历史与请求边界的类型标注。
type AgentMessage = UserMessage | AssistantMessage | ToolResult


class RunOptions(Data):
    """保存一次 Agent 运行的资源限制，由入口组装后交给后续 Loop 使用。
    本类只定义限制数据，实际计数、超时和停止处理由运行控制代码完成。
    """

    # 一次 Run 的模型请求额度，后续由 Loop 计数和执行限制。
    max_requests: int = Field(default=10, ge=1)
    # 一次 Run 允许的工具调用总数；设为零表示不给工具执行额度。
    max_tool_calls: int = Field(default=20, ge=0)
    # 整次 Run 的总时间额度，单位为秒，与单次模型请求超时分别管理。
    timeout_seconds: float = Field(default=120, gt=0)


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