"""ChatModel 供应商工厂与按异常切换的备用模型链。

业务 Service 仍只调用 ``get_chat_model``。主模型、备用模型、OpenAI 兼容参数和故障切换
都集中在这里，因此 Tool Agent 与各种结构化研判不需要知道具体供应商。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable

from app.config import settings
from app.services.errors import AgentConfigError
from app.services.ai_governance_context import AIControlError


@dataclass(frozen=True)
class OpenAICompatibleProviderConfig:
    name: str
    model: str
    api_key: str
    base_url: str


class ProviderFailoverRunnable:
    """按顺序调用 Runnable；只有前一个抛出异常时才尝试下一个。"""

    def __init__(self, candidates: list[tuple[str, Any]]) -> None:
        if not candidates:
            raise AgentConfigError("没有可用的大模型供应商")
        self.candidates = candidates

    def bind_tools(self, tools: list[Any]) -> "ProviderFailoverRunnable":
        return self._map(lambda candidate: candidate.bind_tools(tools))

    def with_structured_output(
        self,
        schema: Any,
        **kwargs: Any,
    ) -> "ProviderFailoverRunnable":
        return self._map(
            lambda candidate: candidate.with_structured_output(schema, **kwargs)
        )

    async def ainvoke(self, input_value: Any, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for provider_name, candidate in self.candidates:
            try:
                response = await candidate.ainvoke(input_value, **kwargs)
                self._mark_provider(response, provider_name)
                return response
            except AIControlError:
                raise
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def invoke(self, input_value: Any, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for provider_name, candidate in self.candidates:
            try:
                response = candidate.invoke(input_value, **kwargs)
                self._mark_provider(response, provider_name)
                return response
            except AIControlError:
                raise
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _map(self, transform: Callable[[Any], Any]) -> "ProviderFailoverRunnable":
        return ProviderFailoverRunnable(
            [
                (provider_name, transform(candidate))
                for provider_name, candidate in self.candidates
            ]
        )

    @staticmethod
    def _mark_provider(response: Any, provider_name: str) -> None:
        """尽量把实际供应商写入 LangChain metadata，失败也不影响正常响应。"""

        metadata = getattr(response, "response_metadata", None)
        if isinstance(metadata, dict):
            metadata.setdefault("fundscope_provider", provider_name)


class ChatModelProviderService:
    """根据环境配置创建一个或多个 OpenAI 兼容 ChatModel。"""

    def create(
        self,
        *,
        max_retries: int,
        temperature: float,
        thinking: bool | None,
        timeout_seconds: float = 30,
        reasoning_effort: str | None = None,
    ) -> Any:
        try:
            from langchain_openai import ChatOpenAI
        except ModuleNotFoundError as exc:
            raise AgentConfigError(
                "请先安装依赖：pip install -r requirements.txt"
            ) from exc

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise AgentConfigError("模型响应超时必须是大于 0 的有限秒数")
        configs = self._provider_configs()
        model_class = ChatOpenAI
        if settings.ai_governance_enabled:
            from app.services.governed_model import GovernedChatOpenAI
            model_class = GovernedChatOpenAI
        candidates: list[tuple[str, Any]] = []
        for config in configs:
            extra_options: dict[str, Any] = {}
            if settings.ai_governance_enabled:
                # 关闭 SDK 内部不透明的重试，治理模型会逐次预留、发送、结算。
                extra_options.update(governance_provider=config.name,
                    governance_retries=max(0, max_retries))
            # 只在业务显式指定时传入；普通调用不覆盖供应商原来的思考强度。
            if reasoning_effort is not None:
                extra_options["reasoning_effort"] = reasoning_effort
            if thinking is not None:
                extra_options["extra_body"] = {
                    "thinking": {
                        "type": "enabled" if thinking else "disabled"
                    }
                }
            candidates.append(
                (
                    config.name,
                    model_class(
                        model=config.model,
                        api_key=config.api_key,
                        base_url=config.base_url,
                        temperature=temperature,
                        # 投研可显式使用更长预算；其他轻量调用保持原来的 30 秒。
                        timeout=timeout_seconds,
                        # Agent Recovery 仍控制“同一供应商重试几次”；这里的 fallback
                        # 只控制一次调用在供应商之间怎样切换。
                        max_retries=0 if settings.ai_governance_enabled else max(0, max_retries),
                        **extra_options,
                    ),
                )
            )

        if len(candidates) == 1:
            return candidates[0][1]
        return ProviderFailoverRunnable(candidates)

    def describe(self) -> list[dict[str, str | bool]]:
        """只暴露供应商名和模型，不返回 base URL 中凭据或 API Key。"""

        return [
            {
                "name": config.name,
                "model": config.model,
                "primary": index == 0,
            }
            for index, config in enumerate(self._provider_configs())
        ]

    @staticmethod
    def _provider_configs() -> list[OpenAICompatibleProviderConfig]:
        configs: list[OpenAICompatibleProviderConfig] = []
        if settings.deepseek_api_key:
            configs.append(
                OpenAICompatibleProviderConfig(
                    name="deepseek-primary",
                    model=settings.deepseek_model,
                    api_key=settings.deepseek_api_key,
                    base_url=settings.deepseek_base_url,
                )
            )
        if settings.llm_fallback_model and settings.llm_fallback_api_key:
            fallback = OpenAICompatibleProviderConfig(
                name="openai-compatible-fallback",
                model=settings.llm_fallback_model,
                api_key=settings.llm_fallback_api_key,
                base_url=settings.llm_fallback_base_url,
            )
            duplicate = any(
                item.model == fallback.model
                and item.base_url == fallback.base_url
                and item.api_key == fallback.api_key
                for item in configs
            )
            if not duplicate:
                configs.append(fallback)
        if not configs:
            raise AgentConfigError("请先设置 DEEPSEEK_API_KEY 或备用模型配置")
        return configs


chat_model_provider_service = ChatModelProviderService()


def get_chat_model(
    *,
    max_retries: int = 2,
    temperature: float = 0.2,
    thinking: bool | None = None,
    timeout_seconds: float = 30,
    reasoning_effort: str | None = None,
) -> Any:
    """创建主模型；配置备用供应商时返回具有相同接口的故障切换包装器。"""

    return chat_model_provider_service.create(
        max_retries=max_retries,
        temperature=temperature,
        thinking=thinking,
        timeout_seconds=timeout_seconds,
        **({"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}),
    )
