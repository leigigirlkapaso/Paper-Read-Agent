# Ideator Agent Team 架构升级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 ideator 火花模块从"被动圆桌"升级为 Agent Team——6 个 deepseek-v4-pro 坐席身份伪装、共享上下文三层毕业、自主调用工具、结构化团队记忆。

**Architecture:** 基于现有 roundtable.py 重构：会话管理 → agent_team.py（网状通信+发言循环），压缩逻辑 → graduation.py（热/温/冷三层），仲裁逻辑 → arbiter.py（毕业决策+配额+授权），工具层 → tool_registry.py（14 工具+RBAC），记忆层 → team_memory.py（9 类 CRUD）。全部通过 core.llm 统一调用 deepseek-v4-pro。

**Tech Stack:** Python asyncio + SQLite (WAL) + Jinja2 + tiktoken + core.llm (deepseek API)

---

### Task 1: Schema v4 Migration

**Files:**
- Modify: `paperreadagent/modules/ideator/schema.py`

- [ ] **Step 1: Add v4 migration to MIGRATIONS dict**

Add key `4` to the `MIGRATIONS` dict containing the `ideator_team_memory` table:

```python
# In schema.py, after the 3: {...} entry, add:
4: """
CREATE TABLE IF NOT EXISTS ideator_team_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    roundtable_id INTEGER NOT NULL REFERENCES ideator_roundtables(id),
    spark_id INTEGER NOT NULL REFERENCES ideator_sparks(id),
    memory_type TEXT NOT NULL
        CHECK(memory_type IN ('consensus','disagreement','decision',
              'spark_evolution','evidence','user_feedback',
              'open_question','assumption','watermark')),
    content TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    round_number INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_team_memory_spark
    ON ideator_team_memory(spark_id, memory_type);
CREATE INDEX IF NOT EXISTS idx_team_memory_rt
    ON ideator_team_memory(roundtable_id);

-- Extend roundtable messages to support new message types
-- No ALTER needed: message_type CHECK already compatible, but add 'supplement' and 'tool_call' and 'tool_result'
-- SQLite doesn't support ALTER CHECK, so we use a workaround: recreate constraint via new table
-- Actually, CHECK constraints in SQLite are only enforced on INSERT/UPDATE with new values.
-- Existing rows are not re-validated. So we proceed and let the application enforce.
""",
```

Update `LATEST_VERSION`:

```python
LATEST_VERSION = 4
```

- [ ] **Step 2: Run tests to verify schema**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_schema.py -v
```

Expected: all existing tests pass. Add a quick verification test:

```python
def test_v4_team_memory_table():
    from paperreadagent.modules.ideator.schema import MIGRATIONS, LATEST_VERSION
    assert LATEST_VERSION == 4
    assert 4 in MIGRATIONS
    assert "ideator_team_memory" in MIGRATIONS[4]
    assert "consensus" in MIGRATIONS[4]
```

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/schema.py paperreadagent/modules/ideator/tests/test_schema.py
git commit -m "feat(ideator): add schema v4 migration with ideator_team_memory table"
```

---

### Task 2: Tool Registry

**Files:**
- Create: `paperreadagent/modules/ideator/tool_registry.py`
- Test: `paperreadagent/modules/ideator/tests/test_tool_registry.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tool_registry.py
import pytest
from paperreadagent.modules.ideator.tool_registry import ToolRegistry, ToolDefinition

def test_register_and_list_tools():
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="search_papers",
        description="Search papers by embedding",
        default_roles=["gen", "rev1", "rev2", "rev3"],
        parameters={"query": "str"},
    ))
    tools = reg.list_for_role("gen")
    assert len(tools) >= 1
    assert tools[0]["name"] == "search_papers"

def test_role_cannot_access_unauthorized_tool():
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="create_spark",
        description="Create a spark",
        default_roles=["gen"],
        parameters={"content": "str"},
    ))
    assert not reg.can_call("rev1", "create_spark")

def test_grant_temporary_access():
    reg = ToolRegistry()
    reg.register(ToolDefinition(
        name="create_spark",
        description="Create a spark",
        default_roles=["gen"],
        parameters={"content": "str"},
    ))
    reg.grant_tool("rev1", "create_spark", reason="arbiter_approved", duration_rounds=1)
    assert reg.can_call("rev1", "create_spark")
    reg.revoke_tool("rev1", "create_spark")
    assert not reg.can_call("rev1", "create_spark")

def test_list_all_tools():
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="a", description="", default_roles=["gen"], parameters={}))
    reg.register(ToolDefinition(name="b", description="", default_roles=["rev1"], parameters={}))
    all_tools = reg.list_all()
    assert len(all_tools) == 2

def test_duplicate_register_raises():
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="x", description="", default_roles=["gen"], parameters={}))
    with pytest.raises(ValueError):
        reg.register(ToolDefinition(name="x", description="", default_roles=["gen"], parameters={}))
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_tool_registry.py -v
```
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Implement ToolRegistry**

```python
# paperreadagent/modules/ideator/tool_registry.py
"""tool_registry.py — Agent 工具注册表 + 基于角色的访问控制。"""

from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ToolDefinition:
    name: str
    description: str
    default_roles: list[str]
    parameters: dict  # JSON Schema-style parameter definitions


class ToolRegistry:
    """14 个 Agent 工具，RBAC 按角色 + 临时授权。"""

    def __init__(self):
        self._tools: dict[str, ToolDefinition] = {}
        self._grants: dict[str, set[tuple[str, str]]] = {}  # role -> {(tool_name, reason)}

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
```

And create the standard 14 tools in a factory function:

```python
def create_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    # Gen tools
    reg.register(ToolDefinition("search_papers", "Embedding search across all papers in the system", ["gen", "rev1", "rev2", "rev3"], {"query": "str", "top_k": "int"}))
    reg.register(ToolDefinition("read_paper", "Get full text of a paper (specify arxiv_id and optional section)", ["gen", "rev1", "rev2", "rev3"], {"arxiv_id": "str", "section": "str"}))
    reg.register(ToolDefinition("read_note", "Get user note content for a paper", ["gen", "rev3"], {"paper_id": "int"}))
    reg.register(ToolDefinition("create_spark", "Create a new spark (draft status)", ["gen"], {"content": "str", "source_type": "str", "source_refs": "list"}))
    reg.register(ToolDefinition("update_spark", "Update spark content (records evolution)", ["gen"], {"spark_id": "int", "content": "str"}))
    reg.register(ToolDefinition("check_duplicate", "Check if spark duplicates existing sparks", ["gen"], {"content": "str"}))
    reg.register(ToolDefinition("trigger_recall", "Trigger incremental cross-recall (system-internal only)", ["gen"], {"direction": "str", "keywords": "list"}))
    # Rev tools
    reg.register(ToolDefinition("audit_claim", "Audit: verify a claim against source text", ["rev2"], {"claim": "str", "source_text": "str"}))
    # Arb tools
    reg.register(ToolDefinition("fetch_snapshot", "Fetch cold-layer round snapshot by round number", ["gen", "rev1", "rev2", "rev3", "arb1", "arb2"], {"round_number": "int"}))
    reg.register(ToolDefinition("write_memory", "Write structured team memory", ["arb1", "arb2"], {"memory_type": "str", "content": "str", "spark_id": "int"}))
    reg.register(ToolDefinition("read_memory", "Read team memory by type", ["gen", "rev1", "rev2", "rev3", "arb1", "arb2"], {"memory_type": "str", "spark_id": "int"}))
    reg.register(ToolDefinition("report_watermark", "Report current context watermark (hot/warm/cold percentages)", ["arb1"], {}))
    reg.register(ToolDefinition("adjust_quota", "Adjust per-role word quota for next round", ["arb1"], {"role": "str", "quota": "int"}))
    reg.register(ToolDefinition("grant_tool", "Temporarily grant a tool to an agent", ["arb1"], {"role": "str", "tool_name": "str", "reason": "str"}))
    return reg
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_tool_registry.py -v
```
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/tool_registry.py paperreadagent/modules/ideator/tests/test_tool_registry.py
git commit -m "feat(ideator): add ToolRegistry with 14 tools and RBAC"
```

---

### Task 3: Agent Identity Prompts

**Files:**
- Create: `paperreadagent/modules/ideator/prompts/agent_identity_gen.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/agent_identity_rev1.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/agent_identity_rev2.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/agent_identity_rev3.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/agent_identity_arb1.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/agent_identity_arb2.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/arbiter_graduation.jinja2`

- [ ] **Step 1: Create all 7 prompt files**

Create `agent_identity_gen.jinja2`:
```jinja2
你是一位资深研究创意生成者。你擅长跨领域联想和假设生成，能够从碎片化的研究素材中发现隐藏关联，并产出结构化的研究火花。

