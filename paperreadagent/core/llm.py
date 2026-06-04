"""
core/llm.py
CoreLLM — 统一 LLM 入口。包装现有 LLMClient，增加 streaming、embedding、prompt 加载。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import AsyncGenerator, Optional

from jinja2 import Environment, FileSystemLoader, BaseLoader

from .decorators import stable, evolving
from .database import CoreDatabase

logger = logging.getLogger(__name__)


class CoreLLM:
    """
    核心 LLM 统一入口。所有模块必须通过此接口调用 LLM。

    封装现有 LLMClient（同步+异步），额外提供：
    - chat_stream: 异步流式对话
    - embed: 文本 embedding
    - load_prompt: Jinja2 prompt 加载渲染
    - 自动 Usage 追踪到 core_llm_usage 表
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_base_url: str,
        model_name: str,
        temperature: float = 0.3,
        timeout: float = 300.0,
        embedding_model: str = "BAAI/bge-m3",
        embedding_provider: str = "local",
        db: CoreDatabase | None = None,
    ):
        self.api_key = api_key
        self.api_base_url = api_base_url
        self.model_name = model_name
        self.temperature = temperature
        self.timeout = timeout
        self.embedding_model = embedding_model
        self.embedding_provider = embedding_provider
        self._db = db
        self._local_embedder = None

        import openai
        self._sync_client = openai.OpenAI(
            api_key=api_key, base_url=api_base_url, timeout=timeout,
        )
        self._async_client = openai.AsyncOpenAI(
            api_key=api_key, base_url=api_base_url, timeout=timeout,
        )

        self._jinja_env = Environment(loader=BaseLoader(), autoescape=False)
        self._jinja_cache: dict[str, Environment] = {}

    @classmethod
    def from_config(cls, llm_cfg: dict, *, db: CoreDatabase | None = None) -> "CoreLLM":
        return cls(
            api_key=llm_cfg["api_key"],
            api_base_url=llm_cfg["api_base_url"],
            model_name=llm_cfg["model_name"],
            temperature=llm_cfg.get("temperature", 0.3),
            embedding_model=llm_cfg.get("embedding_model", "BAAI/bge-m3"),
            embedding_provider=llm_cfg.get("embedding_provider", "local"),
            db=db,
        )

    # ── 同步对话 ──────────────────────────────────────────────

    @stable
    def chat(
        self,
        user_prompt: str,
        system_prompt: Optional[str] = None,
        *,
        module: str = "core",
        purpose: str = "chat",
        max_tokens: int = 16384,
    ) -> tuple[str, dict]:
        """同步对话，返回 (文本, usage_dict)。自动记录用量。"""
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        resp = self._sync_client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=self.temperature,
            max_tokens=max_tokens,
        )
        if not resp.choices:
            logger.error(
                f"[CoreLLM] API 返回空 choices，raw: {resp.model_dump_json()[:500]}"
            )
            return "", {}
        content = resp.choices[0].message.content or ""
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
            "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
            "total_tokens": resp.usage.total_tokens if resp.usage else 0,
        }
        self._track_usage(module, purpose, usage)
        return content, usage

    # ── 异步对话 ──────────────────────────────────────────────

    @evolving
    async def achat(
        self,
        user_prompt: str,
        system_prompt: str | None = None,
        *,
        module: str = "core",
        purpose: str = "chat",
        max_tokens: int = 16384,
    ) -> tuple[str, dict]:
        """异步对话，返回 (文本, usage_dict)。避免模块自行 asyncio.to_thread 包装。"""
        return await asyncio.to_thread(
            self.chat, user_prompt, system_prompt,
            module=module, purpose=purpose, max_tokens=max_tokens,
        )

    # ── 工具调用对话 ──────────────────────────────────────────

    @stable
    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
        module: str = "core",
        purpose: str = "chat_with_tools",
        max_tokens: int = 16384,
        temperature: float | None = None,
    ) -> dict:
        """同步工具调用对话。

        参数:
            messages: OpenAI 格式消息列表 [{"role":"...", "content":"..."}]
            tools: OpenAI 格式工具定义列表 [{"type":"function", "function":{...}}]
            tool_choice: "auto" | "none" | "required"
            module, purpose: 用量追踪标签
            max_tokens: 最大输出 token
            temperature: 覆盖默认温度

        返回:
            {
                "content": str | None,
                "tool_calls": [{"id": str, "name": str, "arguments": dict}] | None,
                "usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int},
                "finish_reason": str,
            }
        """
        import json as _json

        resp = self._sync_client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature if temperature is not None else self.temperature,
            max_tokens=max_tokens,
        )

        if not resp.choices:
            logger.error(
                "[CoreLLM] chat_with_tools API 返回空 choices，raw: %s",
                resp.model_dump_json()[:500] if hasattr(resp, "model_dump_json") else str(resp)[:500],
            )
            return dict(content="", tool_calls=None, usage={}, finish_reason="error")

        usage = {
            "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
            "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
            "total_tokens": resp.usage.total_tokens if resp.usage else 0,
        }
        self._track_usage(module, purpose, usage)

        choice = resp.choices[0]
        msg = choice.message
        finish_reason = getattr(choice, "finish_reason", "stop") or "stop"

        tool_calls = None
        if msg.tool_calls:
            tool_calls = []
            for tc in msg.tool_calls:
                if not tc.function:
                    logger.warning("[CoreLLM] tool_call 缺少 function 字段，跳过")
                    continue
                try:
                    from paperreadagent.utils.json_utils import clean_json
                    args = _json.loads(clean_json(tc.function.arguments))
                except (_json.JSONDecodeError, TypeError):
                    logger.warning(
                        "[CoreLLM] tool_calls arguments JSON 解析失败: %s",
                        tc.function.arguments[:200],
                    )
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })

        return dict(
            content=msg.content or None,
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
        )

    @evolving
    async def achat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
        module: str = "core",
        purpose: str = "chat_with_tools",
        max_tokens: int = 16384,
        temperature: float | None = None,
    ) -> dict:
        """异步工具调用对话。返回格式同 chat_with_tools。"""
        return await asyncio.to_thread(
            self.chat_with_tools, messages, tools,
            tool_choice=tool_choice, module=module, purpose=purpose,
            max_tokens=max_tokens, temperature=temperature,
        )

    # ── 异步流式对话 ──────────────────────────────────────────

    @evolving
    async def chat_stream(
        self,
        messages: list[dict],
        *,
        module: str = "core",
        purpose: str = "chat",
        temperature: float | None = None,
        max_tokens: int = 8192,
    ) -> AsyncGenerator[str, None]:
        """
        异步流式对话，yield 每个 delta chunk。
        messages 格式：[{"role": "system"|"user"|"assistant", "content": "..."}]
        """
        resp = await self._async_client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=temperature if temperature is not None else self.temperature,
            stream=True,
            stream_options={"include_usage": True},
            max_tokens=max_tokens,
        )
        full = []
        async for chunk in resp:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                full.append(delta.content)
                yield delta.content

        # 估算 token（精确计数需 tiktoken，这里用字符估算）
        prompt_chars = sum(len(m.get("content", "")) for m in messages)
        completion_chars = len("".join(full))
        usage = {
            "prompt_tokens": max(1, prompt_chars // 2),
            "completion_tokens": max(1, completion_chars // 2),
            "total_tokens": max(1, (prompt_chars + completion_chars) // 2),
        }
        self._track_usage(module, purpose, usage)

    # ── Embedding ──────────────────────────────────────────────

    @evolving
    async def embed(self, text: str, *, module: str = "core") -> list[float]:
        """生成文本 embedding 向量。provider=local 用本地模型，remote 调 API。"""
        if self.embedding_provider == "local":
            return await self._embed_local(text)
        return await self._embed_remote(text, module)

    async def _embed_local(self, text: str) -> list[float]:
        """本地 BGE 模型 embedding（sentence-transformers）。失败时下次重试。"""
        loop = asyncio.get_running_loop()

        def _run():
            if self._local_embedder is None:
                import os
                # 国内网络环境：镜像 + 短超时 + 少重试，避免长时间阻塞
                os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
                os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "15")
                os.environ.setdefault("REQUESTS_MAX_RETRIES", "1")
                from sentence_transformers import SentenceTransformer
                logger.info(f"[CoreLLM] 加载本地 embedding 模型: {self.embedding_model}")
                self._local_embedder = SentenceTransformer(
                    self.embedding_model,
                    trust_remote_code=True,
                )
            return self._local_embedder.encode(
                text, normalize_embeddings=True,
            ).tolist()

        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, _run), timeout=20,
            )
        except Exception:
            self._local_embedder = None
            logger.warning("[CoreLLM] 本地 embedding 模型暂时不可用，下次调用重试", exc_info=True)
            return []

    async def _embed_remote(self, text: str, module: str) -> list[float]:
        """远程 API embedding。不支持时静默返回空列表。"""
        try:
            resp = await self._async_client.embeddings.create(
                model=self.embedding_model,
                input=text,
            )
            embedding = resp.data[0].embedding
            self._track_usage(module, "embedding", {
                "prompt_tokens": max(1, len(text) // 2),
                "completion_tokens": 0,
                "total_tokens": max(1, len(text) // 2),
            })
            return embedding
        except Exception:
            logger.warning(
                f"[CoreLLM] embedding API 失败（{self.api_base_url}），跳过", exc_info=True
            )
            return []

    # ── Prompt 加载 ───────────────────────────────────────────

    @stable
    def load_prompt(self, module: str, name: str, **variables) -> str:
        """
        加载并渲染 Jinja2 prompt 模板。
        模板文件应位于 modules/{module}/prompts/{name}.jinja2。
        Jinja2 Environment 按模块缓存，避免每次调用重新创建。
        """
        template_path = Path("paperreadagent/modules") / module / "prompts" / f"{name}.jinja2"
        if template_path.exists():
            cache_key = str(template_path.parent)
            if cache_key not in self._jinja_cache:
                self._jinja_cache[cache_key] = Environment(
                    loader=FileSystemLoader(cache_key), autoescape=False
                )
            env = self._jinja_cache[cache_key]
            tmpl = env.get_template(template_path.name)
            return tmpl.render(**variables)

        # 回退：如果文件不存在，尝试从字符串加载
        tmpl = self._jinja_env.from_string(name)
        return tmpl.render(**variables)

    @stable
    @staticmethod
    def extract_json_list(raw: str) -> list[str]:
        """从 LLM 输出中提取字符串列表（JSON 容错解析）。"""
        from paperreadagent.utils.json_utils import extract_json_list
        return extract_json_list(raw)

    # ── 内部 ──────────────────────────────────────────────────

    def _track_usage(self, module: str, purpose: str, usage: dict) -> None:
        if self._db:
            try:
                self._db.record_llm_usage(
                    source_module=module,
                    purpose=purpose,
                    model_name=self.model_name,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                )
            except Exception:
                logger.exception("[CoreLLM] 用量记录失败")
