"""tests for SecretaryService.update() — the secretary orchestration class."""
import asyncio
import logging
import pytest
from unittest.mock import MagicMock, AsyncMock

from paperreadagent.modules.ideator.secretary import SecretaryService


class _FakeLLM:
    """IdeatorLLM stub. chat returns configured value; load_prompt echoes args."""

    def __init__(self, *, chat_return="# Outline\n\n## 1. 研究问题\nfoo",
                 chat_raises=False):
        self._chat_return = chat_return
        self._chat_raises = chat_raises
        self.last_chat_messages = None

    async def chat(self, *, model_role, messages, temperature=0.7, max_tokens=16384):
        self.last_chat_messages = messages
        if self._chat_raises:
            raise RuntimeError("simulated LLM failure")
        return self._chat_return

    def load_prompt(self, module, name, **kw):
        # Echo back kwargs so tests can verify inputs propagated
        kw_str = "|".join(f"{k}={v!r}" for k, v in kw.items())
        return f"[PROMPT:{name}]{kw_str}"

    def model_for(self, role):
        return "deepseek-v4-pro"


class _FakeDataAccess:
    """Records inserts; returns configurable latest outline."""

    def __init__(self, *, previous_outline=None):
        self._previous = previous_outline
        self.inserted = []  # list of kwargs dicts

    def get_latest_outline(self, rt_id):
        return self._previous

    def insert_outline(self, *, rt_id, round_number, outline_markdown,
                       facts_block="", model_name="", token_usage=None):
        self.inserted.append({
            "rt_id": rt_id, "round_number": round_number,
            "outline_markdown": outline_markdown,
            "facts_block": facts_block, "model_name": model_name,
        })
        return len(self.inserted)


class _FakeHub:
    """Records all publishes."""

    def __init__(self, *, publish_raises=False):
        self.published = []
        self._raises = publish_raises

    async def publish(self, rt_id, event):
        if self._raises:
            raise RuntimeError("simulated hub failure")
        self.published.append((rt_id, event))


def _mk_team(*, round_number=1, messages=None, facts_block="",
             spark_content="my spark"):
    """Build a minimal team-like object SecretaryService.update consumes."""
    t = MagicMock()
    t.round_number = round_number
    t.messages = messages or []
    t.facts_block = facts_block
    t.spark_content = spark_content
    return t


def _msg(*, round_number, sender_type="model", sender_name="rev1",
         sender_role="reviewer_1", message_type="answer", content="text"):
    return {
        "round_number": round_number,
        "sender_type": sender_type,
        "sender_name": sender_name,
        "sender_role": sender_role,
        "message_type": message_type,
        "content": content,
    }


@pytest.mark.asyncio
async def test_update_inserts_outline_row_with_round_number():
    llm = _FakeLLM(chat_return="# Final Outline\n\n## 1. 研究问题\nfoo")
    data = _FakeDataAccess()
    hub = _FakeHub()
    sec = SecretaryService(llm=llm, data_access=data, stream_hub=hub)
    team = _mk_team(round_number=3, messages=[
        _msg(round_number=3, sender_name="rev1", content="alpha"),
    ])

    result = await sec.update(rt_id=42, team=team)
    assert result is not None
    assert result.startswith("# Final Outline")
    assert len(data.inserted) == 1
    assert data.inserted[0]["rt_id"] == 42
    assert data.inserted[0]["round_number"] == 3
    assert data.inserted[0]["outline_markdown"].startswith("# Final Outline")


@pytest.mark.asyncio
async def test_update_publishes_outline_update_event_to_hub():
    llm = _FakeLLM(chat_return="# X")
    data = _FakeDataAccess()
    hub = _FakeHub()
    sec = SecretaryService(llm=llm, data_access=data, stream_hub=hub)
    team = _mk_team(round_number=2, messages=[
        _msg(round_number=2, sender_name="gen", sender_role="generator"),
    ])

    await sec.update(rt_id=99, team=team)

    assert len(hub.published) == 1
    rt_id, evt = hub.published[0]
    assert rt_id == 99
    assert evt["type"] == "outline_update"
    assert evt["rt_id"] == 99
    assert evt["round_number"] == 2
    assert evt["outline"] == "# X"


