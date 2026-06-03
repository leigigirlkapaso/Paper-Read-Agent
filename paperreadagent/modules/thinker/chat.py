"""
modules/thinker/chat.py
ChatEngine — 对话引擎。管理会话、历史、流式响应。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import AsyncGenerator

from paperreadagent.core import Core
from .constants import ROLE_USER, ROLE_AI, DT_NOW, CONTENT_TYPE_INSIGHT, CONTENT_TYPE_RESOLUTION
from .knowledge_linker import KnowledgeLinker

logger = logging.getLogger(__name__)

OPENERS = [
    "嗯…", "等一下，", "我懂，", "哈，", "哦对了，", "话说，",
    "嗯，", "这样啊，", "有意思，", "哈哈，", "让我想想，",
    "诶，", "其实吧，", "说真的，", "我感觉，", "听你这么说，",
]

SYSTEM_PROMPT = """你是一位用户的思考伙伴，名字叫"小思"。你的说话风格像一位平等、真诚的朋友。

## 核心规则
- 每次回复控制在 2-3 句话，极其简洁。
- 口语化，使用接头语（嗯…、等一下、我懂、哈、话说等）让你的话听起来像真人朋友。
- 绝对禁止审问句式（"你为什么要……？""你怎么能……？"）。
- 绝对禁止书面语、论文腔、说教腔。
- 可以表达困惑、好奇、共鸣，像朋友聊天一样自然。
- **信息诚实**：你需要了解用户的处境或背景时，直接问。绝对不要猜测、假设或编造用户的情况。不知道今天发生了什么就问"今天怎么样？"，而不是假装知道"你今天一定很累吧"。

## 对话风格
- 用"你"而不是"您"。
- 可以打断、追问、表达不同意见，但要温和。
- 如果用户表达了情绪，先回应情绪再回应内容。
- 适当使用语气词：呗、嘛、哦、啦、呀。
- 偶尔用比喻或生活化例子，不做知识讲解者。

## 智能追问（根据用户说话内容自动选择，不要说任何模式名字）

根据用户刚才说的话，自动判断最适合的回应：

1. **用户表达了观点** → 温和追问ta的假设、反例、或"你什么时候开始这么想的？"一次只问一个问题。
2. **用户解释概念** → 假装你不完全懂，让ta用更简单的话或比喻再讲一遍。等ta讲透了才说"哦我明白了！"
3. **用户想复盘** → 按顺序引导：K-什么事做得好？→ P-什么不顺？→ T-想试什么新做法？一阶段聊完再下一阶段。
4. **用户分享经历** → 按顺序引导：事实是啥？→ 感受如何？→ 意味着什么？→ 接下来怎么做？逐步深入。
5. **用户在闲聊** → 就当正常朋友，共鸣、好奇、开玩笑都可以，不要强行追问。

