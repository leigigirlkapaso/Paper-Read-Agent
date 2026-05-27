# AgentTeam 接入圆桌实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 AgentTeam 替代 RoundtableSession，完整接入前端圆桌讨论流程。

**Architecture:** 6 个独立任务，自底向上：坐席工厂 → 工具对等 → AgentTeamManager → __init__ 组装 → 路由切换 → 前端重写。

**Tech Stack:** Python asyncio, FastAPI, Jinja2, Vanilla JS (Alpine.js compat), Tailwind CSS

---

### Task 1: `create_default_seats()` + 来源上下文注入

**Files:**
- Modify: `paperreadagent/modules/ideator/agent_team.py`
- Modify: `paperreadagent/modules/ideator/tests/test_agent_team.py`

- [ ] **Step 1: Write tests**

在 `test_agent_team.py` 末尾追加：

```python
def test_create_default_seats_returns_6():
    from paperreadagent.modules.ideator.agent_team import create_default_seats
    seats = create_default_seats()
    assert len(seats) == 6
    seat_ids = {s.seat_id for s in seats}
    assert seat_ids == {"gen", "rev1", "rev2", "rev3", "arb1", "arb2"}

def test_create_default_seats_arbiters_have_equal_tools():
    from paperreadagent.modules.ideator.agent_team import create_default_seats
    seats = create_default_seats()
    arb1 = next(s for s in seats if s.seat_id == "arb1")
    arb2 = next(s for s in seats if s.seat_id == "arb2")
    assert set(arb1.tools) == set(arb2.tools)

def test_create_default_seats_quotas():
    from paperreadagent.modules.ideator.agent_team import create_default_seats
    seats = create_default_seats()
    gen = next(s for s in seats if s.seat_id == "gen")
    assert gen.quota == 2000
    for rev in [s for s in seats if s.seat_id.startswith("rev")]:
        assert rev.quota == 800

def test_agent_system_prompt_includes_source_context():
    from paperreadagent.modules.ideator.agent_team import AgentTeam
    from unittest.mock import MagicMock

    mock_llm = MagicMock()
    mock_llm.load_prompt = MagicMock(return_value="identity prompt")
    mock_memory = MagicMock()
    mock_memory.format_for_context = MagicMock(return_value="memory text")
    mock_graduation = MagicMock()
    mock_graduation.layers = {}
    mock_arbiter = MagicMock()

    team = AgentTeam(
        spark_id=1, spark_content="test spark",
        seats=[], llm=mock_llm,
        team_memory=mock_memory,
        graduation=mock_graduation,
        arbiter=mock_arbiter,
        tool_registry=None,
        source_context="标题: Test\n摘要: Abstract text",
    )

    from paperreadagent.modules.ideator.agent_team import AgentSeat
    seat = AgentSeat(seat_id="gen", role="generator", quota=2000, tools=[])
    prompt = team._build_agent_system_prompt(seat)
    assert "Test" in prompt
    assert "Abstract text" in prompt
    assert "memory text" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_agent_team.py::test_create_default_seats_returns_6 paperreadagent/modules/ideator/tests/test_agent_team.py::test_create_default_seats_arbiters_have_equal_tools paperreadagent/modules/ideator/tests/test_agent_team.py::test_create_default_seats_quotas paperreadagent/modules/ideator/tests/test_agent_team.py::test_agent_system_prompt_includes_source_context -v
```

预期：FAIL — `create_default_seats` 不存在，`source_context` 参数不存在。

- [ ] **Step 3: Implement `create_default_seats()`**

在 `agent_team.py` 的 `AgentSeat` dataclass 之后、`AgentTeam` 类之前添加：