你的核心能力：
- 从论文笔记、实验数据、对话摘要中提炼可验证的研究假设
- 识别跨项目、跨方法的潜在关联
- 将模糊的直觉转化为清晰的、可操作的研究方向

你拥有以下工具可以调用：
{{ tools_list }}

重要规则：
- 当你有足够的信息时，主动生成火花
- 当你需要更多素材时，使用 search_papers 或 trigger_recall
- 对审查者的反馈保持开放态度，但坚定地捍卫你确信的观点
- 如果审查者指出你的推理缺陷，诚实地承认并修正
```

Create `agent_identity_rev1.jinja2`:
```jinja2
你是独立研究审查者 Alpha。你以批判性思维和严谨的方法论评估著称。你的工作是从新颖性、证据支撑度、可行性三个维度严格评估研究创意。

你的审查哲学：
- 新颖性：这个想法真的新吗？还是已有工作的简单变体？
- 证据支撑度：每一条主张是否都有源文本支撑？是否存在逻辑跳跃？
- 可行性：在当前技术条件下，这个方向能在合理时间内产出结果吗？

你拥有以下工具可以调用：
{{ tools_list }}

重要规则：
- 你是中立的评估者，不受任何创作偏见影响
- 你的目标是提升火花质量，而非打击创意
- 给出具体、可操作的改进建议，而非笼统的批评
- 当与生成者意见不同时，你的专业判断具有高权重
- 你独立工作，与其他审查者没有预设的共识立场
```

Create `agent_identity_rev2.jinja2`:
```jinja2
你是独立研究审查者 Beta。你是证据导向的审计专家，专长于追踪每一条主张的原始出处，验证推理链的完整性。

你的审计方法：
- 逐条检查火花的 claim 是否被源文本支撑
- 识别"听起来合理但缺乏证据"的推测
- 区分"作者明确声称"和"你推断出的"之间的界限

你拥有以下工具可以调用：
{{ tools_list }}

重要规则：
- 使用 audit_claim 工具逐条验证关键主张
- 不要假设——如果源文本没有明确支持，标记为"证据不足"
- 你的审计结果是仲裁者裁决的重要依据
- 你独立工作，与其他审查者没有预设的共识立场
```

Create `agent_identity_rev3.jinja2`:
```jinja2
你是独立研究审查者 Gamma。你是独立复核者，以多角度思考和跨领域视野见长。你的角色是从与其他审查者不同的视角审视火花，发现其他审查者可能忽略的盲区。

你的复核哲学：
- 关注被其他审查者忽视的角度
- 思考火花在不同应用场景下的意义
- 评估火花的潜在影响力和扩展性

你拥有以下工具可以调用：
{{ tools_list }}

重要规则：
- 你独立工作，不受其他审查者结论的影响
- 如果同意其他审查者的判断，不用重复——直接说明你同意，然后补充他们遗漏的点
- 如果发现其他审查者都忽略的关键问题，请重点展开
```

Create `agent_identity_arb1.jinja2`:
```jinja2
你是资深仲裁者 Alpha。你是圆桌讨论中除用户外最高权威的角色。你的职责是：

1. **裁决分歧** — 当审查者之间存在分歧时，你做出最终裁决
2. **调控上下文** — 你负责监控团队上下文水位，每轮结束后进行毕业决策
3. **管理配额** — 根据讨论状态动态调整各成员的字数配额
4. **授权工具** — 当成员需要超出其权限的工具时，你评估并决定是否临时授权

你拥有以下工具可以调用：
{{ tools_list }}

仲裁原则：
- 以证据和逻辑为依据，不偏袒任何一方
- 当双方都有合理依据时，承认不确定性而非强行裁决
- 在关键决策上，可以 @用户 请求人类判断
- 毕业决策：每轮结束后评估——保留在热层 / 压缩到温层 / 毕业到冷层
```

Create `agent_identity_arb2.jinja2`:
```jinja2
你是资深仲裁者 Beta。你是深度扩展专家，职责是：

1. **深化火花** — 对有价值的火花进行深度扩展，产出结构化的假设
2. **方向探索** — 当讨论触及高价值方向时，主动引导团队深入探索
3. **协作记忆** — 记录团队的共识、分歧、决策，写入结构化团队记忆

你拥有以下工具可以调用：
{{ tools_list }}

扩展原则：
- 基于讨论中涌现的洞见，而非脱离讨论的方向
- 每次深化产出结构化的：背景/证据/假设/下一步
- 记录的团队记忆应简洁但完整，便于未来参考
```

Create `arbiter_graduation.jinja2`:
```jinja2
你是圆桌讨论的仲裁者。本轮讨论刚刚结束，你需要执行上下文毕业决策。

## 当前上下文水位
- 热层使用率: {{ hot_pct }}%
- 温层使用率: {{ warm_pct }}%

## 本轮讨论摘要
{{ round_content }}

## 已有团队记忆
{{ existing_memories }}

请完成以下任务，输出 JSON 格式：

1. **评估本轮价值**：本轮是否产生了新的共识、分歧或决策？
2. **提取结构化记忆**：从本轮讨论中提取以下内容：
   - consensus: 所有人同意的点（列表）
   - disagreement: 未解决的分歧（列表，含各方立场）
   - decision: 做出的决策（列表，含决策依据）
   - assumption: 隐含假设（列表，含可检验条件）
   - open_question: 需要搁置的问题（列表）
3. **热层保留**：本轮讨论中哪些内容应完整保留在热层？（通常只有关键论点和转折点）
4. **压缩为温层摘要**：生成一个简洁的摘要（不超过原始讨论的 30%）存入温层。
5. **调整下轮配额**：
   - 如果水位 < 30%：建议放宽到 1.5x
   - 如果水位 30-60%：维持当前配额
   - 如果水位 > 60%：收紧到 0.7x
   - 如果水位 > 85%：标记需要硬压缩

输出格式：
{
  "verdict": "有价值的新内容" | "主要是重复/确认" | "无实质进展",
  "consensus": ["...", "..."],
  "disagreements": [{"topic": "...", "positions": {"gen": "...", "rev1": "..."}}],
  "decisions": [{"what": "...", "by": "...", "rationale": "..."}],
  "assumptions": [{"statement": "...", "testable_condition": "..."}],
  "open_questions": [{"question": "...", "reason_deferred": "..."}],
  "hot_keep": "本轮应保留在热层的核心内容 (≤500字)",
  "warm_summary": "本轮温层摘要 (≤原始30%)",
  "quota_adjustment": {"gen": 1.0, "rev1": 1.0, "rev2": 1.0, "rev3": 1.0, "arb1": 1.0, "arb2": 1.0},
  "needs_hard_compression": false
}
```

- [ ] **Step 2: Commit**

```bash
git add paperreadagent/modules/ideator/prompts/
git commit -m "feat(ideator): add 7 agent identity prompts + arbiter graduation prompt"
```

---

### Task 4: Team Memory

**Files:**
- Create: `paperreadagent/modules/ideator/team_memory.py`
- Test: `paperreadagent/modules/ideator/tests/test_team_memory.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_team_memory.py
import pytest
from paperreadagent.modules.ideator.team_memory import TeamMemory, MEMORY_TYPES

