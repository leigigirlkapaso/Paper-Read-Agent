"""
modules/thinker/profile.py
ProfileManager — 用户画像提取、合并、查询。
"""

from __future__ import annotations

import json
import logging

from paperreadagent.core import Core

logger = logging.getLogger(__name__)

_MERGE_ALPHA = 0.3  # 新信息权重
_CONFIDENCE_INCREMENT = 0.2
_CONFIDENCE_MAX = 0.95


class ProfileManager:
    """管理 thinker 用户画像。指数滑动平均合并，避免单次对话过度影响。"""

    def __init__(self, core: Core):
        self.core = core

    def get_profile(self) -> dict:
        """获取当前用户画像，无记录时返回空结构。"""
        row = self.core.db.conn.execute(
            "SELECT * FROM thinker_user_profile WHERE id = 1"
        ).fetchone()
        if not row:
            return {
                "research_domains": [],
                "knowledge_level": {},
                "thinking_style": "",
                "long_term_goals": [],
                "interaction_prefs": {},
                "confidence_scores": {},
            }
        r = dict(row)
        for field in ("research_domains", "knowledge_level", "long_term_goals",
                       "interaction_prefs", "confidence_scores"):
            raw = r.get(field, "{}")
            try:
                r[field] = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                r[field] = {} if field in ("knowledge_level", "interaction_prefs", "confidence_scores") else []
        return r

    def _save_profile(self, profile: dict) -> None:
        """写入数据库。列表和字典字段 JSON 序列化。"""
        self.core.db.conn.execute(
            """INSERT OR REPLACE INTO thinker_user_profile
               (id, research_domains, knowledge_level, thinking_style,
                long_term_goals, interaction_prefs, confidence_scores, last_updated)
               VALUES (1, ?, ?, ?, ?, ?, ?, datetime('now'))""",
            (
                json.dumps(profile.get("research_domains", []), ensure_ascii=False),
                json.dumps(profile.get("knowledge_level", {}), ensure_ascii=False),
                profile.get("thinking_style", ""),
                json.dumps(profile.get("long_term_goals", []), ensure_ascii=False),
                json.dumps(profile.get("interaction_prefs", {}), ensure_ascii=False),
                json.dumps(profile.get("confidence_scores", {}), ensure_ascii=False),
            ),
        )
        self.core.db.conn.commit()

    def merge(self, current: dict, updates: dict) -> dict:
        """指数滑动平均合并 — 不修改传入的 current。"""
        import copy
        merged = copy.deepcopy(current)
        conf = merged.setdefault("confidence_scores", {})

        # 列表字段：追加去重
        for list_field in ("research_domains", "long_term_goals"):
            new_items = updates.get(list_field, [])
            if new_items:
                existing = set(merged.get(list_field, []))
                for item in new_items:
                    if item not in existing:
                        existing.add(item)
                        merged[list_field].append(item)
                conf[list_field] = min(
                    conf.get(list_field, 0.0) + _CONFIDENCE_INCREMENT,
                    _CONFIDENCE_MAX,
                )

        # 字典字段：指数滑动
        for dict_field in ("knowledge_level", "interaction_prefs"):
            new_dict = updates.get(dict_field, {})
            if new_dict:
                old_dict = merged.get(dict_field, {})
                for k, v in new_dict.items():
                    if k in old_dict and old_dict[k] == v:
                        conf_key = f"{dict_field}.{k}"
                        conf[conf_key] = min(
                            conf.get(conf_key, 0.0) + _CONFIDENCE_INCREMENT,
                            _CONFIDENCE_MAX,
                        )
                    else:
                        old_dict[k] = v
                        conf_key = f"{dict_field}.{k}"
                        conf[conf_key] = _MERGE_ALPHA
                merged[dict_field] = old_dict

        # 标量字段：新值非空则更新
        for scalar_field in ("thinking_style",):
            new_val = updates.get(scalar_field, "")
            if new_val:
                old_val = merged.get(scalar_field, "")
                if old_val == new_val:
                    conf[scalar_field] = min(
                        conf.get(scalar_field, 0.0) + _CONFIDENCE_INCREMENT,
                        _CONFIDENCE_MAX,
                    )
                else:
                    merged[scalar_field] = new_val
                    conf[scalar_field] = _MERGE_ALPHA

        merged["confidence_scores"] = conf
        return merged

    async def extract_and_merge(
        self, conversation_id: int, messages: list[dict]
    ) -> dict:
        """从对话摘要中提取画像更新并合并。"""
        current = self.get_profile()
        summary = self._build_conversation_text(messages)

        prompt = self.core.llm.load_prompt(
            "thinker", "profile_extract",
            current_profile=json.dumps(current, ensure_ascii=False, indent=2),
            conversation_summary=summary,
        )

        try:
            raw, _ = await self.core.llm.achat(
                user_prompt=prompt, module="thinker", purpose="profile_extract",
            )
            updates = json.loads(raw)
            if not isinstance(updates, dict):
                return current
        except Exception:
            logger.warning("[ProfileManager] 画像提取失败", exc_info=True)
            return current

        merged = self.merge(current, updates)
        self._save_profile(merged)
        return merged

    @staticmethod
    def _build_conversation_text(messages: list[dict]) -> str:
        if not messages:
            return ""
        lines = []
        for m in messages[-30:]:
            role = m.get("role", m.get("speaker", ""))
            content = m.get("content", "")
            if isinstance(content, str) and len(content) > 300:
                content = content[:1000]
            lines.append(f"[{role}] {content}")
        return "\n".join(lines)
