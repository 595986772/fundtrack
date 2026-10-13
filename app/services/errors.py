"""service 层专用异常。

router 层会捕获这些异常，再转换成 HTTP 响应。
这样 service 层不用直接依赖 FastAPI，也更容易复用和测试。
"""


class AgentConfigError(RuntimeError):
    """Agent 配置错误，比如没有设置 API Key 或缺少依赖包。"""


class AgentRuntimeError(RuntimeError):
    """Agent 调用外部模型时发生的运行错误。"""


class ServiceError(RuntimeError):
    """业务失败的稳定代码，由产生错误的分支指定，不靠中文关键词猜原因。

    旧错误默认 business_error；仍继承 RuntimeError，原路由捕获方式保持兼容。
    """

    code = "business_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code or type(self).code