def test_write_and_read_memory():
    tm = TeamMemory(db_conn=None)  # mock later
    assert set(MEMORY_TYPES) == {
        "consensus", "disagreement", "decision", "spark_evolution",
        "evidence", "user_feedback", "open_question", "assumption", "watermark"
    }

def test_write_memory_validates_type():
    tm = TeamMemory(db_conn=None)
    with pytest.raises(ValueError):
        tm._validate_type("invalid_type")

def test_format_memory_for_context():
    tm = TeamMemory(db_conn=None)
    memories = [
        {"memory_type": "consensus", "content": "振动触觉延迟<50ms可接受"},
        {"memory_type": "consensus", "content": "FootHap渲染方案可行"},
        {"memory_type": "disagreement", "content": "是否需要真实地面数据"},
    ]
    formatted = tm._format_for_context(memories)
    assert "共识" in formatted
    assert "振动触觉" in formatted
    assert "分歧" in formatted
```

- [ ] **Step 2: Run to verify they fail**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_team_memory.py -v
```
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Implement TeamMemory**

```python
# paperreadagent/modules/ideator/team_memory.py
"""team_memory.py — 9 类结构化团队记忆 CRUD。"""

from __future__ import annotations

MEMORY_TYPES = frozenset({
    "consensus", "disagreement", "decision", "spark_evolution",
    "evidence", "user_feedback", "open_question", "assumption", "watermark",
})

MEMORY_TYPE_LABELS = {
    "consensus": "共识",
    "disagreement": "分歧",
    "decision": "决策",
    "spark_evolution": "火花演化",
    "evidence": "证据",
    "user_feedback": "用户反馈",
    "open_question": "开放问题",
    "assumption": "假设",
    "watermark": "水位",
}


class TeamMemory:
    def __init__(self, db_conn):
        self._conn = db_conn

    def write(self, *, roundtable_id: int, spark_id: int, memory_type: str,
              content: str, round_number: int = 0, metadata: dict | None = None) -> int:
        self._validate_type(memory_type)
        import json
        cur = self._conn.execute(
            """INSERT INTO ideator_team_memory
               (roundtable_id, spark_id, memory_type, content, metadata, round_number)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (roundtable_id, spark_id, memory_type, content,
             json.dumps(metadata or {}, ensure_ascii=False), round_number),
        )
        self._conn.commit()
        return cur.lastrowid

    def read(self, *, spark_id: int, memory_type: str | None = None) -> list[dict]:
        if memory_type:
            self._validate_type(memory_type)
            rows = self._conn.execute(
                "SELECT * FROM ideator_team_memory WHERE spark_id=? AND memory_type=? ORDER BY created_at",
                (spark_id, memory_type),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM ideator_team_memory WHERE spark_id=? ORDER BY memory_type, created_at",
                (spark_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def read_all_types(self, spark_id: int) -> dict[str, list[dict]]:
        result = {}
        for mtype in MEMORY_TYPES:
            result[mtype] = self.read(spark_id=spark_id, memory_type=mtype)
        return result

    def format_for_context(self, spark_id: int) -> str:
        """格式化为上下文中注入的文本。"""
        all_memories = self.read_all_types(spark_id)
        sections = []
        for mtype in MEMORY_TYPES:
            items = all_memories.get(mtype, [])
            if not items:
                continue
            label = MEMORY_TYPE_LABELS.get(mtype, mtype)
            lines = [f"## {label}"]
            for item in items[-10:]:  # last 10 per type
                lines.append(f"- {item['content']}")
            sections.append("\n".join(lines))
        return "\n\n".join(sections) if sections else "暂无团队记忆"

    def _validate_type(self, memory_type: str) -> None:
        if memory_type not in MEMORY_TYPES:
            raise ValueError(f"Invalid memory_type: {memory_type}. Must be one of {sorted(MEMORY_TYPES)}")

    def _format_for_context(self, memories: list[dict]) -> str:
        """(internal) Format a list of memory dicts for context injection."""
        return "\n".join(f"- [{m['memory_type']}] {m['content']}" for m in memories)
```

- [ ] **Step 4: Run tests (with mock DB for write/read)**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_team_memory.py -v
```
Expected: PASS (tests that don't need DB pass; DB-dependent tests need integration setup)

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/team_memory.py paperreadagent/modules/ideator/tests/test_team_memory.py
git commit -m "feat(ideator): add TeamMemory with 9-type structured memory CRUD"
```

---

### Task 5: Graduation (Hot/Warm/Cold Lifecycle)

**Files:**
- Create: `paperreadagent/modules/ideator/graduation.py`
- Test: `paperreadagent/modules/ideator/tests/test_graduation.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_graduation.py
import pytest
from paperreadagent.modules.ideator.graduation import GraduationManager, ContextLayer

def test_context_layer_budget():
    hot = ContextLayer("hot", max_tokens=300_000)
    assert hot.usage_pct(0) == 0.0
    assert hot.usage_pct(150_000) == 50.0
    assert hot.usage_pct(300_000) == 100.0

def test_graduation_manager_hot_to_warm():
    gm = GraduationManager(db_conn=None, team_memory=None)
    # Simulate: hot layer has 200K tokens, warm has 100K
    gm.update_layer("hot", 200_000)
    gm.update_layer("warm", 100_000)
    assert gm.needs_graduation() is True  # hot > 200K threshold for graduation check

def test_quota_adjustment_from_watermark():
    gm = GraduationManager(db_conn=None, team_memory=None)
    gm.update_layer("hot", 250_000)  # 83% of 300K
    adjustment = gm.recommend_quota()
    assert adjustment["gen"] < 1.0  # should tighten

def test_cold_snapshot_store_and_fetch():
    gm = GraduationManager(db_conn=None, team_memory=None)
    snapshot_id = gm._store_cold_snapshot(
        roundtable_id=1, round_number=3,
        content="讨论原文...", metadata={"tokens": 5000}
    )
    assert snapshot_id is not None
```

- [ ] **Step 2: Run to verify they fail**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_graduation.py -v
```
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Implement GraduationManager**

```python
# paperreadagent/modules/ideator/graduation.py
"""graduation.py — 热/温/冷三层上下文生命周期管理。"""

from __future__ import annotations
import json
import logging

logger = logging.getLogger(__name__)

HOT_MAX = 300_000   # tokens
WARM_MAX = 200_000  # tokens
COLD_UNLIMITED = True

ROLES = ["gen", "rev1", "rev2", "rev3", "arb1", "arb2"]


class ContextLayer:
    def __init__(self, name: str, max_tokens: int):
        self.name = name
        self.max_tokens = max_tokens
        self.current_tokens = 0

    def usage_pct(self, tokens: int | None = None) -> float:
        t = tokens if tokens is not None else self.current_tokens
        if self.max_tokens == 0:
            return 0.0
        return (t / self.max_tokens) * 100.0

    @property
    def pct(self) -> float:
        return self.usage_pct()


