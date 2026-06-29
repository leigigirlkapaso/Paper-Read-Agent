"""
modules/thinker/tests/test_integration.py
Thinker 模块全链路集成测试。
"""

import pytest


@pytest.mark.asyncio
async def test_full_module_registration(thinker_core):
    """验证模块注册：表创建 + 模块出现在 core 注册表。"""
    assert thinker_core.get_module("thinker") is not None
    info = thinker_core.get_module("thinker")
    assert info.name == "thinker"
    assert info.version == "0.2.0"
    assert info.schema_version == 3

    # 验证七张表存在
    tables = thinker_core.db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'thinker_%'"
    ).fetchall()
    table_names = [r[0] for r in tables]
    assert "thinker_conversations" in table_names
    assert "thinker_messages" in table_names
    assert "thinker_resolutions" in table_names
    assert "thinker_pending_questions" in table_names
    assert "thinker_memory_index" in table_names
    assert "thinker_user_profile" in table_names
    assert "thinker_rehearsals" in table_names

    # 验证不注册全局浮动组件（v0.2.0 改为独立页面）
    comp_names = [c.name for c in thinker_core.frontend._components]
    assert "thinker-panel" not in comp_names


@pytest.mark.asyncio
async def test_chat_to_close_lifecycle(thinker_core):
    """对话生命周期：创建 → 发消息 → 流式回复 → 关闭。"""
    from paperreadagent.modules.thinker.chat import ChatEngine

    engine = ChatEngine(thinker_core)

    # 1. 创建会话
    conv_id = await engine.create_conversation(mode="chat")
    assert conv_id > 0

    # 2. 发用户消息（不实际调 LLM，只验证消息存储）
    thinker_core.db.conn.execute(
        "INSERT INTO thinker_messages (conversation_id, role, content) VALUES (?, 'user', ?)",
        (conv_id, "今天想聊聊习惯养成"),
    )
    thinker_core.db.conn.execute(
        "INSERT INTO thinker_messages (conversation_id, role, content) VALUES (?, 'assistant', ?)",
        (conv_id, "好啊，你对什么习惯感兴趣？"),
    )
    thinker_core.db.conn.commit()

    # 3. 验证消息存在
    messages = await engine.get_messages(conv_id)
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert "习惯养成" in messages[0]["content"]

    # 4. 暂停再恢复
    await engine.pause(conv_id, 30)
    conv = await engine.get_conversation(conv_id)
    assert conv["status"] == "paused"

    await engine.resume(conv_id)
    conv = await engine.get_conversation(conv_id)
    assert conv["status"] == "active"

    # 5. 关闭
    await engine.close_conversation(conv_id)
    conv = await engine.get_conversation(conv_id)
    assert conv["status"] == "closed"


@pytest.mark.asyncio
async def test_mode_switching(thinker_core):
    """验证模式切换。"""
    from paperreadagent.modules.thinker.chat import ChatEngine
    from paperreadagent.modules.thinker.deep_inquiry import DeepInquiryEngine

    engine = ChatEngine(thinker_core)
    inquiry = DeepInquiryEngine(thinker_core)

    conv_id = await engine.create_conversation(mode="chat")

    for mode in ["socratic", "feynman", "kpt", "orid"]:
        await engine.update_mode(conv_id, mode)
        conv = await engine.get_conversation(conv_id)
        assert conv["mode"] == mode

        prompt = inquiry.get_system_prompt(mode)
        assert prompt and len(prompt) > 0

    # 回到聊天模式
    await engine.update_mode(conv_id, "chat")
    conv = await engine.get_conversation(conv_id)
    assert conv["mode"] == "chat"