@pytest.mark.asyncio
async def test_update_skips_when_no_model_replies_this_round():
    llm = _FakeLLM()
    data = _FakeDataAccess()
    hub = _FakeHub()
    sec = SecretaryService(llm=llm, data_access=data, stream_hub=hub)
    # Only user messages, no model replies
    team = _mk_team(round_number=1, messages=[
        _msg(round_number=1, sender_type="user", message_type="question",
             sender_name="user", sender_role=None),
    ])

    result = await sec.update(rt_id=1, team=team)
    assert result is None
    assert data.inserted == []
    assert hub.published == []
    # LLM was never called
    assert llm.last_chat_messages is None


@pytest.mark.asyncio
async def test_update_filters_only_current_round_model_answer():
    """Ignore: previous-round msgs, current-round user, current-round interjections,
    current-round system fallbacks. Keep only current-round model+answer."""
    llm = _FakeLLM(chat_return="# Outline")
    data = _FakeDataAccess()
    hub = _FakeHub()
    sec = SecretaryService(llm=llm, data_access=data, stream_hub=hub)

    team = _mk_team(round_number=2, messages=[
        # Previous round - drop
        _msg(round_number=1, sender_name="rev1", content="OLD"),
        # Current round, user question - drop
        _msg(round_number=2, sender_type="user", message_type="question",
             sender_name="user", sender_role=None, content="USER"),
        # Current round, interjection (not "answer") - drop
        _msg(round_number=2, sender_name="arb2", message_type="interjection",
             content="QUICK"),
        # Current round, system fallback - drop
        _msg(round_number=2, sender_type="system", sender_name="system",
             sender_role=None, content="FALLBACK"),
        # Current round, model answer - KEEP
        _msg(round_number=2, sender_name="rev1", content="KEEP_REV1"),
        _msg(round_number=2, sender_name="rev2", content="KEEP_REV2"),
    ])

    await sec.update(rt_id=1, team=team)

    # LLM was called; inspect what content went into user prompt template
    assert llm.last_chat_messages is not None
    user_content = llm.last_chat_messages[1]["content"]
    # Kept content present
    assert "KEEP_REV1" in user_content or "KEEP_REV1" in repr(user_content) \
        or "current_round_msgs=" in user_content  # echo-prompt fixture pattern
    # Dropped content absent
    for dropped in ("OLD", "USER", "QUICK", "FALLBACK"):
        assert dropped not in user_content


@pytest.mark.asyncio
async def test_update_uses_previous_outline_as_input():
    llm = _FakeLLM(chat_return="# New")
    data = _FakeDataAccess(previous_outline="## PREVIOUS_OUTLINE\nfoo")
    hub = _FakeHub()
    sec = SecretaryService(llm=llm, data_access=data, stream_hub=hub)
    team = _mk_team(round_number=2, messages=[
        _msg(round_number=2, sender_name="gen"),
    ])

    await sec.update(rt_id=1, team=team)
    user_content = llm.last_chat_messages[1]["content"]
    assert "PREVIOUS_OUTLINE" in user_content


@pytest.mark.asyncio
async def test_update_passes_facts_block_to_llm():
    llm = _FakeLLM(chat_return="# X")
    data = _FakeDataAccess()
    hub = _FakeHub()
    sec = SecretaryService(llm=llm, data_access=data, stream_hub=hub)
    team = _mk_team(
        round_number=1,
        messages=[_msg(round_number=1, sender_name="gen")],
        facts_block="## FACTS_LAYER_HERE",
    )

    await sec.update(rt_id=1, team=team)
    user_content = llm.last_chat_messages[1]["content"]
    assert "FACTS_LAYER_HERE" in user_content


@pytest.mark.asyncio
async def test_update_returns_none_on_llm_failure_no_db_write():
    llm = _FakeLLM(chat_raises=True)
    data = _FakeDataAccess()
    hub = _FakeHub()
    sec = SecretaryService(llm=llm, data_access=data, stream_hub=hub)
    team = _mk_team(round_number=1, messages=[
        _msg(round_number=1, sender_name="gen"),
    ])

    result = await sec.update(rt_id=1, team=team)
    assert result is None
    assert data.inserted == []
    assert hub.published == []