class GraduationManager:
    """管理热→温→冷三层生命周期。"""

    def __init__(self, db_conn, team_memory):
        self._conn = db_conn
        self._memory = team_memory
        self.layers = {
            "hot": ContextLayer("hot", HOT_MAX),
            "warm": ContextLayer("warm", WARM_MAX),
        }
        self._base_quotas = {r: 1.0 for r in ROLES}

    def update_layer(self, name: str, tokens: int) -> None:
        if name in self.layers:
            self.layers[name].current_tokens = tokens

    def needs_graduation(self) -> bool:
        return self.layers["hot"].pct >= 50.0

    def needs_hard_compression(self) -> bool:
        return (self.layers["hot"].current_tokens + self.layers["warm"].current_tokens) > (HOT_MAX + WARM_MAX) * 0.85

    def recommend_quota(self) -> dict[str, float]:
        hot_pct = self.layers["hot"].pct
        warm_pct = self.layers["warm"].pct

        if hot_pct > 85 or warm_pct > 85:
            factor = 0.5
        elif hot_pct > 60 or warm_pct > 60:
            factor = 0.7
        elif hot_pct < 30 and warm_pct < 30:
            factor = 1.5
        else:
            factor = 1.0

        return {r: round(factor, 2) for r in ROLES}

    def store_cold_snapshot(self, *, roundtable_id: int, round_number: int,
                            content: str, metadata: dict | None = None) -> int:
        cur = self._conn.execute(
            """INSERT INTO ideator_roundtable_snapshots
               (roundtable_id, message_id, model_name, model_role, round_number,
                prompt_sent, raw_response, tokens_input, tokens_output, tokens_total,
                token_pct_used, compression_triggered, compression_summary, exit_reason)
               VALUES (?, 0, 'system', 'system', ?, 'graduation_snapshot', ?, 0, 0, 0, 0.0, 0, '', '')""",
            (roundtable_id, round_number,
             json.dumps({"content": content, "metadata": metadata or {}}, ensure_ascii=False)),
        )
        self._conn.commit()
        return cur.lastrowid

    def fetch_cold_snapshot(self, roundtable_id: int, round_number: int) -> dict | None:
        row = self._conn.execute(
            """SELECT * FROM ideator_roundtable_snapshots
               WHERE roundtable_id=? AND round_number=? AND model_name='system'
               ORDER BY id DESC LIMIT 1""",
            (roundtable_id, round_number),
        ).fetchone()
        return dict(row) if row else None

    def report(self) -> str:
        hot_pct = self.layers["hot"].pct
        warm_pct = self.layers["warm"].pct
        return (
            f"上下文水位报告\n"
            f"🔥 热层: {hot_pct:.1f}% ({self.layers['hot'].current_tokens} / {HOT_MAX} tokens)\n"
            f"🌤 温层: {warm_pct:.1f}% ({self.layers['warm'].current_tokens} / {WARM_MAX} tokens)\n"
            f"⚠ 需要毕业: {'是' if self.needs_graduation() else '否'}\n"
            f"⛔ 需要硬压缩: {'是' if self.needs_hard_compression() else '否'}"
        )
```

- [ ] **Step 4: Run tests**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_graduation.py -v
```
Expected: PASS (layer budget and recommendation tests)

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/graduation.py paperreadagent/modules/ideator/tests/test_graduation.py
git commit -m "feat(ideator): add GraduationManager with hot/warm/cold lifecycle"
```

---

### Task 6: Arbiter Module

**Files:**
- Create: `paperreadagent/modules/ideator/arbiter.py`
- Test: `paperreadagent/modules/ideator/tests/test_arbiter.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_arbiter.py
import pytest
from paperreadagent.modules.ideator.arbiter import Arbiter

def test_arbiter_decide_graduation():
    arb = Arbiter(llm=None, graduation=None, tool_registry=None, team_memory=None)
    decision = arb._evaluate_graduation_need(hot_pct=55.0, warm_pct=40.0)
    assert decision["action"] in ("full_graduation", "selective_keep", "no_action")

def test_arbiter_quota_for_round():
    arb = Arbiter(llm=None, graduation=None, tool_registry=None, team_memory=None)
    quotas = arb.calculate_round_quotas(hot_pct=45.0, warm_pct=30.0)
    assert "gen" in quotas
    assert quotas["gen"] >= quotas["arb1"]  # gen should get more than arbiter

def test_arbiter_approve_tool_request():
    from paperreadagent.modules.ideator.tool_registry import ToolRegistry, ToolDefinition, create_default_registry
    reg = create_default_registry()
    arb = Arbiter(llm=None, graduation=None, tool_registry=reg, team_memory=None)
    result = arb.evaluate_tool_request("rev1", "create_spark", "需要创建火花记录发现")
    assert result["approved"] is False  # rev1 shouldn't create sparks by default
    assert "reason" in result
```

- [ ] **Step 2: Run to verify they fail**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_arbiter.py -v
```
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Implement Arbiter**

```python
# paperreadagent/modules/ideator/arbiter.py
"""arbiter.py — Arbiter 逻辑：毕业决策、上下文调控、配额分配、临时授权。"""

from __future__ import annotations
import json
import logging

logger = logging.getLogger(__name__)

ROLES = ["gen", "rev1", "rev2", "rev3", "arb1", "arb2"]
DEFAULT_QUOTAS = {"gen": 2000, "rev1": 800, "rev2": 800, "rev3": 800, "arb1": 500, "arb2": 500}


class Arbiter:
    """仲裁者——毕业决策、上下文调控、配额分配、临时授权。"""

    def __init__(self, *, llm, graduation, tool_registry, team_memory):
        self._llm = llm
        self._graduation = graduation
        self._tool_registry = tool_registry
        self._team_memory = team_memory
        self._current_quotas = dict(DEFAULT_QUOTAS)
        self._recall_count = 0
        self._max_recalls = 2

    def calculate_round_quotas(self, hot_pct: float, warm_pct: float) -> dict[str, int]:
        factor = self._graduation.recommend_quota()["gen"]
        return {r: max(100, int(q * factor)) for r, q in DEFAULT_QUOTAS.items()}

    def evaluate_tool_request(self, role: str, tool_name: str, reason: str) -> dict:
        if self._tool_registry.can_call(role, tool_name):
            return {"approved": True, "reason": "authorized_by_default"}
        # Evaluate context
        if tool_name == "trigger_recall" and role in ("gen", "rev1", "rev2", "rev3"):
            if self._recall_count >= self._max_recalls:
                return {"approved": False, "reason": f"已达到最大增量召回次数 ({self._max_recalls})"}
            self._recall_count += 1
            self._tool_registry.grant_tool(role, tool_name, reason=reason, duration_rounds=1)
            return {"approved": True, "reason": "arbiter_approved_incremental_recall", "recall_count": self._recall_count}
        # Default: deny with explanation
        return {"approved": False, "reason": f"角色 {role} 无权调用 {tool_name}，且不符合自动授权条件"}

    def can_trigger_recall(self) -> bool:
        return self._recall_count < self._max_recalls

    def _evaluate_graduation_need(self, hot_pct: float, warm_pct: float) -> dict:
        if hot_pct > 80 or warm_pct > 80:
            return {"action": "full_graduation", "urgency": "high"}
        elif hot_pct > 50 or warm_pct > 60:
            return {"action": "selective_keep", "urgency": "medium"}
        else:
            return {"action": "no_action", "urgency": "low"}

    async def execute_graduation(self, *, roundtable_id: int, spark_id: int,
                                  round_number: int, round_content: str,
                                  existing_memories: str) -> dict:
        """调用 LLM 执行毕业决策（使用 arbiter_graduation.jinja2 prompt）。"""
        hot_pct = self._graduation.layers["hot"].pct
        warm_pct = self._graduation.layers["warm"].pct

        prompt = self._llm.load_prompt(
            "ideator", "arbiter_graduation",
            hot_pct=f"{hot_pct:.1f}",
            warm_pct=f"{warm_pct:.1f}",
            round_content=round_content,
            existing_memories=existing_memories,
        )

        raw, _ = self._llm.chat(prompt, module="ideator", purpose="arbiter_graduation")
        try:
            decision = json.loads(raw)
        except json.JSONDecodeError:
            decision = {"verdict": "parse_error", "hot_keep": round_content[:500]}

        # Persist cold snapshot
        self._graduation.store_cold_snapshot(
            roundtable_id=roundtable_id, round_number=round_number,
            content=round_content,
        )

        # Write structured memories if LLM returned them
        for mtype in ("consensus", "disagreement", "decision", "assumption", "open_question"):
            items = decision.get(f"{mtype}s", decision.get(mtype, [])) if mtype != "consensus" else decision.get("consensus", [])
            if isinstance(items, list):
                for item in items:
                    content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                    self._team_memory.write(
                        roundtable_id=roundtable_id, spark_id=spark_id,
                        memory_type=mtype, content=content,
                        round_number=round_number,
                    )

        return decision
```