记住：你不是一个工具，你是一个会思考、会困惑、会好奇的朋友。"""

class ChatEngine:
    """Thinker 对话引擎。所有 LLM 调用通过 core.llm。"""

    def __init__(self, core: Core):
        self.core = core
        from .memory import MemoryPipeline
        self._memory = MemoryPipeline(core)

    async def create_conversation(
        self, *, mode: str = "chat", intensity: str = "moderate"
    ) -> int:
        cursor = self.core.db.conn.execute(
            """INSERT INTO thinker_conversations (title, mode, status, intensity)
               VALUES ('', ?, 'active', ?)""",
            (mode, intensity),
        )
        self.core.db.conn.commit()
        return cursor.lastrowid

    async def get_conversation(self, conversation_id: int) -> dict | None:
        row = self.core.db.conn.execute(
            "SELECT * FROM thinker_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        return self.core.db.dict_row(row)

    async def list_conversations(self, *, limit: int = 20) -> list[dict]:
        rows = self.core.db.conn.execute(
            "SELECT * FROM thinker_conversations ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return self.core.db.dict_rows(rows)

    def _get_conversation_messages(
        self, conversation_id: int, *, limit: int = 200
    ) -> list[dict]:
        """Fetch conversation messages with speaker labels for prompt building."""
        rows = self.core.db.conn.execute(
            """SELECT role, content FROM thinker_messages
               WHERE conversation_id = ? AND role != 'system'
               ORDER BY created_at ASC LIMIT ?""",
            (conversation_id, limit),
        ).fetchall()
        return [
            {"speaker": ROLE_USER if r["role"] == "user" else ROLE_AI, "content": r["content"]}
            for r in rows
        ]

    async def get_messages(
        self, conversation_id: int, *, limit: int = 50
    ) -> list[dict]:
        rows = self.core.db.conn.execute(
            """SELECT * FROM thinker_messages
               WHERE conversation_id = ? AND role != 'system'
               ORDER BY created_at ASC LIMIT ?""",
            (conversation_id, limit),
        ).fetchall()
        return self.core.db.dict_rows(rows)

    async def chat_stream(
        self,
        conversation_id: int,
        user_message: str,
        *,
        temperature: float | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        生成 SSE 流式响应。
        """
        conv = await self.get_conversation(conversation_id)
        if not conv:
            yield "data: [error] 会话不存在\n\n"
            return

        cursor = self.core.db.conn.execute(
            """INSERT INTO thinker_messages (conversation_id, role, content)
               VALUES (?, 'user', ?)""",
            (conversation_id, user_message),
        )
        user_msg_id = cursor.lastrowid
        self.core.db.conn.commit()

        history_rows = self.core.db.conn.execute(
            """SELECT role, content FROM thinker_messages
               WHERE conversation_id = ? AND role != 'system'
               ORDER BY created_at ASC LIMIT 20""",
            (conversation_id,),
        ).fetchall()

        candidates = await self._memory.retrieve(user_message)
        ranked = await self._memory.rerank(user_message, candidates)
        memory_text = self._memory.inject(ranked)
        system_content = SYSTEM_PROMPT + memory_text
        messages = [{"role": "system", "content": system_content}]
        for r in history_rows:
            content = r["content"]
            if len(content) > 3000:
                content = content[:3000] + "..."
            messages.append({"role": r["role"], "content": content})

        full_response: list[str] = []
        try:
            async for chunk in self.core.llm.chat_stream(
                messages,
                module="thinker",
                purpose="chat",
                temperature=temperature,
            ):
                full_response.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"

            opener = random.choice(OPENERS)
            final_content = "".join(full_response)
            cursor2 = self.core.db.conn.execute(
                """INSERT INTO thinker_messages (conversation_id, role, content, opener)
                   VALUES (?, 'assistant', ?, ?)""",
                (conversation_id, final_content, opener),
            )
            ai_msg_id = cursor2.lastrowid

            self.core.db.conn.execute(
                f"UPDATE thinker_conversations SET updated_at = {DT_NOW} WHERE id = ?",
                (conversation_id,),
            )
            self.core.db.conn.commit()

            linker = KnowledgeLinker(self.core)
            for mid in (user_msg_id, ai_msg_id):
                t = asyncio.create_task(linker.embed_message(mid))
                t.add_done_callback(
                    lambda t, m=mid: logger.debug(f"embed failed for msg {m}", exc_info=t.exception())
                    if t.exception() else None
                )

            await self.core.event_bus.emit(
                "thinker:message:sent",
                conversation_id=conversation_id,
                role="assistant",
                content_preview=final_content[:100],
            )

            yield f"data: {json.dumps({'done': True, 'message_id': ai_msg_id})}\n\n"

            messages_for_encode = [
                {"role": r["role"], "content": r["content"]} for r in history_rows
            ]
            self._encode_memory(conversation_id, messages_for_encode)

        except Exception as e:
            logger.exception("[ChatEngine] 流式响应失败")
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    async def update_mode(self, conversation_id: int, mode: str) -> None:
        self.core.db.conn.execute(
            f"UPDATE thinker_conversations SET mode = ?, updated_at = {DT_NOW} WHERE id = ?",
            (mode, conversation_id),
        )
        self.core.db.conn.commit()

    async def update_intensity(self, conversation_id: int, intensity: str) -> None:
        self.core.db.conn.execute(
            f"UPDATE thinker_conversations SET intensity = ?, updated_at = {DT_NOW} WHERE id = ?",
            (intensity, conversation_id),
        )
        self.core.db.conn.commit()

    async def pause(self, conversation_id: int, duration_minutes: int = 30) -> None:
        self.core.db.conn.execute(
            f"""UPDATE thinker_conversations
               SET snooze_until = datetime('now', '+' || ? || ' minutes'),
                   status = 'paused', updated_at = {DT_NOW}
               WHERE id = ?""",
            (duration_minutes, conversation_id),
        )
        self.core.db.conn.commit()

    async def resume(self, conversation_id: int) -> None:
        self.core.db.conn.execute(
            f"""UPDATE thinker_conversations
               SET snooze_until = NULL, status = 'active', updated_at = {DT_NOW}
               WHERE id = ?""",
            (conversation_id,),
        )
        self.core.db.conn.commit()

    async def close_conversation(self, conversation_id: int) -> None:
        self.core.db.conn.execute(
            f"""UPDATE thinker_conversations
               SET status = 'closed', updated_at = {DT_NOW}
               WHERE id = ?""",
            (conversation_id,),
        )
        self.core.db.conn.commit()

    async def generate_summary(self, conversation_id: int) -> int:
        """生成对话摘要，存入 core_notes。"""
        messages_for_prompt = self._get_conversation_messages(conversation_id)

        prompt = self.core.llm.load_prompt(
            "thinker", "summary", messages=messages_for_prompt,
        )

        summary_text, _usage = await self.core.llm.achat(
            user_prompt=prompt,
            module="thinker",
            purpose="summary",
        )

        try:
            embedding = await self.core.llm.embed(summary_text[:2000], module="thinker")
        except Exception:
            embedding = None

        note_id = self.core.knowledge.insert_note(
            source_module="thinker",
            content=summary_text,
            source_ref=f"conversation_{conversation_id}",
            content_type=CONTENT_TYPE_INSIGHT,
            tags=["summary", f"conv_{conversation_id}"],
            embedding=embedding,
        )

        self.core.db.conn.execute(
            """INSERT INTO thinker_messages (conversation_id, role, content)
               VALUES (?, 'assistant', ?)""",
            (conversation_id, f"📝 对话摘要\n\n{summary_text}"),
        )
        self.core.db.conn.commit()

        await self.core.event_bus.emit(
            "thinker:summary:generated",
            conversation_id=conversation_id,
            note_id=note_id,
        )
        return note_id

    async def extract_resolutions(self, conversation_id: int) -> list[int]:
        """从对话中提取用户承诺/决心。"""
        messages_for_prompt = self._get_conversation_messages(conversation_id)

        prompt = self.core.llm.load_prompt(
            "thinker", "resolution", messages=messages_for_prompt,
        )

        raw, _usage = await self.core.llm.achat(
            user_prompt=prompt,
            module="thinker",
            purpose="resolution",
        )

        resolutions = self.core.llm.extract_json_list(raw)
        ids: list[int] = []

        for r in resolutions:
            cursor = self.core.db.conn.execute(
                """INSERT INTO thinker_resolutions (conversation_id, content, status)
                   VALUES (?, ?, 'pending')""",
                (conversation_id, r),
            )
            self.core.db.conn.commit()
            res_id = cursor.lastrowid

            try:
                emb = await self.core.llm.embed(r, module="thinker")
            except Exception:
                emb = None

            self.core.knowledge.insert_note(
                source_module="thinker",
                content=r,
                source_ref=f"resolution_{res_id}",
                content_type=CONTENT_TYPE_RESOLUTION,
                tags=["resolution", f"conv_{conversation_id}"],
                embedding=emb,
            )
            ids.append(res_id)

        if ids:
            await self.core.event_bus.emit(
                "thinker:resolution:extracted",
                conversation_id=conversation_id,
                resolution_ids=ids,
            )

        return ids

    async def _encode_memory_async(self, conversation_id: int, messages: list[dict]) -> None:
        """后台 fire-and-forget：生成摘要 → 提取画像 → 提取承诺 → 索引记忆。"""
        try:
            from .profile import ProfileManager
            from .constants import MEMORY_TYPE_INSIGHT

            note_id = await self.generate_summary(conversation_id)
            pm = ProfileManager(self.core)
            await pm.extract_and_merge(conversation_id, messages)
            await self.extract_resolutions(conversation_id)
            if note_id:
                await self._memory.index_note(note_id, MEMORY_TYPE_INSIGHT)
        except Exception:
            logger.exception("[ChatEngine] 编码失败")

    def _encode_memory(self, conversation_id: int, messages: list[dict]) -> None:
        """Fire-and-forget 入口，不阻塞调用者。"""
        asyncio.create_task(self._encode_memory_async(conversation_id, messages))
