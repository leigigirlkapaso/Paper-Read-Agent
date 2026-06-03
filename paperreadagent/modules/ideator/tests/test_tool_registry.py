import pytest
from paperreadagent.modules.ideator.tool_registry import ToolRegistry, ToolDefinition, create_default_registry


def test_register_and_list_tools():
    reg = ToolRegistry()
    reg.register(ToolDefinition("search_papers", "Search papers by embedding", ["gen", "rev1", "rev2", "rev3"], {"query": "str"}))
    tools = reg.list_for_role("gen")
    assert len(tools) >= 1
    assert tools[0]["name"] == "search_papers"


def test_role_cannot_access_unauthorized_tool():
    reg = ToolRegistry()
    reg.register(ToolDefinition("create_spark", "Create a spark", ["gen"], {"content": "str"}))
    assert not reg.can_call("rev1", "create_spark")


def test_grant_temporary_access():
    reg = ToolRegistry()
    reg.register(ToolDefinition("create_spark", "Create a spark", ["gen"], {"content": "str"}))
    reg.grant_tool("rev1", "create_spark", reason="arbiter_approved", duration_rounds=1)
    assert reg.can_call("rev1", "create_spark")
    reg.revoke_tool("rev1", "create_spark")
    assert not reg.can_call("rev1", "create_spark")


def test_list_all_tools():
    reg = ToolRegistry()
    reg.register(ToolDefinition("a", "", ["gen"], {}))
    reg.register(ToolDefinition("b", "", ["rev1"], {}))
    assert len(reg.list_all()) == 2


def test_duplicate_register_raises():
    reg = ToolRegistry()
    reg.register(ToolDefinition("x", "", ["gen"], {}))
    with pytest.raises(ValueError):
        reg.register(ToolDefinition("x", "", ["gen"], {}))


def test_default_registry_has_14_tools():
    reg = create_default_registry()
    assert len(reg.list_all()) == 14


def test_default_registry_role_permissions():
    reg = create_default_registry()
    # gen can create sparks
    assert reg.can_call("gen", "create_spark")
    # rev1 can search but NOT create
    assert reg.can_call("rev1", "search_papers")
    assert not reg.can_call("rev1", "create_spark")
    # arb1 can write memory
    assert reg.can_call("arb1", "write_memory")
    assert reg.can_call("arb1", "grant_tool")


def test_arb1_arb2_have_equal_tools():
    from paperreadagent.modules.ideator.tool_registry import create_default_registry
    reg = create_default_registry()
    arb1_tools = set(t["name"] for t in reg.list_for_role("arb1"))
    arb2_tools = set(t["name"] for t in reg.list_for_role("arb2"))
    assert arb1_tools == arb2_tools
    assert "grant_tool" in arb1_tools
    assert "adjust_quota" in arb2_tools
    assert "report_watermark" in arb1_tools
    assert "write_memory" in arb2_tools