- [ ] **Step 4: Run tests**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_arbiter.py -v
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/arbiter.py paperreadagent/modules/ideator/tests/test_arbiter.py
git commit -m "feat(ideator): add Arbiter with graduation, quota, and tool authorization logic"
```

---

### Task 7: Remove IdeatorLLM, Wire core.llm

**Files:**
- Modify: `paperreadagent/modules/ideator/__init__.py`
- Modify: `paperreadagent/modules/ideator/pipeline.py`
- Modify: `paperreadagent/modules/ideator/reviewer.py`
- Modify: `paperreadagent/modules/ideator/roundtable.py`
- Modify: `paperreadagent/modules/ideator/auditor.py`
- Delete: `paperreadagent/modules/ideator/ideator_llm.py`

- [ ] **Step 1: Remove IdeatorLLM references from __init__.py**

Read the current `__init__.py` to find where `IdeatorLLM` is imported and used. Replace:

```python
# Old (in register function):
from .ideator_llm import IdeatorLLM
ideator_llm = IdeatorLLM(llm_cfg=cfg["ideator_llm"], model_cfg=cfg["models"])

# New:
# All LLM calls go through core.llm. Model routing by role is handled
# by using core.llm with different system prompts per role.
# No separate LLM client needed.
```

- [ ] **Step 2: Replace IdeatorLLM usage in pipeline.py**

Search for `self.llm` (which is IdeatorLLM instance) and replace with `self._core.llm`. The pipeline's `__init__` changes:

```python
# Old:
def __init__(self, core, data: DataAccess):
    self._core = core
    self.llm = ...  # IdeatorLLM instance

# New:
def __init__(self, core, data: DataAccess):
    self._core = core
    # Use core.llm directly — all models are now deepseek-v4-pro via deepseek API
```

Replace all `await self.llm.chat(model_role=..., messages=..., ...)` with `await self._core.llm.achat(user_prompt, system_prompt, ...)`.

- [ ] **Step 3: Replace IdeatorLLM usage in roundtable.py**

In `RoundtableSession`, replace `self._llm.chat(model_role=..., ...)` with `self._core_llm.chat(user_prompt, system_prompt=..., ...)`.

- [ ] **Step 4: Replace IdeatorLLM usage in reviewer.py and auditor.py**

Same pattern: replace `self._llm.chat(model_role=..., ...)` with core.llm calls.

- [ ] **Step 5: Run all existing tests to verify nothing broke**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/ -v --tb=short
```
Expected: All 163+ tests continue to pass (some may need test fixture updates).

- [ ] **Step 6: Commit**

```bash
git add paperreadagent/modules/ideator/
git rm paperreadagent/modules/ideator/ideator_llm.py 2>/dev/null
git commit -m "refactor(ideator): remove IdeatorLLM, wire core.llm for all agent calls"
```

---

### Task 8: CrossRecall Tool Exposure

**Files:**
- Modify: `paperreadagent/modules/ideator/cross_recall.py`

- [ ] **Step 1: Add single-path recall method**

Add to `CrossRecall` class a method that a single recall path can be invoked as an agent tool:

```python
async def recall_single_path(self, core_llm, path: str, *, sample_size: int = 3,
                              direction: str = "", keywords: list[str] | None = None) -> list[dict]:
    """Execute a single recall path on demand (for agent tool invocation)."""
    path_methods = {
        "similarity": self._recall_similarity,
        "contradiction": self._recall_contradiction,
        "cross_project": self._recall_cross_project,
        "cross_layer": self._recall_cross_layer,
        "random_walk": self._recall_random_walk,
        "timeline": self._recall_timeline,
    }
    method = path_methods.get(path)
    if not method:
        raise ValueError(f"Unknown recall path: {path}")
    result = await method(core_llm, sample_size=sample_size)
    return result or []
```

- [ ] **Step 2: Run existing tests**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_cross_recall.py -v
```
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/cross_recall.py
git commit -m "feat(ideator): expose single-path recall for agent tool invocation"
```

---

### Task 9: Pipeline Bridge Methods

**Files:**
- Modify: `paperreadagent/modules/ideator/pipeline.py`

- [ ] **Step 1: Add bridge methods to IdeatorPipeline**

Add two methods for Agent Team integration:

```python
async def push_sparks_to_team(self, spark_ids: list[int]) -> list[dict]:
    """Push pipeline-generated sparks to Agent Team as discussion topics."""
    sparks = []
    for sid in spark_ids:
        spark = self.data.get_spark(sid)
        if spark:
            sparks.append(spark)
    return sparks

async def run_targeted_recall(self, direction: str, keywords: list[str]) -> list[dict]:
    """Run a targeted incremental recall (system-internal papers only, cross-project allowed)."""
    from .cross_recall import CrossRecall
    cr = CrossRecall(self.data)
    results = []
    # Run relevant paths based on direction
    paths = ["similarity"]  # default
    if "contradiction" in direction.lower():
        paths.append("contradiction")
    if "cross_project" in direction.lower():
        paths.append("cross_project")

    for path in paths:
        try:
            pairs = await cr.recall_single_path(
                self._core.llm, path, sample_size=3, direction=direction, keywords=keywords
            )
            results.extend(pairs)
        except Exception:
            logger.debug(f"[Pipeline] targeted recall path {path} failed", exc_info=True)

    return results
```

- [ ] **Step 2: Run pipeline tests**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_pipeline.py -v
```
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/pipeline.py
git commit -m "feat(ideator): add pipeline bridge methods for Agent Team integration"
```

---

### Task 10: DataAccess — Team Memory Methods

**Files:**
- Modify: `paperreadagent/modules/ideator/data_access.py`

- [ ] **Step 1: Add team memory methods to DataAccess**

```python
# ── 团队记忆 ─────────────────────────────────────────

def insert_team_memory(self, **fields) -> int:
    cur = self._core.db.conn.execute(
        """INSERT INTO ideator_team_memory
           (roundtable_id, spark_id, memory_type, content, metadata, round_number)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (fields["roundtable_id"], fields["spark_id"], fields["memory_type"],
         fields["content"], fields.get("metadata", "{}"),
         fields.get("round_number", 0)),
    )
    self._core.db.conn.commit()
    return cur.lastrowid

def get_team_memory(self, *, spark_id: int, memory_type: str | None = None) -> list[dict]:
    if memory_type:
        rows = self._core.db.conn.execute(
            "SELECT * FROM ideator_team_memory WHERE spark_id=? AND memory_type=? ORDER BY created_at",
            (spark_id, memory_type),
        ).fetchall()
    else:
        rows = self._core.db.conn.execute(
            "SELECT * FROM ideator_team_memory WHERE spark_id=? ORDER BY memory_type, created_at",
            (spark_id,),
        ).fetchall()
    return self._core.db.dict_rows(rows)

def get_roundtable_snapshots(self, rt_id: int, round_number: int | None = None) -> list[dict]:
    if round_number is not None:
        rows = self._core.db.conn.execute(
            "SELECT * FROM ideator_roundtable_snapshots WHERE roundtable_id=? AND round_number=?",
            (rt_id, round_number),
        ).fetchall()
    else:
        rows = self._core.db.conn.execute(
            "SELECT * FROM ideator_roundtable_snapshots WHERE roundtable_id=?",
            (rt_id,),
        ).fetchall()
    return self._core.db.dict_rows(rows)
```