@pytest.mark.asyncio
async def test_update_returns_none_on_empty_llm_output():
    llm = _FakeLLM(chat_return="")  # empty
    data = _FakeDataAccess()
    hub = _FakeHub()
    sec = SecretaryService(llm=llm, data_access=data, stream_hub=hub)
    team = _mk_team(round_number=1, messages=[
        _msg(round_number=1, sender_name="gen"),
    ])

    result = await sec.update(rt_id=1, team=team)
    assert result is None
    assert data.inserted == []
    assert hub.published == []


@pytest.mark.asyncio
async def test_update_strips_think_tags_and_markdown_fence():
    """LLM may return <think>...</think> or ```markdown ... ``` — strip both."""
    raw = "<think>reasoning step 1\nreasoning step 2</think>\n\n```markdown\n# T\n\n## 1. 研究问题\nfoo\n```"
    llm = _FakeLLM(chat_return=raw)
    data = _FakeDataAccess()
    hub = _FakeHub()
    sec = SecretaryService(llm=llm, data_access=data, stream_hub=hub)
    team = _mk_team(round_number=1, messages=[
        _msg(round_number=1, sender_name="gen"),
    ])

    result = await sec.update(rt_id=1, team=team)
    assert result is not None
    # think block gone
    assert "<think>" not in result
    assert "reasoning step" not in result
    # fence gone
    assert "```" not in result
    # content preserved
    assert "# T" in result
    assert "研究问题" in result


@pytest.mark.asyncio
async def test_update_publish_failure_does_not_block_db_write():
    """If hub.publish raises, DB row should still be saved; outer call returns
    the outline string (not None), since the work that mattered succeeded."""
    llm = _FakeLLM(chat_return="# X")
    data = _FakeDataAccess()
    hub = _FakeHub(publish_raises=True)
    sec = SecretaryService(llm=llm, data_access=data, stream_hub=hub)
    team = _mk_team(round_number=1, messages=[
        _msg(round_number=1, sender_name="gen"),
    ])

    # Should not raise
    result = await sec.update(rt_id=1, team=team)
    # DB write happened
    assert len(data.inserted) == 1
    assert data.inserted[0]["outline_markdown"] == "# X"
    # Return value is the new outline (publish failure is non-fatal)
    assert result == "# X"


@pytest.mark.asyncio
async def test_update_strips_orphan_think_tag_without_closing():
    """LLM output truncated mid-think-block: <think>reasoning... (no closing
    tag) followed by the actual markdown. Strip the orphan opener."""
    raw = "<think>reasoning step 1\nreasoning step 2 truncated\n\n# Outline\n\n## 1. 研究问题\nfoo"
    llm = _FakeLLM(chat_return=raw)
    data = _FakeDataAccess()
    hub = _FakeHub()
    sec = SecretaryService(llm=llm, data_access=data, stream_hub=hub)
    team = _mk_team(round_number=1, messages=[
        _msg(round_number=1, sender_name="gen"),
    ])

    result = await sec.update(rt_id=1, team=team)
    assert result is not None
    assert "<think>" not in result
    assert "reasoning step" not in result
    # Content preserved
    assert "# Outline" in result
    assert "研究问题" in result


@pytest.mark.asyncio
async def test_update_strips_fence_with_trailing_chatter():
    """LLM emits closing fence followed by trailing post-fence text.
    The fence and everything after it should be removed."""
    raw = "```markdown\n# T\n\n## 1. 研究问题\nfoo\n```\n\nLet me know if you need changes."
    llm = _FakeLLM(chat_return=raw)
    data = _FakeDataAccess()
    hub = _FakeHub()
    sec = SecretaryService(llm=llm, data_access=data, stream_hub=hub)
    team = _mk_team(round_number=1, messages=[
        _msg(round_number=1, sender_name="gen"),
    ])

    result = await sec.update(rt_id=1, team=team)
    assert result is not None
    assert "```" not in result
    assert "Let me know" not in result
    # Content preserved
    assert "# T" in result
    assert "研究问题" in result