@pytest.mark.asyncio
async def test_resolution_tracking(thinker_core):
    """验证承诺追踪流程。"""
    from paperreadagent.modules.thinker.chat import ChatEngine
    from paperreadagent.modules.thinker.resolutions import ResolutionTracker

    engine = ChatEngine(thinker_core)
    tracker = ResolutionTracker(thinker_core)

    conv_id = await engine.create_conversation()

    # 插入模拟消息
    thinker_core.db.conn.execute(
        "INSERT INTO thinker_messages (conversation_id, role, content) VALUES (?, 'user', ?)",
        (conv_id, "我决定每天早起跑步"),
    )
    thinker_core.db.conn.commit()

    # 模拟提取承诺
    thinker_core.db.conn.execute(
        "INSERT INTO thinker_resolutions (conversation_id, content, status) VALUES (?, '每天早起跑步', 'pending')",
        (conv_id,),
    )
    thinker_core.db.conn.commit()

    # 标记完成
    res_row = thinker_core.db.conn.execute(
        "SELECT id FROM thinker_resolutions WHERE conversation_id = ? LIMIT 1", (conv_id,)
    ).fetchone()
    await tracker.mark_fulfilled(res_row["id"])

    res = thinker_core.db.conn.execute(
        "SELECT status FROM thinker_resolutions WHERE id = ?", (res_row["id"],)
    ).fetchone()
    assert res["status"] == "fulfilled"


@pytest.mark.asyncio
async def test_pending_questions(thinker_core):
    """验证主动提问生成和投递。"""
    from paperreadagent.modules.thinker.chat import ChatEngine
    from paperreadagent.modules.thinker.questions import QuestionGenerator

    engine = ChatEngine(thinker_core)
    gen = QuestionGenerator(thinker_core)

    conv_id = await engine.create_conversation()

    # 无消息时，pending 为空
    q = await gen.get_pending_question(conv_id)
    assert q is None

    # 手动插入一条问题
    thinker_core.db.conn.execute(
        "INSERT INTO thinker_pending_questions (conversation_id, question, question_type) VALUES (?, '今天想聊什么？', 'inactivity')",
        (conv_id,),
    )
    thinker_core.db.conn.commit()

    # 再次获取
    q = await gen.get_pending_question(conv_id)
    assert q is not None
    assert "今天想聊什么" in q["question"]

    # 忽略
    await gen.dismiss_question(q["id"])

    # 确认已忽略
    row = thinker_core.db.conn.execute(
        "SELECT dismissed FROM thinker_pending_questions WHERE id = ?", (q["id"],)
    ).fetchone()
    assert row["dismissed"] == 1


@pytest.mark.asyncio
async def test_knowledge_linker(thinker_core):
    """验证知识关联（不实际调用 embedding API）。"""
    from paperreadagent.modules.thinker.knowledge_linker import KnowledgeLinker
    from paperreadagent.modules.thinker.chat import ChatEngine
    from paperreadagent.core.embedding import pack_embedding, unpack_embedding

    engine = ChatEngine(thinker_core)
    conv_id = await engine.create_conversation()

    linker = KnowledgeLinker(thinker_core)

    # 插入一条带 embedding 的消息
    thinker_core.db.conn.execute(
        "INSERT INTO thinker_messages (conversation_id, role, content, embedding) VALUES (?, 'user', ?, ?)",
        (conv_id, "测试消息", pack_embedding([0.1] * 256)),
    )
    thinker_core.db.conn.commit()

    msg_id = thinker_core.db.conn.execute(
        "SELECT id FROM thinker_messages WHERE content = '测试消息'"
    ).fetchone()["id"]

    # 验证 embedding 可解包
    row = thinker_core.db.conn.execute(
        "SELECT embedding FROM thinker_messages WHERE id = ?", (msg_id,)
    ).fetchone()
    emb = unpack_embedding(row["embedding"])
    assert len(emb) == 256


@pytest.mark.asyncio
async def test_event_bus_integration(thinker_core):
    """验证事件总线集成。"""
    received = []

    async def _test_handler(event, **data):
        received.append({"event": event, "data": data})

    thinker_core.event_bus.subscribe("test", "thinker:*", _test_handler)
    await thinker_core.event_bus.emit("thinker:message:sent", conversation_id=1, role="user")
    await thinker_core.event_bus.emit("thinker:summary:generated", conversation_id=1, note_id=42)

    assert len(received) == 2
    assert received[0]["event"] == "thinker:message:sent"
    assert received[1]["data"]["note_id"] == 42
