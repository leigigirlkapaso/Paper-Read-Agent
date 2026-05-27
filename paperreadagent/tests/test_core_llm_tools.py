"""测试 CoreLLM.chat_with_tools() / achat_with_tools()"""
import json
from unittest.mock import MagicMock


class FakeToolCall:
    def __init__(self, id, function_name, arguments_dict):
        self.id = id
        self.type = "function"
        self.function = MagicMock()
        self.function.name = function_name
        self.function.arguments = json.dumps(arguments_dict)


class FakeChoice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeUsage:
    prompt_tokens = 100
    completion_tokens = 50
    total_tokens = 150


class FakeResponse:
    def __init__(self, choices, usage=None):
        self.choices = choices
        self.usage = usage or FakeUsage()


def make_core_llm():
    from paperreadagent.core.llm import CoreLLM
    return CoreLLM(
        api_key="sk-test",
        api_base_url="https://test.api/v1",
        model_name="deepseek-v4-pro",
        temperature=0.3,
        db=None,
    )


def test_chat_with_tools_returns_content_when_no_tool_calls():
    """LLM 返回纯文本时，content 有值，tool_calls 为 None。"""
    llm = make_core_llm()
    fake_msg = FakeMessage(content='{"result": "ok"}')
    fake_choice = FakeChoice(message=fake_msg, finish_reason="stop")
    fake_resp = FakeResponse(choices=[fake_choice])

    llm._sync_client.chat.completions.create = MagicMock(return_value=fake_resp)

    result = llm.chat_with_tools(
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"type": "function", "function": {"name": "search", "parameters": {}}}],
    )

    assert result["content"] == '{"result": "ok"}'
    assert result["tool_calls"] is None
    assert result["finish_reason"] == "stop"
    assert result["usage"]["total_tokens"] == 150


def test_chat_with_tools_returns_tool_calls():
    """LLM 请求工具调用时，tool_calls 有值，content 为 None。"""
    llm = make_core_llm()
    tc = FakeToolCall("call_1", "read_paper", {"arxiv_id": "2301.00001"})
    fake_msg = FakeMessage(content=None, tool_calls=[tc])
    fake_choice = FakeChoice(message=fake_msg, finish_reason="tool_calls")
    fake_resp = FakeResponse(choices=[fake_choice])

    llm._sync_client.chat.completions.create = MagicMock(return_value=fake_resp)

    result = llm.chat_with_tools(
        messages=[{"role": "user", "content": "read paper 2301.00001"}],
        tools=[{"type": "function", "function": {"name": "read_paper", "parameters": {}}}],
    )

    assert result["content"] is None
    assert result["tool_calls"] is not None
    assert len(result["tool_calls"]) == 1
    assert result["tool_calls"][0]["name"] == "read_paper"
    assert result["tool_calls"][0]["arguments"] == {"arxiv_id": "2301.00001"}
    assert result["finish_reason"] == "tool_calls"


def test_chat_with_tools_handles_empty_choices():
    """API 返回空 choices 时不抛异常，返回空 content。"""
    llm = make_core_llm()
    fake_resp = FakeResponse(choices=[])
    llm._sync_client.chat.completions.create = MagicMock(return_value=fake_resp)

    result = llm.chat_with_tools(
        messages=[{"role": "user", "content": "test"}],
        tools=[],
    )

    assert result["content"] == ""
    assert result["tool_calls"] is None


def test_chat_with_tools_passes_tool_choice_to_api():
    """验证 tool_choice 参数正确传到 API。"""
    llm = make_core_llm()
    fake_msg = FakeMessage(content="ok")
    fake_choice = FakeChoice(message=fake_msg)
    fake_resp = FakeResponse(choices=[fake_choice])

    mock_create = MagicMock(return_value=fake_resp)
    llm._sync_client.chat.completions.create = mock_create

    llm.chat_with_tools(
        messages=[{"role": "user", "content": "test"}],
        tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
        tool_choice="required",
        temperature=0.5,
        max_tokens=4096,
    )

    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["tool_choice"] == "required"
    assert call_kwargs["temperature"] == 0.5
    assert call_kwargs["max_tokens"] == 4096
    assert "tools" in call_kwargs
