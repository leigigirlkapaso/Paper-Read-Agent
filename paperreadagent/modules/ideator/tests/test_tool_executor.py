"""测试 ToolExecutor — 工具定义→实际执行"""
import pytest
from paperreadagent.modules.ideator.tool_executor import ToolExecutor


def test_to_openai_tools_format():
    """to_openai_tools() 返回正确的 OpenAI tools 格式。"""
    from paperreadagent.modules.ideator.tool_registry import create_default_registry
    registry = create_default_registry()
    executor = ToolExecutor(
        data_access=None, core_llm=None, tool_registry=registry,
    )
    tools = executor.to_openai_tools()

    assert isinstance(tools, list)
    assert len(tools) >= 5
    for tool in tools:
        assert "type" in tool
        assert tool["type"] == "function"
        assert "function" in tool
        assert "name" in tool["function"]
        assert "description" in tool["function"]
        assert "parameters" in tool["function"]


def test_to_openai_tools_includes_s2_tools():
    """S2 可用的 5 个工具都在列表中。"""
    from paperreadagent.modules.ideator.tool_registry import create_default_registry
    registry = create_default_registry()
    executor = ToolExecutor(
        data_access=None, core_llm=None, tool_registry=registry,
    )
    tools = executor.to_openai_tools()
    names = [t["function"]["name"] for t in tools]
    assert "search_papers" in names
    assert "read_paper" in names
    assert "read_note" in names
    assert "check_duplicate" in names
    assert "audit_claim" in names


def test_execute_roundtable_only_tool_returns_unavailable():
    """圆桌专属工具调用时返回不可用提示。"""
    import asyncio
    from paperreadagent.modules.ideator.tool_registry import create_default_registry
    registry = create_default_registry()
    executor = ToolExecutor(
        data_access=None, core_llm=None, tool_registry=registry,
    )

    roundtable_tools = [
        "create_spark", "update_spark", "trigger_recall",
        "fetch_snapshot", "write_memory", "read_memory",
        "report_watermark", "adjust_quota", "grant_tool",
    ]
    for tool_name in roundtable_tools:
        result = asyncio.run(executor.execute(tool_name, {}))
        assert "圆桌讨论" in result, f"{tool_name} should be roundtable-only"


def test_execute_unknown_tool_returns_error():
    """不存在的工具返回错误信息。"""
    import asyncio
    executor = ToolExecutor(
        data_access=None, core_llm=None, tool_registry=None,
    )
    result = asyncio.run(executor.execute("nonexistent_tool", {}))
    assert "未知" in result or "不可用" in result


def test_execute_missing_data_returns_error():
    """缺少 data_access 时 read_paper 返回错误信息（不抛异常）。"""
    import asyncio
    from paperreadagent.modules.ideator.tool_registry import create_default_registry
    registry = create_default_registry()
    executor = ToolExecutor(
        data_access=None, core_llm=None, tool_registry=registry,
    )
    result = asyncio.run(executor.execute("read_paper", {"arxiv_id": "test"}))
    assert isinstance(result, str)
    assert len(result) > 0