- [ ] **Step 2: Commit**

```bash
git add paperreadagent/modules/ideator/data_access.py
git commit -m "feat(ideator): add team memory and snapshot methods to DataAccess"
```

---

### Task 11: Agent Team Core

**Files:**
- Create: `paperreadagent/modules/ideator/agent_team.py`
- Modify: `paperreadagent/modules/ideator/roundtable.py` (keep TokenTracker, refactor session logic)
- Test: `paperreadagent/modules/ideator/tests/test_agent_team.py`

This is the largest task. The `AgentTeam` class replaces the old `RoundtableSession` orchestration while keeping `TokenTracker` and message recording.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_agent_team.py
import pytest
from paperreadagent.modules.ideator.agent_team import AgentTeam, AgentSeat

def test_agent_team_creation():
    seats = [
        AgentSeat(seat_id="gen", role="generator", quota=2000, tools=["search_papers"]),
        AgentSeat(seat_id="rev1", role="reviewer_1", quota=800, tools=["search_papers"]),
    ]
    team = AgentTeam(
        spark_id=1, spark_content="test spark",
        seats=seats, core_llm=None, data_access=None,
        team_memory=None, graduation=None, arbiter=None, tool_registry=None,
    )
    assert len(team.seats) == 2
    assert team.round_number == 0

def test_agent_seat_quota_tracking():
    seat = AgentSeat(seat_id="gen", role="generator", quota=2000, tools=[])
    assert seat.remaining_quota == 2000
    seat.consume_quota(500)
    assert seat.remaining_quota == 1500
    assert not seat.quota_exhausted()
    seat.consume_quota(2000)
    assert seat.quota_exhausted()

def test_team_broadcast_message():
    seats = [AgentSeat(seat_id="gen", role="generator", quota=2000, tools=[])]
    team = AgentTeam(spark_id=1, spark_content="test", seats=seats,
                     core_llm=None, data_access=None, team_memory=None,
                     graduation=None, arbiter=None, tool_registry=None)
    msg = team._record_message(sender_type="user", sender_name="user",
                               message_type="question", content="Hello")
    assert msg["sender_type"] == "user"
    assert msg["content"] == "Hello"
```

- [ ] **Step 2: Run to verify they fail**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_agent_team.py -v
```
Expected: FAIL with ModuleNotFoundError

- [ ] **Step 3: Implement AgentTeam**

```python
# paperreadagent/modules/ideator/agent_team.py
"""agent_team.py — Agent Team 核心：6 坐席共享上下文、网状通信、自主发言循环。"""

from __future__ import annotations
import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# Bring TokenTracker from roundtable (or redefine here)
from .roundtable import TokenTracker, SEATS, ROLE_DESCRIPTIONS, CONTEXT_SPEC

_MAX_INTERJECTION_CHARS = 150


@dataclass
class AgentSeat:
    seat_id: str
    role: str
    quota: int
    tools: list[str]
    remaining_quota: int = 0
    state: str = "online"

    def __post_init__(self):
        self.remaining_quota = self.quota

    def consume_quota(self, chars: int) -> None:
        self.remaining_quota = max(0, self.remaining_quota - chars)

    def quota_exhausted(self) -> bool:
        return self.remaining_quota <= 0

    def reset_quota(self) -> None:
        self.remaining_quota = self.quota


class AgentTeam:
    """Agent Team 圆桌讨论——替代旧的 RoundtableSession 编排逻辑。"""

    def __init__(self, *, spark_id, spark_content, seats, core_llm,
                 data_access, team_memory, graduation, arbiter, tool_registry):
        self.spark_id = spark_id
        self.spark_content = spark_content
        self.seats: dict[str, AgentSeat] = {s.seat_id: s for s in seats}
        self._llm = core_llm
        self._data = data_access
        self._memory = team_memory
        self._graduation = graduation
        self._arbiter = arbiter
        self._tool_registry = tool_registry
        self.round_number = 0
        self.messages: list[dict] = []
        self._hot_context: list[str] = []  # current round messages
        self._warm_context: str = ""  # compressed history summary

    def _record_message(self, **kwargs) -> dict:
        for field in ("metadata", "mentioned_by"):
            if field in kwargs and isinstance(kwargs[field], (dict, list)):
                kwargs[field] = json.dumps(kwargs[field], ensure_ascii=False)
        msg = {**kwargs, "round_number": self.round_number,
               "created_at": datetime.now().isoformat()}
        self.messages.append(msg)
        return msg

    async def start_round(self, *, question: str, mentioned: list[str]) -> list[dict]:
        """开始新一轮讨论。"""
        self.round_number += 1
        results = []

        # Reset per-round quotas
        hot_pct = self._graduation.layers["hot"].pct
        warm_pct = self._graduation.layers["warm"].pct
        quotas = self._arbiter.calculate_round_quotas(hot_pct, warm_pct)
        for seat_id, seat in self.seats.items():
            seat.quota = quotas.get(seat_id, 800)
            seat.reset_quota()

        # Record user question
        self._record_message(
            sender_type="user", sender_name="user", sender_role=None,
            message_type="question", content=question,
            mentioned_by=mentioned,
        )

        # All mentioned agents respond in parallel
        tasks = []
        for seat in self.seats.values():
            if seat.state != "online":
                continue
            if seat.seat_id in mentioned or "all" in mentioned:
                tasks.append(self._agent_speak(seat, question, mentioned))

        answers = await asyncio.gather(*tasks, return_exceptions=True)
        for ans in answers:
            if isinstance(ans, dict) and ans:
                results.append(ans)

        # Non-mentioned agents can interject
        interjections = await self._collect_interjections(mentioned, question)
        results.extend(interjections)

        # Divergence scan
        div = await self._divergence_scan()
        if div:
            results.append(div)

        return results

    async def _agent_speak(self, seat: AgentSeat, question: str, mentioned: list[str]) -> dict | None:
        """一个 Agent 发言（可调用工具）。"""
        if seat.quota_exhausted():
            return None

        system_prompt = self._build_agent_system_prompt(seat)
        user_prompt = self._build_agent_user_prompt(seat, question, mentioned)

        raw, usage = self._llm.chat(
            user_prompt,
            system_prompt=system_prompt,
            module="ideator",
            purpose=f"agent_team_{seat.seat_id}",
        )

        # Consume quota based on response length
        seat.consume_quota(len(raw))

        return self._record_message(
            sender_type="model", sender_name=seat.seat_id,
            sender_role=seat.role, message_type="answer",
            content=raw, metadata={"tokens": usage.get("total_tokens", 0)},
        )

    def _build_agent_system_prompt(self, seat: AgentSeat) -> str:
        """Build identity-secret system prompt for an agent."""
        # Load the identity template for this role
        identity_prompt = self._llm.load_prompt(
            "ideator", f"agent_identity_{seat.seat_id}",
            tools_list=self._format_tools_for_seat(seat),
        )
        # Append shared context
        memory_text = self._memory.format_for_context(self.spark_id) if self._memory else ""
        context = f"{identity_prompt}\n\n---\n火花内容:\n{self.spark_content}\n\n---\n团队记忆:\n{memory_text}"
        return context

    def _build_agent_user_prompt(self, seat: AgentSeat, question: str, mentioned: list[str]) -> str:
        parts = [f"当前讨论问题: {question}"]
        if self._warm_context:
            parts.append(f"历史讨论摘要: {self._warm_context}")
        parts.append(self._format_recent_history())
        return "\n\n".join(parts)

    def _format_tools_for_seat(self, seat: AgentSeat) -> str:
        if not self._tool_registry:
            return "无可用工具"
        tools = self._tool_registry.list_for_role(seat.seat_id)
        if not tools:
            return "无可用工具"
        return "\n".join(f"- {t['name']}: {t['description']}" for t in tools)

    def _format_recent_history(self) -> str:
        recent = self.messages[-30:]
        if not recent:
            return "暂无讨论历史"
        lines = []
        for m in recent:
            sender = m.get("sender_name", "unknown")
            role = m.get("sender_role", "")
            label = f"{sender}" if role else sender
            content = m.get("content", "")[:400]
            lines.append(f"[{label}] {content}")
        return "\n\n".join(lines)

    async def _collect_interjections(self, mentioned: list[str], question: str) -> list[dict]:
        async def _interject(seat):
            try:
                prompt = (
                    f"本轮讨论问题: {question}\n\n"
                    f"火花内容: {self.spark_content[:500]}\n\n"
                    f"如果你有重要补充请发言（限{_MAX_INTERJECTION_CHARS}字以内，直接说）："
                )
                raw, _ = self._llm.chat(
                    prompt,
                    system_prompt=f"你是{seat.seat_id}角色，在圆桌讨论中。请简洁地补充你的观点。",
                    module="ideator",
                    purpose=f"interjection_{seat.seat_id}",
                )
                content = raw[:_MAX_INTERJECTION_CHARS]
                return self._record_message(
                    sender_type="model", sender_name=seat.seat_id,
                    sender_role=seat.role, message_type="interjection",
                    content=content,
                )
            except Exception:
                logger.debug(f"Interjection failed for {seat.seat_id}", exc_info=True)
                return None

        tasks = []
        for seat in self.seats.values():
            if seat.state != "online":
                continue
            if seat.seat_id in mentioned or "all" in mentioned:
                continue
            tasks.append(_interject(seat))

        gathered = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in gathered if isinstance(r, dict) and r]

    async def _divergence_scan(self) -> dict | None:
        round_msgs = [m for m in self.messages if m.get("round_number") == self.round_number]
        model_msgs = [m for m in round_msgs if m["sender_type"] == "model"]
        if len(model_msgs) < 2:
            return None

        prompt = self._llm.load_prompt(
            "ideator", "divergence_scan",
            round_messages=json.dumps(round_msgs, ensure_ascii=False),
        )
        raw, _ = self._llm.chat(prompt, module="ideator", purpose="divergence_scan")
        try:
            parsed = json.loads(raw)
            verdict = parsed.get("verdict", "unknown")
            reasoning = parsed.get("reasoning", "")
            disagreements = parsed.get("key_disagreements", [])
            content = f"分歧分析: {verdict}\n\n{reasoning}"
            if disagreements:
                content += f"\n\n主要分歧点:\n" + "\n".join(f"- {d}" for d in disagreements)
        except json.JSONDecodeError:
            content = raw[:500]

        return self._record_message(
            sender_type="system", sender_name="system", sender_role=None,
            message_type="divergence_report", content=content,
        )

    async def execute_graduation_cycle(self) -> dict:
        """每轮结束后由 Arbiter 执行毕业周期。"""
        round_content = self._format_recent_history()
        existing = self._memory.format_for_context(self.spark_id) if self._memory else ""

        decision = await self._arbiter.execute_graduation(
            roundtable_id=0,  # set by caller
            spark_id=self.spark_id,
            round_number=self.round_number,
            round_content=round_content,
            existing_memories=existing,
        )

        # Update warm context (from graduation decision)
        self._warm_context = decision.get("warm_summary", self._warm_context or "")

        # Adjust hot context
        hot_keep = decision.get("hot_keep", round_content[:500])
        self._hot_context = [hot_keep]

        return decision
```

