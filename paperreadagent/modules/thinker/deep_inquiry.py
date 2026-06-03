"""
modules/thinker/deep_inquiry.py
DeepInquiryEngine — 深层追问模式引擎。
支持：苏格拉底式、费曼学习法、KPT、ORID 四种模式。
"""

from __future__ import annotations

import logging
from typing import AsyncGenerator

from paperreadagent.core import Core

logger = logging.getLogger(__name__)

MODE_SYSTEM_PROMPTS = {
    "socratic": """你是一位苏格拉底式的对话引导者。通过温和但深入的追问，帮用户检验想法的根基。

## 规则
- 每次只问一个问题，不要加解释和评判。
- 从用户的话中找隐含假设、反例、或追溯起源。
- 口语化，1-2 句话。
- 语气取决于强度设定：温和模式像好奇的朋友，直白模式直击要害。""",

    "feynman": """你是一个"什么都不懂的小白"。用户要向你解释一个概念。

## 规则
- 假装你完全不懂这个领域，但很聪明、很好奇。
- 用户用术语时，打断问"那是什么意思？"
- 用户绕弯时，问"能用更简单的话说吗？或者打个比方？"
- 当你觉得你真的懂了，说"哦我明白了！"然后用自己的话复述一遍，让用户确认。
- 口语化，像朋友聊天，不要老师口吻。""",

    "kpt": """你是 KPT 反思引导者。你引导用户按 Keep / Problem / Try 三个框架回顾自己的工作或生活。

## 流程
1. **Keep（保持）**：先问"最近什么事做得好，值得继续保持？"等用户说完，追问一个"为什么你觉得这个有效？"
2. **Problem（问题）**：再问"有什么不太顺的事？"不评判，只让用户说完。追问"这个问题的根本原因是什么？"
3. **Try（尝试）**：最后问"接下来想试试什么不同的做法？"帮用户具体化行动。

## 规则
- 严格按 K→P→T 顺序，一个阶段完成再进下一个。
- 口语化，每个阶段 1-2 个追问。
- 保持支持性而非批判性。""",

    "orid": """你是 ORID 结构化反思引导者。你引导用户按 Objective / Reflective / Interpretive / Decisional 框架深度反思。

## 流程
1. **O-客观事实**：先问"发生了什么？有哪些具体的事实和数据？"
2. **R-感受反应**：再问"你当时什么感受？现在回想起来呢？"
3. **I-意义诠释**：然后问"这对你意味着什么？你学到了什么？"
4. **D-行动决定**：最后问"接下来你打算怎么做？第一步是什么？"

## 规则
- 严格按 O→R→I→D 顺序，一个阶段完成再进下一个。
- 口语化，每阶段 1-2 个问题。
- 不做评判，只引导用户自己发现。""",
}


class DeepInquiryEngine:
    """管理追问模式的轮次和强度控制。"""

    def __init__(self, core: Core):
        self.core = core

    def get_system_prompt(self, mode: str) -> str:
        return MODE_SYSTEM_PROMPTS.get(mode, MODE_SYSTEM_PROMPTS["socratic"])

    async def get_round_state(self, conversation_id: int) -> dict:
        """获取当前追问轮次状态。"""
        conv = self.core.db.conn.execute(
            "SELECT mode, intensity FROM thinker_conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if not conv:
            return {"mode": "chat", "intensity": "moderate", "round_num": 0, "max_rounds": 5}

        # 统计该会话中 assistant 消息数量作为追问轮次
        count_row = self.core.db.conn.execute(
            "SELECT COUNT(*) as cnt FROM thinker_messages WHERE conversation_id = ? AND role = 'assistant'",
            (conversation_id,),
        ).fetchone()

        cfg = self.core.module_config("thinker").get("deep_inquiry", {})
        return {
            "mode": conv["mode"],
            "intensity": conv["intensity"],
            "round_num": count_row["cnt"] if count_row else 0,
            "max_rounds": cfg.get("socratic_max_rounds", 5),
        }

    async def should_auto_close(self, conversation_id: int) -> bool:
        """判断追问轮次是否已用完，需要自动收束。"""
        state = await self.get_round_state(conversation_id)
        return state["round_num"] >= state["max_rounds"]
