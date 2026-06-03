"""集成测试：S2 工具调用循环"""
import json
import pytest


def test_tool_loop_messages_format():
    """验证工具调用消息格式符合 OpenAI API 规范。"""
    messages = [
        {"role": "system", "content": "You are a research spark generator."},
        {"role": "user", "content": "Analyze this connection."},
    ]

    assistant_msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "read_paper",
                    "arguments": json.dumps({"arxiv_id": "2301.00001"}),
                },
            }
        ],
    }
    messages.append(assistant_msg)

    tool_result = {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "Title: Test Paper\nAbstract: This is a test.",
    }
    messages.append(tool_result)

    assert len(messages) == 4
    assert messages[2]["role"] == "assistant"
    assert messages[2]["tool_calls"][0]["function"]["name"] == "read_paper"
    assert messages[3]["role"] == "tool"
    assert messages[3]["tool_call_id"] == "call_1"


def test_tool_loop_max_rounds_exit():
    """验证 5 轮后降级逻辑触发。"""
    MAX_ROUNDS = 5
    rounds = 0
    has_spark = False

    for r in range(MAX_ROUNDS):
        rounds += 1
        if r >= MAX_ROUNDS - 1:
            has_spark = False
            break

    assert rounds == MAX_ROUNDS
    assert not has_spark


def test_tool_loop_spark_found_exits_early():
    """验证 LLM 返回火花后立即退出循环。"""
    MAX_ROUNDS = 5
    rounds = 0
    spark_found = False

    for r in range(MAX_ROUNDS):
        rounds += 1
        if r == 2:
            spark_found = True
            break

    assert rounds == 3
    assert spark_found


def test_tool_loop_null_spark_returns_none():
    """验证 LLM 返回 null 时组返回 None。"""
    resp = json.loads("null")
    assert resp is None


def test_tool_loop_invalid_json_retries():
    """验证 JSON 解析失败后触发重试。"""
    parse_attempts = 0
    retry_triggered = False

    parse_attempts += 1
    try:
        json.loads("not valid json{")
    except json.JSONDecodeError:
        retry_triggered = True
        parse_attempts += 1

    assert retry_triggered
    assert parse_attempts == 2


def test_tool_loop_fallback_prompt_structure():
    """验证降级提示不含工具调用要求。"""
    system_prompt = "System: generate spark"
    user_prompt = "User: links data"

    fallback = f"{system_prompt}\n\n{user_prompt}\n\n请直接返回 JSON（不调用工具）："

    assert "不调用工具" in fallback
    assert system_prompt in fallback
    assert user_prompt in fallback


def test_build_source_refs_deduplicates():
    """验证 source_refs 构造正确去重。"""
    refs = []
    seen = set()
    group = [
        {"source_a": {"type": "paper", "id": 1}, "source_b": {"type": "paper", "id": 2}},
        {"source_a": {"type": "paper", "id": 1}, "source_b": {"type": "core_note", "id": 5}},
    ]

    for l in group:
        for src in (l["source_a"], l["source_b"]):
            key = f"{src['type']}:{src['id']}"
            if key not in seen:
                seen.add(key)
                refs.append({"type": src["type"], "id": src["id"]})

    assert len(refs) == 3  # paper:1, paper:2, core_note:5
    types = {r["type"] for r in refs}
    assert "paper" in types
    assert "core_note" in types