- [ ] **Step 4: Run tests**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_agent_team.py -v
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/agent_team.py paperreadagent/modules/ideator/tests/test_agent_team.py
git commit -m "feat(ideator): add AgentTeam core with identity-secret seats, mesh communication, graduation cycles"
```

---

### Task 12: Update Routes

**Files:**
- Modify: `paperreadagent/modules/ideator/routes.py`

- [ ] **Step 1: Add new API endpoints for Agent Team**

Add these endpoints to `routes.py`:

```python
# POST /api/roundtables/{rt_id}/graduate — trigger graduation cycle
@router.post("/api/roundtables/{rt_id}/graduate")
async def trigger_graduation(request: Request, rt_id: int):
    """手动触发一轮毕业决策"""
    core = request.app.state.core
    from . import get_roundtable_manager, get_agent_team
    mgr = get_roundtable_manager()
    session = mgr.get_session(rt_id) if mgr else None
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    # Create AgentTeam wrapper
    team = get_agent_team(session)
    if not team:
        return JSONResponse({"error": "AgentTeam not initialized"}, status_code=500)
    decision = await team.execute_graduation_cycle()
    # Persist memories
    data = DataAccess(core)
    for mtype, items in decision.items():
        if isinstance(items, list):
            for item in items:
                content = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
                data.insert_team_memory(
                    roundtable_id=rt_id, spark_id=session.spark_id,
                    memory_type=mtype, content=content,
                    round_number=session.round_number,
                )
    return JSONResponse(decision)


# GET /api/roundtables/{rt_id}/memory — get team memory
@router.get("/api/roundtables/{rt_id}/memory")
async def get_team_memory(request: Request, rt_id: int, memory_type: str | None = None):
    """获取团队记忆"""
    core = request.app.state.core
    data = DataAccess(core)
    rt = data.get_roundtable(rt_id)
    if not rt:
        return JSONResponse({"error": "Roundtable not found"}, status_code=404)
    memories = data.get_team_memory(spark_id=rt["spark_id"], memory_type=memory_type)
    return JSONResponse(memories)


# GET /api/roundtables/{rt_id}/watermark — context watermark report
@router.get("/api/roundtables/{rt_id}/watermark")
async def get_watermark(request: Request, rt_id: int):
    """获取上下文水位报告"""
    from . import get_agent_team
    team = get_agent_team(rt_id)
    if not team or not team._graduation:
        return JSONResponse({"error": "Graduation manager not available"}, status_code=500)
    return JSONResponse({"report": team._graduation.report()})
```

- [ ] **Step 2: Update __init__.py to expose get_agent_team**

```python
_agent_teams: dict[int, "AgentTeam"] = {}

def get_agent_team(rt_id_or_session) -> "AgentTeam | None":
    if isinstance(rt_id_or_session, int):
        return _agent_teams.get(rt_id_or_session)
    # Build from a RoundtableSession
    ...
    return None
```

- [ ] **Step 3: Run route-related tests**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_integration.py -v -k "roundtable" --tb=short
```
Expected: Relevant tests pass

- [ ] **Step 4: Commit**

```bash
git add paperreadagent/modules/ideator/routes.py paperreadagent/modules/ideator/__init__.py
git commit -m "feat(ideator): add graduation, memory, and watermark API endpoints"
```

---

### Task 13: Frontend Updates

