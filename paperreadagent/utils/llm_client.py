"""
utils/llm_client.py
统一的 LLM 调用封装，基于 openai SDK（兼容任意 OpenAI 格式接口）。
支持同步调用（AGENT1 使用）和异步调用（AGENT2 并发使用）。
Phase 4.2：Token 用量追踪。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import openai

logger = logging.getLogger(__name__)


@dataclass
class LLMUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, other: "LLMUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class LLMClient:
    """
    封装 OpenAI 兼容接口的同步 + 异步客户端。

    使用示例：
        client = LLMClient.from_config(cfg["llm"])
        response, usage = client.chat("你好")
        response, usage = await client.achat("你好")
    """

    def __init__(
        self,
        api_key: str,
        api_base_url: str,
        model_name: str,
        temperature: float = 0.3,
        timeout: float = 300.0,
    ) -> None:
        self.model_name = model_name
        self.temperature = temperature
        self.total_usage = LLMUsage()

        self._sync_client = openai.OpenAI(
            api_key=api_key,
            base_url=api_base_url,
            timeout=timeout,
        )
        self._async_client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=api_base_url,
            timeout=timeout,
        )

    @classmethod
    def from_config(cls, llm_cfg: dict) -> "LLMClient":
        return cls(
            api_key=llm_cfg["api_key"],
            api_base_url=llm_cfg["api_base_url"],
            model_name=llm_cfg["model_name"],
            temperature=llm_cfg.get("temperature", 0.3),
        )

    # ── 同步调用 ──────────────────────────────────────────────
    def chat(
        self,
        user_prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: int = 16384,
    ) -> tuple[str, LLMUsage]:
        """同步调用，返回 (回复文本, 用量)。"""
        messages = self._build_messages(user_prompt, system_prompt)
        try:
            resp = self._sync_client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=self.temperature,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content or ""
            usage = LLMUsage(
                prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
                completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
                total_tokens=resp.usage.total_tokens if resp.usage else 0,
            )
            self.total_usage.add(usage)
            return content, usage
        except Exception as e:
            logger.error(f"[LLMClient] 同步调用失败: {e}")
            raise

    # ── 异步调用 ──────────────────────────────────────────────
    async def achat(
        self,
        user_prompt: str,
        system_prompt: Optional[str] = None,
        max_tokens: int = 16384,
    ) -> tuple[str, LLMUsage]:
        """异步调用，返回 (回复文本, 用量)。"""
        messages = self._build_messages(user_prompt, system_prompt)
        try:
            resp = await self._async_client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=self.temperature,
                max_tokens=max_tokens,
            )
            content = resp.choices[0].message.content or ""
            usage = LLMUsage(
                prompt_tokens=resp.usage.prompt_tokens if resp.usage else 0,
                completion_tokens=resp.usage.completion_tokens if resp.usage else 0,
                total_tokens=resp.usage.total_tokens if resp.usage else 0,
            )
            self.total_usage.add(usage)
            return content, usage
        except Exception as e:
            logger.error(f"[LLMClient] 异步调用失败: {e}")
            raise

    # ── 向后兼容：仅返回文本 ─────────────────────────────────
    def chat_text(
        self,
        user_prompt: str,
        system_prompt: Optional[str] = None,
    ) -> str:
        content, _ = self.chat(user_prompt, system_prompt)
        return content

    async def achat_text(
        self,
        user_prompt: str,
        system_prompt: Optional[str] = None,
    ) -> str:
        content, _ = await self.achat(user_prompt, system_prompt)
        return content

    # ── 内部工具 ──────────────────────────────────────────────
    @staticmethod
    def _build_messages(
        user_prompt: str,
        system_prompt: Optional[str],
    ) -> list[dict]:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})
        return messages
