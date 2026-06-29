"""
modules/ideator/ideator_llm.py
IdeatorLLM — 适配器：将 CoreLLM (deepseek API) 适配为
reviewer/auditor/roundtable 期望的 chat(model_role, messages, ...) 接口。

所有 6 坐席统一使用 deepseek-v4-pro，通过 core.llm 入口。
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

logger = logging.getLogger(__name__)


class IdeatorLLMError(Exception):
    """ideator LLM 调用失败。"""


class IdeatorLLM:
    """适配 CoreLLM 到 chat(model_role, messages, ...) 接口。

    所有 Agent 调用统一走 deepseek API (core.llm)，不再使用独立 API。
    model_for() 始终返回 core.llm 的 model_name (deepseek-v4-pro)。
    """

    def __init__(self, *, core_llm):
        self._core_llm = core_llm

    def model_for(self, role: str) -> str:
        """所有角色统一使用 core.llm 的模型 (deepseek-v4-pro)。"""
        return self._core_llm.model_name

    async def chat(
        self,
        *,
        model_role: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 16384,
    ) -> str:
        """委托 core.llm.achat() — 走统一的重试+限流+token 追踪。

        注意: CoreLLM.achat() 不支持 model 覆盖，所有角色统一使用
        core.llm 配置的模型 (deepseek-v4-pro)。model_for() 返回值
        仅用于日志/记录目的，不传给 API。
        """
        if not messages:
            raise IdeatorLLMError("chat() called with empty messages list")

        # Convert messages list to user_prompt + system_prompt strings
        system_parts = []
        user_parts = []
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if role == "system":
                system_parts.append(content)
            elif role == "user":
                user_parts.append(content)
            elif role == "assistant":
                user_parts.append(f"[Assistant]: {content}")
            elif role == "tool":
                user_parts.append(
                    f"[Tool result ({m.get('tool_call_id', '')})]: {content}"
                )

        system_prompt = "\n\n".join(system_parts) if system_parts else None
        user_prompt = "\n\n".join(user_parts)

        try:
            content, _usage = await self._core_llm.achat(
                user_prompt=user_prompt,
                system_prompt=system_prompt,
                module="ideator",
                purpose=model_role,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            logger.error(f"[IdeatorLLM] {model_role} call failed: {exc}")
            raise IdeatorLLMError(f"LLM call failed: {exc}") from exc

        return content or ""

    def load_prompt(self, module: str, name: str, **variables) -> str:
        """透传 CoreLLM 的 prompt 加载。"""
        return self._core_llm.load_prompt(module, name, **variables)

    async def chat_stream(
        self,
        *,
        model_role: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int = 32768,
    ) -> AsyncGenerator[str, None]:
        """Streaming counterpart to chat(). Yields delta strings.

        NOTE: unlike chat(), no 3-retry-on-JSON-parse logic. Roundtable agent
        replies are freeform markdown (not JSON), so JSON guardrail is not
        needed. Network errors propagate to caller (AgentTeam handles them).
        """
        if not messages:
            raise IdeatorLLMError("chat_stream() called with empty messages list")
        async for delta in self._core_llm.chat_stream(
            messages,
            module="ideator",
            purpose=model_role,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            yield delta