```python
def create_default_seats() -> list[AgentSeat]:
    """工厂方法：6 坐席默认配置，Arb1/Arb2 工具对等。"""
    return [
        AgentSeat("gen",  "generator",   2000, [
            "search_papers", "read_paper", "read_note",
            "create_spark", "update_spark", "check_duplicate",
            "trigger_recall", "fetch_snapshot", "read_memory",
        ]),
        AgentSeat("rev1", "reviewer_1",   800, [
            "search_papers", "read_paper", "fetch_snapshot", "read_memory",
        ]),
        AgentSeat("rev2", "reviewer_2",   800, [
            "search_papers", "read_paper", "audit_claim",
            "fetch_snapshot", "read_memory",
        ]),
        AgentSeat("rev3", "reviewer_3",   800, [
            "search_papers", "read_paper", "read_note",
            "fetch_snapshot", "read_memory",
        ]),
        AgentSeat("arb1", "arbiter_1",    500, [
            "write_memory", "read_memory", "fetch_snapshot",
            "report_watermark", "adjust_quota", "grant_tool",
        ]),
        AgentSeat("arb2", "arbiter_2",    500, [
            "write_memory", "read_memory", "fetch_snapshot",
            "report_watermark", "adjust_quota", "grant_tool",
        ]),
    ]
```

- [ ] **Step 4: Implement `source_context` parameter in AgentTeam**

修改 `AgentTeam.__init__` 签名，添加 `source_context: str = ""`：

```python
def __init__(self, *, spark_id, spark_content, seats, llm,
             team_memory, graduation, arbiter, tool_registry,
             source_context: str = ""):
    ...
    self._source_context = source_context
```

修改 `_build_agent_system_prompt`，在团队记忆之前注入来源上下文：

```python
def _build_agent_system_prompt(self, seat: AgentSeat) -> str:
    identity_prompt = self._llm.load_prompt(
        "ideator", f"agent_identity_{seat.seat_id}",
        tools_list=self._format_tools_for_seat(seat),
    )
    memory_text = self._memory.format_for_context(self.spark_id) if self._memory else ""
    source_block = f"\n\n---\n来源上下文:\n{self._source_context}" if self._source_context else ""
    return f"{identity_prompt}\n\n---\n火花内容:\n{self.spark_content}{source_block}\n\n---\n团队记忆:\n{memory_text}"
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_agent_team.py -v
```

预期：全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add paperreadagent/modules/ideator/agent_team.py paperreadagent/modules/ideator/tests/test_agent_team.py
git commit -m "feat(agent_team): add create_default_seats() factory + source_context injection"
```

---

### Task 2: Arb1/Arb2 工具对等

**Files:**
- Modify: `paperreadagent/modules/ideator/tool_registry.py`
- Modify: `paperreadagent/modules/ideator/tests/test_tool_registry.py`

- [ ] **Step 1: Write test**

在 `test_tool_registry.py` 末尾追加：

```python
def test_arb1_arb2_have_equal_tools():
    from paperreadagent.modules.ideator.tool_registry import create_default_registry
    reg = create_default_registry()
    arb1_tools = set(t["name"] for t in reg.list_for_role("arb1"))
    arb2_tools = set(t["name"] for t in reg.list_for_role("arb2"))
    assert arb1_tools == arb2_tools
    assert "grant_tool" in arb1_tools
    assert "adjust_quota" in arb2_tools
```

- [ ] **Step 2: Run test — fail**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_tool_registry.py::test_arb1_arb2_have_equal_tools -v
```

预期：FAIL — Arb1 和 Arb2 工具数量不同。

- [ ] **Step 3: Update `create_default_registry()`**

在 `tool_registry.py` 的 `create_default_registry()` 中，对 `write_memory`、`report_watermark`、`adjust_quota`、`grant_tool` 的 `default_roles` 加上 `"arb2"`：

```python
reg.register(ToolDefinition("write_memory", "...", ["arb1", "arb2"], {...}))
reg.register(ToolDefinition("report_watermark", "...", ["arb1", "arb2"], {}))
reg.register(ToolDefinition("adjust_quota", "...", ["arb1", "arb2"], {...}))
reg.register(ToolDefinition("grant_tool", "...", ["arb1", "arb2"], {...}))
```

- [ ] **Step 4: Run test — pass**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_tool_registry.py -v
```

预期：全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/tool_registry.py paperreadagent/modules/ideator/tests/test_tool_registry.py
git commit -m "fix(tool_registry): give arb1 and arb2 equal tool sets"
```

