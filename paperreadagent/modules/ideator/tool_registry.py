"""tool_registry.py — Agent Tool Registry + Role-Based Access Control."""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class ToolDefinition:
    name: str
    description: str
    default_roles: list[str]
    parameters: dict


class ToolRegistry:
    """14 Agent tools with RBAC by role + temporary grants."""

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._grants: dict[str, set[tuple[str, str]]] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' already registered")
        self._tools[tool.name] = tool

    def list_for_role(self, role: str) -> list[dict]:
        result = []
        for name, tool in self._tools.items():
            if role in tool.default_roles or self.can_call(role, name):
                result.append({
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                })
        return result

    def can_call(self, role: str, tool_name: str) -> bool:
        if tool_name not in self._tools:
            return False
        if role in self._tools[tool_name].default_roles:
            return True
        grants = self._grants.get(role, set())
        return any(t == tool_name for t, _ in grants)

    def grant_tool(self, role: str, tool_name: str, *, reason: str, duration_rounds: int = 1) -> None:
        if role not in self._grants:
            self._grants[role] = set()
        self._grants[role].add((tool_name, reason))

    def revoke_tool(self, role: str, tool_name: str) -> None:
        if role in self._grants:
            self._grants[role] = {(t, r) for t, r in self._grants[role] if t != tool_name}

    def list_all(self) -> list[dict]:
        return [{"name": t.name, "description": t.description} for t in self._tools.values()]

    def get_tool(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)


def create_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(ToolDefinition("search_papers", "Embedding search across all papers in the system", ["gen", "rev1", "rev2", "rev3"], {"query": "str", "top_k": "int"}))
    reg.register(ToolDefinition("read_paper", "Get full text of a paper", ["gen", "rev1", "rev2", "rev3"], {"arxiv_id": "str", "section": "str"}))
    reg.register(ToolDefinition("read_note", "Get user note content for a paper", ["gen", "rev3"], {"paper_id": "int"}))
    reg.register(ToolDefinition("create_spark", "Create a new spark (draft status)", ["gen"], {"content": "str", "source_type": "str", "source_refs": "list"}))
    reg.register(ToolDefinition("update_spark", "Update spark content (records evolution)", ["gen"], {"spark_id": "int", "content": "str"}))
    reg.register(ToolDefinition("check_duplicate", "Check if spark duplicates existing sparks", ["gen"], {"content": "str"}))
    reg.register(ToolDefinition("trigger_recall", "Trigger incremental cross-recall (system-internal only)", ["gen"], {"direction": "str", "keywords": "list"}))
    reg.register(ToolDefinition("audit_claim", "Audit: verify a claim against source text", ["rev2"], {"claim": "str", "source_text": "str"}))
    reg.register(ToolDefinition("fetch_snapshot", "Fetch cold-layer round snapshot", ["gen", "rev1", "rev2", "rev3", "arb1", "arb2"], {"round_number": "int"}))
    reg.register(ToolDefinition("write_memory", "Write structured team memory entry", ["arb1", "arb2"], {"memory_type": "str", "content": "str", "spark_id": "int"}))
    reg.register(ToolDefinition("read_memory", "Read team memory by type", ["gen", "rev1", "rev2", "rev3", "arb1", "arb2"], {"memory_type": "str", "spark_id": "int"}))
    reg.register(ToolDefinition("report_watermark", "Report current context watermark", ["arb1", "arb2"], {}))
    reg.register(ToolDefinition("adjust_quota", "Adjust per-role word quota for next round", ["arb1", "arb2"], {"role": "str", "quota": "int"}))
    reg.register(ToolDefinition("grant_tool", "Temporarily grant a tool to an agent", ["arb1", "arb2"], {"role": "str", "tool_name": "str", "reason": "str"}))
    return reg
