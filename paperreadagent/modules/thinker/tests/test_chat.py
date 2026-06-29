"""
modules/thinker/tests/test_chat.py
测试 ChatEngine 对话引擎核心功能。
"""

import pytest
from paperreadagent.utils.json_utils import extract_json_list
from paperreadagent.modules.thinker.chat import ChatEngine


class TestParseResolutionJSON:
    def test_parse_list(self):
        result = extract_json_list('["明天跑步", "读一本书"]')
        assert result == ["明天跑步", "读一本书"]

    def test_parse_single(self):
        result = extract_json_list('["我决定每天早起"]')
        assert result == ["我决定每天早起"]

    def test_parse_empty(self):
        result = extract_json_list("[]")
        assert result == []

    def test_parse_invalid_json_extracts_manually(self):
        result = extract_json_list("前言 blabla\n- 承诺1\n- 承诺2\n后面的话")
        assert len(result) >= 2

    def test_parse_bare_text(self):
        result = extract_json_list("没有任何 JSON 格式的普通文本")
        assert len(result) >= 1

    def test_parse_json_with_surrounding_text(self):
        result = extract_json_list(
            '好的，我提取了以下承诺：["每周锻炼三次", "早睡早起"]，共两条。'
        )
        assert "每周锻炼三次" in result
        assert "早睡早起" in result


class TestChatEngine:
    @pytest.mark.asyncio
    async def test_create_conversation(self, thinker_core):
        engine = ChatEngine(thinker_core)
        conv_id = await engine.create_conversation()
        assert conv_id > 0

        conv = await engine.get_conversation(conv_id)
        assert conv["mode"] == "chat"
        assert conv["status"] == "active"

    @pytest.mark.asyncio
    async def test_list_conversations(self, thinker_core):
        engine = ChatEngine(thinker_core)
        await engine.create_conversation()
        convs = await engine.list_conversations()
        assert len(convs) >= 1

    @pytest.mark.asyncio
    async def test_update_mode(self, thinker_core):
        engine = ChatEngine(thinker_core)
        conv_id = await engine.create_conversation()
        await engine.update_mode(conv_id, "socratic")

        conv = await engine.get_conversation(conv_id)
        assert conv["mode"] == "socratic"

    @pytest.mark.asyncio
    async def test_pause_and_resume(self, thinker_core):
        engine = ChatEngine(thinker_core)
        conv_id = await engine.create_conversation()
        await engine.pause(conv_id, 30)

        conv = await engine.get_conversation(conv_id)
        assert conv["status"] == "paused"
        assert conv["snooze_until"] is not None

        await engine.resume(conv_id)
        conv = await engine.get_conversation(conv_id)
        assert conv["status"] == "active"
        assert conv["snooze_until"] is None

    @pytest.mark.asyncio
    async def test_close_conversation(self, thinker_core):
        engine = ChatEngine(thinker_core)
        conv_id = await engine.create_conversation()

        thinker_core.db.conn.execute(
            "INSERT INTO thinker_messages (conversation_id, role, content) VALUES (?, ?, ?)",
            (conv_id, "user", "今天聊聊压力管理"),
        )
        thinker_core.db.conn.execute(
            "INSERT INTO thinker_messages (conversation_id, role, content) VALUES (?, ?, ?)",
            (conv_id, "assistant", "好啊，最近压力大吗？"),
        )
        thinker_core.db.conn.commit()

        await engine.close_conversation(conv_id)
        conv = await engine.get_conversation(conv_id)
        assert conv["status"] == "closed"