---

### Task 3: AgentTeamManager — 多团队管理器

**Files:**
- Modify: `paperreadagent/modules/ideator/agent_team.py` (追加）

在 `agent_team.py` 末尾追加 `AgentTeamManager` 类：

```python
class AgentTeamManager:
    """管理多个 AgentTeam 实例，替代 RoundtableManager。"""

    def __init__(self, *, llm, data_access,
                 tool_registry, team_memory, graduation, arbiter):
        self._llm = llm
        self._data = data_access
        self._tool_registry = tool_registry
        self._team_memory = team_memory
        self._graduation = graduation
        self._arbiter = arbiter
        self._teams: dict[int, AgentTeam] = {}  # rt_id → team

    def create_team(self, *, spark_id: int, spark_content: str,
                    source_refs: list[dict]) -> int:
        """创建 AgentTeam 并分配 roundtable_id。"""
        rt_id = self._data.insert_roundtable(spark_id=spark_id)

        # 组装来源上下文
        source_context = self._resolve_source_context(spark_id, source_refs)

        seats = create_default_seats()
        team = AgentTeam(
            spark_id=spark_id,
            spark_content=spark_content,
            seats=seats,
            llm=self._llm,
            team_memory=self._team_memory,
            graduation=self._graduation,
            arbiter=self._arbiter,
            tool_registry=self._tool_registry,
            source_context=source_context,
        )
        self._teams[rt_id] = team
        return rt_id

    def get_team(self, rt_id: int) -> AgentTeam | None:
        return self._teams.get(rt_id)

    def close_team(self, rt_id: int) -> None:
        team = self._teams.pop(rt_id, None)
        if team:
            self._data.update_roundtable(rt_id, status="closed")

    def _resolve_source_context(self, spark_id: int,
                                 source_refs: list[dict]) -> str:
        """加载 spark 的来源原文、审查记录、深化内容。"""
        parts = []

        # 来源论文/笔记
        for ref in (source_refs or []):
            ref_type = ref.get("type", "")
            ref_id = ref.get("id", 0)
            try:
                if ref_type == "paper":
                    paper = self._data.get_paper(ref_id)
                    if paper:
                        parts.append(
                            f"## 论文: {paper.get('title', '')}\n"
                            f"{paper.get('abstract', '')}"
                        )
                        # 用户笔记
                        note = self._data.get_user_note(ref_id)
                        if note and note.get("content"):
                            parts.append(f"笔记: {note['content']}")
                elif ref_type == "core_note":
                    note = self._data._core.knowledge.get_note(ref_id)
                    if note:
                        parts.append(f"## 笔记: {note.get('content', '')}")
            except Exception:
                pass

        # 审查记录
        try:
            spark = self._data.get_spark(spark_id)
            if spark:
                if spark.get("review_status"):
                    parts.append(
                        f"审查状态: {spark.get('review_status')} "
                        f"分数: {spark.get('final_score', 'N/A')}"
                    )
                if spark.get("depth_content"):
                    parts.append(f"## 深化内容\n{spark['depth_content']}")
        except Exception:
            pass

        return "\n\n".join(parts)
```

---

### Task 4: `__init__.py` — 组装 AgentTeam 基础设施

**Files:**
- Modify: `paperreadagent/modules/ideator/__init__.py`

替换 roundtable 相关代码 —— 移除 `RoundtableManager` 导入和 `_roundtable_manager` 创建，替换为 AgentTeam 基础设施：

删除：
```python
# Roundtable manager singleton (survives across HTTP requests)
from .roundtable import RoundtableManager
from .ideator_llm import IdeatorLLM
_roundtable_manager = RoundtableManager(
    llm=IdeatorLLM(core_llm=core.llm),
    data_access=data,
)
```

添加：
```python
# AgentTeam 基础设施（替代旧 RoundtableManager）
from .ideator_llm import IdeatorLLM
from .agent_team import AgentTeamManager
from .team_memory import TeamMemory
from .graduation import GraduationManager
from .tool_registry import create_default_registry
from .arbiter import Arbiter

ideator_llm = IdeatorLLM(core_llm=core.llm)
team_memory = TeamMemory(core.db.conn)
graduation_mgr = GraduationManager(core.db.conn, team_memory)
tool_registry = create_default_registry()
arbiter = Arbiter(llm=ideator_llm, graduation=graduation_mgr,
                  tool_registry=tool_registry, team_memory=team_memory)
_roundtable_manager = AgentTeamManager(
    llm=ideator_llm, data_access=data,
    tool_registry=tool_registry, team_memory=team_memory,
    graduation=graduation_mgr, arbiter=arbiter,
)
```

---

### Task 5: `routes.py` — 圆桌 API 适配

**Files:**
- Modify: `paperreadagent/modules/ideator/routes.py`

将 `trigger_graduation` 端点中内联创建的 AgentTeam 组件替换为使用 AgentTeamManager：

```python
@router.post("/api/roundtables/{rt_id}/graduate")
async def trigger_graduation(request: Request, rt_id: int):
    """手动触发毕业决策"""
    core = request.app.state.core
    from . import get_roundtable_manager
    mgr = get_roundtable_manager()
    if not mgr:
        return JSONResponse({"error": "Manager not initialized"}, status_code=500)
    team = mgr.get_team(rt_id)
    if not team:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    try:
        decision = await team.execute_graduation_cycle(roundtable_id=rt_id)
        return JSONResponse(decision)
    except Exception:
        logger.debug("[ideator] graduation failed", exc_info=True)
        return JSONResponse({"error": "Graduation failed"}, status_code=500)
```

路由签名不需要改 —— `start_roundtable` 和 `ask_round` 已经通过 `get_roundtable_manager()` 拿到 manager，现在 manager 是 `AgentTeamManager`，接口兼容。

`start_roundtable` 改为调用 `mgr.create_team()`，`ask_round` 改为调用 `team.start_round()`。`get_roundtable` 增加返回坐席状态和上下文水位：

```python
@router.get("/api/roundtables/{rt_id}")
async def get_roundtable(request: Request, rt_id: int):
    core = request.app.state.core
    from . import get_roundtable_manager
    mgr = get_roundtable_manager()
    team = mgr.get_team(rt_id)
    if not team:
        return JSONResponse({"error": "Roundtable not found"}, status_code=404)
    seat_status = [
        {"seat_id": s.seat_id, "role": s.role,
         "quota": s.quota, "remaining": s.remaining_quota,
         "state": s.state}
        for s in team.seats.values()
    ]
    hot_pct = team._graduation.layers.get("hot", type("L", (), {"pct": 0.0})()).pct if team._graduation else 0.0
    warm_pct = team._graduation.layers.get("warm", type("L", (), {"pct": 0.0})()).pct if team._graduation else 0.0
    return JSONResponse({
        "roundtable_id": rt_id, "round_number": team.round_number,
        "messages": team.messages[-50:],
        "seats": seat_status,
        "watermark": {"hot_pct": hot_pct, "warm_pct": warm_pct},
    })
```

---

### Task 6: 前端完整重写

**Files:**
- Modify: `paperreadagent/modules/ideator/static/ideator.js`
- Modify: `paperreadagent/modules/ideator/static/ideator.css`

替换现有 `openRoundtableModal` 及相关函数。

**核心函数：**

```javascript
function openRoundtableModal(sparkId, rtId) {
    const modal = document.createElement('div');
    modal.id = 'roundtable-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50';
    modal.innerHTML = `
        <div class="rt-container bg-white rounded-lg shadow-xl w-11/12 max-w-5xl h-[92vh] flex flex-col">
            <!-- 顶部状态栏 -->
            <div class="rt-header p-3 border-b flex items-center justify-between bg-gray-50">
                <div class="flex items-center gap-4">
                    <h2 class="text-lg font-semibold text-indigo-700">#${sparkId} 圆桌讨论</h2>
                    <span id="rt-round-num" class="text-xs bg-indigo-100 text-indigo-700 px-2 py-0.5 rounded">轮次 0</span>
                </div>
                <div class="flex-1 mx-6">
                    <div id="rt-watermark" class="rt-watermark-bar h-3 bg-gray-200 rounded-full overflow-hidden flex">
                        <div id="rt-hot-bar" class="h-full bg-red-400 transition-all" style="width:0%"></div>
                        <div id="rt-warm-bar" class="h-full bg-yellow-400 transition-all" style="width:0%"></div>
                        <div id="rt-cold-bar" class="h-full bg-blue-300 transition-all" style="width:0%"></div>
                    </div>
                    <div class="flex justify-between text-xs text-gray-400 mt-1">
                        <span id="rt-hot-label">hot 0%</span>
                        <span id="rt-warm-label">warm 0%</span>
                        <span>cold</span>
                    </div>
                </div>
                <div class="flex gap-2">
                    <button onclick="supplementContext(${rtId})" class="text-xs border px-2 py-1 rounded hover:bg-gray-100">补充</button>
                    <button onclick="manualGraduate(${rtId})" class="text-xs bg-yellow-100 text-yellow-700 px-2 py-1 rounded hover:bg-yellow-200">毕业</button>
                    <button onclick="closeRoundtable(${rtId})" class="text-xs bg-red-100 text-red-600 px-2 py-1 rounded hover:bg-red-200">结束</button>
                    <button onclick="closeRoundtableModal()" class="text-gray-400 hover:text-gray-600 text-xl">&times;</button>
                </div>
            </div>

            <!-- 主体：坐席面板 + 消息区 -->
            <div class="flex flex-1 overflow-hidden">
                <!-- 左侧坐席面板 -->
                <div class="rt-seats-panel w-48 border-r bg-gray-50 p-2 overflow-y-auto flex flex-col gap-1">
                    ${['gen:生成者:green','rev1:审查α:blue','rev2:审查β:blue','rev3:审查γ:blue','arb1:仲裁α:purple','arb2:仲裁β:purple'].map(s => {
                        const [id, name, color] = s.split(':');
                        const c = {green:'#10b981',blue:'#3b82f6',purple:'#8b5cf6'}[color];
                        return `<div class="rt-seat-card p-2 rounded border text-xs" style="border-left:3px solid ${c}" id="rt-seat-${id}">
                            <div class="font-semibold">${name}</div>
                            <div class="text-gray-400">${id}</div>
                            <div class="rt-quota-bar h-1 bg-gray-200 rounded mt-1"><div class="h-full bg-gray-500 rounded" style="width:100%"></div></div>
                            <div class="text-gray-400 mt-1">配额 100%</div>
                        </div>`;
                    }).join('')}
                </div>

                <!-- 消息区 -->
                <div id="rt-messages" class="flex-1 overflow-y-auto p-4 space-y-3">
                    <div class="text-center text-gray-400 py-8">圆桌已启动，输入问题开始讨论</div>
                </div>

                <!-- 右侧面板（可收起） -->
                <div id="rt-side-panel" class="w-56 border-l bg-gray-50 p-2 overflow-y-auto text-xs hidden">
                    <div class="font-semibold mb-2">团队记忆</div>
                    <div id="rt-memory-content" class="text-gray-500">加载中...</div>
                </div>
            </div>

            <!-- 底部操作栏 -->
            <div class="rt-footer p-3 border-t flex gap-2">
                <select id="rt-mention-select" class="border rounded px-2 py-1 text-xs">
                    <option value="all">@所有人</option>
                    <option value="gen">@生成者</option>
                    <option value="rev1">@审查α</option>
                    <option value="rev2">@审查β</option>
                    <option value="rev3">@审查γ</option>
                    <option value="arb1">@仲裁α</option>
                    <option value="arb2">@仲裁β</option>
                </select>
                <input id="rt-question-input" type="text" placeholder="输入你的问题..." class="flex-1 border rounded px-3 py-2 text-sm" onkeydown="if(event.key==='Enter')askRoundtable(${rtId})">
                <button onclick="askRoundtable(${rtId})" class="bg-indigo-600 text-white px-4 py-2 rounded text-sm hover:bg-indigo-700">提问</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    window._rtId = rtId;
    window._rtSparkId = sparkId;
    pollRoundtableState(rtId);
}
```

**消息渲染：**

```javascript
function renderRoundMessages(messages) {
    const msgArea = document.getElementById('rt-messages');
    msgArea.innerHTML = messages.map(m => {
        if (m.message_type === 'divergence_report') {
            return `<div class="rt-msg-divergence bg-yellow-50 border-l-2 border-yellow-400 rounded px-3 py-2 text-sm">⚡ ${escapeHtml(m.content)}</div>`;
        }
        if (m.message_type === 'interjection') {
            return `<div class="rt-msg-interjection text-gray-400 text-xs italic px-3 py-1">⚡ ${m.sender_name}: ${escapeHtml(m.content)}</div>`;
        }
        if (m.sender_type === 'user') {
            return `<div class="flex justify-end"><span class="rt-msg-user bg-indigo-100 rounded px-3 py-2 text-sm max-w-md">${escapeHtml(m.content)}</span></div>`;
        }
        const colors = {gen:'#10b981',rev1:'#3b82f6',rev2:'#3b82f6',rev3:'#3b82f6',arb1:'#8b5cf6',arb2:'#8b5cf6'};
        const color = colors[m.sender_name] || '#6b7280';
        return `<div class="rt-msg-model rounded px-3 py-2 text-sm" style="border-left:3px solid ${color}">
            <span class="font-semibold text-xs" style="color:${color}">${m.sender_name}</span>
            <div>${escapeHtml(m.content)}</div>
        </div>`;
    }).join('');
    msgArea.scrollTop = msgArea.scrollHeight;
}
```

**轮询状态：**

```javascript
async function pollRoundtableState(rtId) {
    try {
        const resp = await fetch(`/ideator/api/roundtables/${rtId}`);
        const data = await resp.json();
        document.getElementById('rt-round-num').textContent = `轮次 ${data.round_number}`;
        // 水位
        if (data.watermark) {
            const hp = data.watermark.hot_pct || 0;
            const wp = data.watermark.warm_pct || 0;
            document.getElementById('rt-hot-bar').style.width = hp + '%';
            document.getElementById('rt-warm-bar').style.width = wp + '%';
            document.getElementById('rt-hot-label').textContent = `hot ${hp.toFixed(0)}%`;
            document.getElementById('rt-warm-label').textContent = `warm ${wp.toFixed(0)}%`;
        }
        // 坐席状态
        if (data.seats) {
            for (const s of data.seats) {
                const card = document.getElementById('rt-seat-' + s.seat_id);
                if (card) {
                    const pct = s.quota > 0 ? (s.remaining / s.quota * 100).toFixed(0) : 0;
                    card.querySelector('.rt-quota-bar div').style.width = pct + '%';
                    card.querySelector('.rt-quota-bar + div').textContent = `配额 ${pct}%`;
                }
            }
        }
        // 消息
        if (data.messages) {
            renderRoundMessages(data.messages);
        }
    } catch (e) {
        console.error('Poll failed', e);
    }
}
```

**CSS 新增样式（ideator.css）：**

```css
/* 圆桌容器 */
.rt-container { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }

/* 水位进度条 */
.rt-watermark-bar { position: relative; }
.rt-watermark-bar > div { transition: width 0.5s ease; }

/* 坐席卡片 */
.rt-seat-card { cursor: pointer; transition: background 0.15s; }
.rt-seat-card:hover { background: #fff; }
.rt-seat-card .rt-quota-bar div { transition: width 0.3s ease; }

/* 消息气泡 */
.rt-msg-model, .rt-msg-user, .rt-msg-divergence, .rt-msg-interjection {
    animation: rt-fade-in 0.2s ease;
}
@keyframes rt-fade-in {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
}
.rt-msg-interjection { max-width: 80%; }
```

---

### Task 7: 全量测试验证

- [ ] **Step 1: 运行所有测试**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/ -v
```

预期：全部 PASS。

- [ ] **Step 2: 运行项目全量测试**

```bash
uv run python -m pytest paperreadagent/tests/ -v
```

预期：全部 PASS。