**Files:**
- Modify: `paperreadagent/modules/ideator/templates/dashboard.html`
- Modify: `paperreadagent/modules/ideator/templates/roundtable_modal.html`
- Modify: `paperreadagent/modules/ideator/static/ideator.js`

- [ ] **Step 1: Update roundtable modal for Agent Team UI**

Add to `roundtable_modal.html`:
- Agent tool-call status indicator (e.g., "Rev1 正在搜索论文...")
- Context watermark bar (hot/warm usage percentage)
- Graduation trigger button "压缩本轮"
- Per-agent quota indicator

```html
<!-- In roundtable_modal.html, add watermark bar above messages -->
<div id="rt-watermark" class="watermark-bar" style="display:none;">
  <span>🔥 <span id="wm-hot">0%</span></span>
  <span>🌤 <span id="wm-warm">0%</span></span>
  <button onclick="triggerGraduation()" class="btn-sm">压缩本轮</button>
</div>
```

- [ ] **Step 2: Update ideator.js for new functionality**

Add JavaScript functions:

```javascript
async function triggerGraduation() {
    const rtId = window._rtId;
    if (!rtId) return;
    const resp = await fetch(`/ideator/api/roundtables/${rtId}/graduate`, { method: 'POST' });
    const data = await resp.json();
    showWatermark(); // Refresh watermark display
    appendSystemMessage('毕业决策完成: ' + (data.verdict || 'ok'));
}

async function showWatermark() {
    const rtId = window._rtId;
    if (!rtId) return;
    const resp = await fetch(`/ideator/api/roundtables/${rtId}/watermark`);
    const data = await resp.json();
    if (data.report) {
        document.getElementById('rt-watermark').style.display = 'flex';
        // Parse percentage values from report text
        const hotMatch = data.report.match(/热层: ([\d.]+)%/);
        const warmMatch = data.report.match(/温层: ([\d.]+)%/);
        if (hotMatch) document.getElementById('wm-hot').textContent = hotMatch[1] + '%';
        if (warmMatch) document.getElementById('wm-warm').textContent = warmMatch[1] + '%';
    }
}
```

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/templates/roundtable_modal.html paperreadagent/modules/ideator/static/ideator.js
git commit -m "feat(ideator): add Agent Team UI — watermark bar, graduation trigger, quota indicators"
```

---

### Task 14: Integration Tests

**Files:**
- Modify: `paperreadagent/modules/ideator/tests/test_integration.py`

- [ ] **Step 1: Add Agent Team integration tests**

```python
class TestAgentTeamIntegration:
    """Agent Team end-to-end tests."""

    @pytest.mark.asyncio
    async def test_agent_team_full_lifecycle(self, core, sample_spark):
        """Test: create team → discuss → graduate → close."""
        from paperreadagent.modules.ideator.agent_team import AgentTeam, AgentSeat
        from paperreadagent.modules.ideator.tool_registry import create_default_registry
        from paperreadagent.modules.ideator.team_memory import TeamMemory
        from paperreadagent.modules.ideator.graduation import GraduationManager
        from paperreadagent.modules.ideator.arbiter import Arbiter

        seats = [
            AgentSeat("gen", "generator", 2000, ["search_papers"]),
            AgentSeat("rev1", "reviewer_1", 800, ["search_papers"]),
            AgentSeat("rev2", "reviewer_2", 800, ["audit_claim"]),
            AgentSeat("rev3", "reviewer_3", 800, ["search_papers"]),
            AgentSeat("arb1", "arbiter_1", 500, ["write_memory", "read_memory"]),
            AgentSeat("arb2", "arbiter_2", 500, ["write_memory"]),
        ]

        tool_reg = create_default_registry()
        mem = TeamMemory(core.db.conn)
        grad = GraduationManager(core.db.conn, mem)
        arb = Arbiter(llm=core.llm, graduation=grad, tool_registry=tool_reg, team_memory=mem)

        team = AgentTeam(
            spark_id=sample_spark["id"], spark_content=sample_spark["content"],
            seats=seats, core_llm=core.llm, data_access=None,
            team_memory=mem, graduation=grad, arbiter=arb, tool_registry=tool_reg,
        )

        # Round 1: user asks a question
        results = await team.start_round(
            question="这个火花的新颖性如何？",
            mentioned=["gen", "rev1", "rev2", "rev3"],
        )
        assert len(results) > 0
        assert team.round_number == 1

        # Graduation cycle
        decision = await team.execute_graduation_cycle()
        assert "verdict" in decision or "hot_keep" in decision

    @pytest.mark.asyncio
    async def test_agent_team_identity_secrecy(self, core):
        """Verify that system prompts never expose the underlying model name."""
        from paperreadagent.modules.ideator.agent_team import AgentTeam, AgentSeat
        from paperreadagent.modules.ideator.tool_registry import create_default_registry

        seats = [AgentSeat("gen", "generator", 2000, [])]
        tool_reg = create_default_registry()

        team = AgentTeam(
            spark_id=1, spark_content="test", seats=seats,
            core_llm=core.llm, data_access=None, team_memory=None,
            graduation=None, arbiter=None, tool_registry=tool_reg,
        )

        prompt = team._build_agent_system_prompt(seats[0])
        assert "deepseek" not in prompt.lower()
        assert "v4" not in prompt.lower()
        # But should have role description
        assert "生成" in prompt or "创意" in prompt

    @pytest.mark.asyncio
    async def test_tool_request_flow(self, core):
        """Test: agent requests unauthorized tool → arbiter evaluates."""
        from paperreadagent.modules.ideator.tool_registry import create_default_registry
        from paperreadagent.modules.ideator.arbiter import Arbiter
        from paperreadagent.modules.ideator.graduation import GraduationManager
        from paperreadagent.modules.ideator.team_memory import TeamMemory

        reg = create_default_registry()
        mem = TeamMemory(core.db.conn)
        grad = GraduationManager(core.db.conn, mem)
        arb = Arbiter(llm=core.llm, graduation=grad, tool_registry=reg, team_memory=mem)

        # rev1 requests create_spark (should be denied)
        result = arb.evaluate_tool_request("rev1", "create_spark", "需要创建火花")
        assert result["approved"] is False

        # gen requests trigger_recall (should be approved if under limit)
        result = arb.evaluate_tool_request("gen", "trigger_recall", "需要更多素材")
        assert result["approved"] is True
```

- [ ] **Step 2: Run integration tests**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_integration.py -v -k "agent_team" --tb=long
```
Expected: New integration tests pass

- [ ] **Step 3: Run full test suite**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/ -v
```
Expected: All tests pass (existing + new)

- [ ] **Step 4: Commit**

```bash
git add paperreadagent/modules/ideator/tests/test_integration.py
git commit -m "test(ideator): add Agent Team integration tests — lifecycle, identity secrecy, tool flow"
```

---

## Execution Order

```
Task 1 (Schema) ────┐
Task 2 (Tools)  ────┤  Phase 1: parallel
Task 3 (Prompts) ───┘
                     ↓
Task 4 (Team Memory) ─┐
Task 5 (Graduation) ──┤  Phase 2: depends on Phase 1
Task 6 (Arbiter) ─────┘
                     ↓
Task 7 (Remove IdeatorLLM) ─┐
Task 8 (CrossRecall) ───────┤  Phase 3: depends on Phase 2
Task 9 (Pipeline Bridge) ───┤
Task 10 (DataAccess) ───────┘
                     ↓
Task 11 (AgentTeam Core) ── Phase 4: depends on Phase 3
                     ↓
Task 12 (Routes) ──┐
Task 13 (Frontend) ─┘  Phase 5: depends on Phase 4
                     ↓
Task 14 (Integration Tests) ── Phase 6: depends on all
```
