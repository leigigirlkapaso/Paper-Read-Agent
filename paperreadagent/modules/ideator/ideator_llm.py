"""
modules/ideator/ideator_llm.py
IdeatorLLM — 适配器：将 CoreLLM (deepseek API) 适配为
reviewer/auditor/roundtable 期望的 chat(model_role, messages, ...) 接口。

所有 6 坐席统一使用 deepseek-v4-pro，通过 core.llm 入口。
"""

from __future__ import annotations

import logging

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
        self._client = None

    def _get_client(self):
        if self._client is None:
            import openai
            self._client = openai.AsyncOpenAI(
                api_key=self._core_llm.api_key,
                base_url=self._core_llm.api_base_url,
                timeout=self._core_llm.timeout,
            )
        return self._client

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
        response_format: dict | None = None,
        model: str | None = None,
    ) -> str:
        model_name = model or self.model_for(model_role)
        client = self._get_client()
        kwargs = dict(
            model=model_name,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if response_format:
            kwargs["response_format"] = response_format
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            logger.error(f"[IdeatorLLM] {model_role} ({model_name}) call failed: {exc}")
            raise IdeatorLLMError(f"LLM call failed: {exc}") from exc
        content = resp.choices[0].message.content or ""
        reasoning = getattr(resp.choices[0].message, "reasoning_content", None)
        if not content and reasoning:
            logger.warning(
                "[IdeatorLLM] %s (%s): 思考耗尽输出预算 — reasoning=%d chars, content 为空",
                model_role, model_name, len(reasoning),
            )
        return content

    def load_prompt(self, module: str, name: str, **variables) -> str:
        """透传 CoreLLM 的 prompt 加载。"""
        return self._core_llm.load_prompt(module, name, **variables)
