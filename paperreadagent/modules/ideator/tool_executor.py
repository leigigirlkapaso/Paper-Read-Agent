"""tool_executor.py — ToolExecutor: 将 ToolRegistry 工具定义映射到实际执行。"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger(__name__)

# S2 阶段不可用的圆桌专属工具
_ROUNDTABLE_ONLY = {
    "create_spark", "update_spark", "trigger_recall",
    "fetch_snapshot", "write_memory", "read_memory",
    "report_watermark", "adjust_quota", "grant_tool",
}

# S2 阶段可用工具
_S2_AVAILABLE = {"search_papers", "read_paper", "read_note", "check_duplicate", "audit_claim"}

_TYPE_MAP = {"str": "string", "int": "integer", "float": "number", "list": "array"}
_MAX_PAPER_CHARS = 8000
_TOOL_TIMEOUT = 30


class ToolExecutor:
    """将 ToolRegistry 的工具定义映射到可执行函数。

    依赖:
        - data_access (DataAccess) — 访问论文、笔记、火花
        - core_llm (CoreLLM) — embedding + audit_claim LLM 调用
        - tool_registry (ToolRegistry) — 工具定义
    """

    def __init__(self, *, data_access, core_llm, tool_registry):
        self._data = data_access
        self._llm = core_llm
        self._registry = tool_registry

    def to_openai_tools(self) -> list[dict]:
        """将 14 个工具全部转为 OpenAI tools 格式。S2 阶段无 RBAC 限制，
        但圆桌专属工具执行时返回不可用提示。"""
        tools = []
        all_tool_names = _S2_AVAILABLE | _ROUNDTABLE_ONLY
        if self._registry:
            for name in all_tool_names:
                tool_def = self._registry.get_tool(name)
                if tool_def:
                    properties = {}
                    for k, v in tool_def.parameters.items():
                        json_type = _TYPE_MAP.get(v, "string")
                        properties[k] = {"type": json_type}
                        if v == "list":
                            properties[k]["items"] = {"type": "string"}
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": tool_def.name,
                            "description": tool_def.description,
                            "parameters": {
                                "type": "object",
                                "properties": properties,
                                "required": list(tool_def.parameters.keys()),
                            },
                        },
                    })
        return tools

    async def execute(self, tool_name: str, arguments: dict) -> str:
        """执行单个工具调用，返回结果字符串。不抛异常。"""
        try:
            return await asyncio.wait_for(
                self._execute_inner(tool_name, arguments),
                timeout=_TOOL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return f"工具 '{tool_name}' 执行超时（{_TOOL_TIMEOUT}s），请尝试缩小查询范围。"
        except Exception as e:
            logger.warning("[ToolExecutor] %s 执行失败: %s", tool_name, e, exc_info=True)
            return f"工具 '{tool_name}' 执行失败: {e}"

    async def _execute_inner(self, tool_name: str, arguments: dict) -> str:
        if tool_name in _ROUNDTABLE_ONLY:
            return f"工具 '{tool_name}' 仅在圆桌讨论中可用，当前火花生成阶段不可调用。"

        if tool_name == "search_papers":
            return await self._search_papers(arguments)
        elif tool_name == "read_paper":
            return await self._read_paper(arguments)
        elif tool_name == "read_note":
            return await self._read_note(arguments)
        elif tool_name == "check_duplicate":
            return await self._check_duplicate(arguments)
        elif tool_name == "audit_claim":
            return await self._audit_claim(arguments)
        else:
            return f"未知工具: {tool_name}"

    # ── 具体工具实现 ─────────────────────────────────

    async def _search_papers(self, args: dict) -> str:
        """Embedding 搜索论文和笔记。"""
        if not self._data or not self._llm:
            return "搜索服务不可用"
        query = args.get("query", "")
        top_k = int(args.get("top_k", 5))
        if not query:
            return "search_papers 需要 query 参数"
        try:
            emb = await self._llm.embed(query, module="ideator")
            if not emb:
                return "搜索 embedding 生成失败"
            results = self._data.search_core_notes(emb, top_k=top_k, min_similarity=0.3)
            if not results:
                return "未找到相关论文或笔记"
            lines = ["找到以下相关内容:"]
            for r in results[:top_k]:
                content_preview = (r.get("content", "") or "")[:300]
                lines.append(
                    f"- [{r.get('source_module', '?')}] {content_preview}"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"搜索失败: {e}"

    async def _read_paper(self, args: dict) -> str:
        """获取论文全文。"""
        if not self._data:
            return "论文数据访问不可用"
        arxiv_id = args.get("arxiv_id", "")
        paper_id = args.get("paper_id", 0)

        paper = None
        if paper_id:
            paper = self._data.get_paper(int(paper_id))
        if not paper and arxiv_id:
            try:
                paper = self._data.get_paper_by_arxiv_id(arxiv_id)
            except Exception:
                logger.warning("[ToolExecutor] get_paper_by_arxiv_id 失败", exc_info=True)
            # Fall back to full scan (includes notes)
            if not paper:
                try:
                    all_papers = self._data.get_all_papers_with_notes()
                    for p in all_papers:
                        if p.get("arxiv_id", "") == arxiv_id:
                            paper = p
                            break
                except Exception:
                    logger.warning("[ToolExecutor] get_all_papers_with_notes 失败", exc_info=True)

        if not paper:
            return f"未找到论文: arxiv_id={arxiv_id}, paper_id={paper_id}"

        parts = [f"标题: {paper.get('title', '未知')}"]
        if paper.get("abstract"):
            parts.append(f"摘要: {paper['abstract']}")

        full_text = paper.get("full_text", "") or paper.get("_full_text", "")
        if full_text:
            parts.append(f"全文 (截断到 {_MAX_PAPER_CHARS} 字):\n{full_text[:_MAX_PAPER_CHARS]}")

        note = self._data.get_user_note(paper.get("id", paper_id) if paper.get("id") else 0)
        if note and note.get("content"):
            parts.append(f"用户笔记:\n{note['content'][:_MAX_PAPER_CHARS]}")

        return "\n\n".join(parts)

    async def _read_note(self, args: dict) -> str:
        """获取用户笔记。"""
        if not self._data:
            return "笔记数据访问不可用"
        paper_id = int(args.get("paper_id", 0))
        if not paper_id:
            return "read_note 需要 paper_id 参数"
        note = self._data.get_user_note(paper_id)
        if not note or not note.get("content"):
            return f"论文 {paper_id} 没有笔记"
        return f"论文 {paper_id} 笔记:\n{note['content'][:_MAX_PAPER_CHARS]}"

    async def _check_duplicate(self, args: dict) -> str:
        """检查火花是否与已有火花重复。"""
        if not self._data or not self._llm:
            return "重复检查服务不可用"
        content = args.get("content", "")
        if not content:
            return "check_duplicate 需要 content 参数"

        try:
            emb = await self._llm.embed(content, module="ideator")
            if not emb:
                return "Embedding 生成失败，无法检查重复"
            similar = self._data.find_similar_sparks_by_embedding(
                emb, top_k=3, min_similarity=0.60,
            )
            if not similar:
                return "未发现重复火花（无相似度 ≥ 0.60 的已有火花）"
            lines = [f"发现 {len(similar)} 个可能重复的火花:"]
            for s in similar:
                lines.append(
                    f"  - [ID={s['id']} sim={s['similarity']}] {s['content'][:200]}"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"重复检查失败: {e}"

    async def _audit_claim(self, args: dict) -> str:
        """用 LLM 验证一个声明是否有来源支撑。"""
        if not self._llm:
            return "审计服务不可用"
        claim = args.get("claim", "")
        source_text = args.get("source_text", "")
        if not claim:
            return "audit_claim 需要 claim 参数"

        prompt = (
            f"请验证以下声明是否有来源支撑。\n\n"
            f"声明:\n```\n{claim}\n```\n"
            f"来源文本:\n```\n{source_text[:3000] if source_text else '（无来源文本）'}\n```\n\n"
            f"返回 JSON: {{\"verdict\": \"SUPPORTED|UNSUPPORTED|UNCERTAIN\", \"reason\": \"简短理由\"}}"
        )
        try:
            raw, _ = self._llm.chat(
                user_prompt=prompt, module="ideator", purpose="audit_claim",
                max_tokens=2048,
            )
            from paperreadagent.utils.json_utils import clean_json
            raw = clean_json(raw)
            data = json.loads(raw)
            return f"审计结果: {data.get('verdict', 'UNCERTAIN')} — {data.get('reason', '')}"
        except Exception as e:
            return f"审计失败: {e}"
