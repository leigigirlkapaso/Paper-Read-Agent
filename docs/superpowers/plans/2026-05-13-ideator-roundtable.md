# Ideator 火花深度讨论圆桌 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 ideator 模块增加多模型实时圆桌讨论——6 模型坐席、@点名提问、并行回答+插话、角色上下文分层、自压缩退场、分歧分析。

**Architecture:** 新增 roundtable.py 圆桌引擎（编排 6 模型对话、token 监控、上下文组装）、roundtable_modal.html + roundtable.js（聊天弹窗 UI）、4 个 prompt 模板。Schema v3 迁移新增 3 张表。路由层新增 6 个 API 端点。依赖已完成的跨模型审查升级（ideator_llm / reviewer / auditor）。

**Tech Stack:** Python (asyncio, openai), SQLite, Jinja2, FastAPI, Tailwind CSS, vanilla JS

---

### 依赖顺序

```
Task 1: Schema v3          ──┐
Task 2: Prompts (4 files)  ──┤
Task 3: Roundtable Engine   ──┼──→ Task 4: Data Access
                             │    Task 5: Routes
                             │    Task 6: Frontend (HTML+JS+CSS)
                             │    Task 7: Dashboard Integration
                             │    Task 8: Tests
```

---

### Task 1: Schema v3 迁移

**Files:**
- Modify: `paperreadagent/modules/ideator/schema.py`

- [ ] **Step 1: 更新 LATEST_VERSION 并添加 MIGRATIONS[3]**

```python
LATEST_VERSION = 3

MIGRATIONS = {
    1: """...""",  # 不变
    2: """...""",  # 不变
    3: """
CREATE TABLE IF NOT EXISTS ideator_roundtables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spark_id INTEGER NOT NULL REFERENCES ideator_sparks(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','closed')),
    participants TEXT NOT NULL DEFAULT '[]',
    round_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS ideator_roundtable_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    roundtable_id INTEGER NOT NULL REFERENCES ideator_roundtables(id),
    round_number INTEGER NOT NULL DEFAULT 1,
    sender_type TEXT NOT NULL CHECK(sender_type IN ('user','model','system')),
    sender_name TEXT NOT NULL,
    sender_role TEXT,
    message_type TEXT NOT NULL CHECK(message_type IN ('question','answer','interjection','compression','exit_statement','divergence_report')),
    content TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    mentioned_by TEXT NOT NULL DEFAULT '[]',
    parent_id INTEGER REFERENCES ideator_roundtable_messages(id),
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rt_msg_roundtable ON ideator_roundtable_messages(roundtable_id, round_number);

CREATE TABLE IF NOT EXISTS ideator_roundtable_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    roundtable_id INTEGER NOT NULL REFERENCES ideator_roundtables(id),
    message_id INTEGER REFERENCES ideator_roundtable_messages(id),
    model_name TEXT NOT NULL,
    model_role TEXT NOT NULL,
    round_number INTEGER NOT NULL DEFAULT 1,
    prompt_sent TEXT NOT NULL DEFAULT '',
    raw_response TEXT NOT NULL DEFAULT '',
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    tokens_total INTEGER NOT NULL DEFAULT 0,
    token_pct_used REAL NOT NULL DEFAULT 0.0,
    compression_triggered INTEGER NOT NULL DEFAULT 0,
    compression_summary TEXT NOT NULL DEFAULT '',
    exit_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_rt_snap_roundtable ON ideator_roundtable_snapshots(roundtable_id);

ALTER TABLE ideator_sparks ADD COLUMN roundtable_id INTEGER;
""",
}
```

- [ ] **Step 2: 运行测试确认无回归**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_schema.py -v
```

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/schema.py
git commit -m "feat(ideator): add schema v3 migration with roundtables, messages, snapshots tables"
```

---

### Task 2: Roundtable Prompt 模板

**Files:**
- Create: `paperreadagent/modules/ideator/prompts/roundtable_model_answer.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/compress_history.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/exit_statement.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/divergence_scan.jinja2`

- [ ] **Step 1: 创建 roundtable_model_answer.jinja2**

```jinja2
你是"{{ model_name }}"（{{ model_role }}），正在参加一场研究火花圆桌讨论。

## 你的身份
{{ role_description }}

## 火花内容
{{ spark_content }}

{% if context_papers %}
## 源论文
{{ context_papers }}
{% endif %}

{% if context_reports %}
## AI 精读报告
{{ context_reports }}
{% endif %}

{% if context_notes %}
## 用户笔记
{{ context_notes }}
{% endif %}

{% if context_reviews %}
## 管道审查记录
{{ context_reviews }}
{% endif %}

{% if context_deepen %}
## 深化结果
{{ context_deepen }}
{% endif %}

## 讨论历史
{{ discussion_history }}

## 当前提问
用户（{{ mentioned_by_str }}）提问：{{ question }}

## 回答要求
- 基于你的角色视角回答
- 引用上下文中的具体证据
- 如果与之前讨论有分歧，明确指出
- 直接、简洁、有针对性
```

- [ ] **Step 2: 创建 compress_history.jinja2**

```jinja2
你正在进行一场研究火花圆桌讨论。由于讨论较长，需要将历史压缩为摘要。

## 讨论历史
{{ history }}

## 压缩要求
将以上讨论历史压缩为简洁摘要。保留：
1. 每轮的关键问题
2. 各模型的核心论点
3. 出现的分歧和共识
4. 引用的证据和原文出处

摘要长度不超过原文的 30%。只输出摘要文本，不要格式。
```

- [ ] **Step 3: 创建 exit_statement.jinja2**

```jinja2
你即将因 {{ exit_reason }} 退出圆桌讨论。请在退场前发表离场声明。

## 你的模型：{{ model_name }}
## 你的角色：{{ model_role }}

## 讨论历史
{{ history }}

## 离场声明要求
1. 总结你在本次讨论中的核心立场（1-2句）
2. 指出你未能充分回应的问题（如有）
3. 建议用户后续关注什么

简洁有力，不超过 300 字。
```

- [ ] **Step 4: 创建 divergence_scan.jinja2**

```jinja2
扫描以下圆桌讨论的一轮对话，识别模型之间的分歧和共识。

## 本轮消息
{% for msg in round_messages %}
### {{ msg.sender_name }}（{{ msg.sender_role }}）
{{ msg.content }}
{% endfor %}

## 分析要求
返回 JSON：
{
  "divergences": [
    {"topic": "分歧主题", "model_a": "模型A", "model_b": "模型B", "intensity": "minor|moderate|major", "detail": "具体分歧内容"}
  ],
  "consensus": [
    {"topic": "共识主题", "models": ["模型A","模型B"], "detail": "共识内容"}
  ]
}

只输出 JSON，不要其他文字。
```

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/prompts/roundtable_model_answer.jinja2 \
        paperreadagent/modules/ideator/prompts/compress_history.jinja2 \
        paperreadagent/modules/ideator/prompts/exit_statement.jinja2 \
        paperreadagent/modules/ideator/prompts/divergence_scan.jinja2
git commit -m "feat(ideator): add roundtable prompts for answer, compression, exit, divergence"
```

---

### Task 3: Roundtable 引擎

**Files:**
- Create: `paperreadagent/modules/ideator/roundtable.py`
- Test: `paperreadagent/modules/ideator/tests/test_roundtable.py`

- [ ] **Step 1: 编写测试**

```python
"""tests for roundtable engine"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from modules.ideator.roundtable import (
    RoundtableManager, RoundtableSession, SEATS, ROLE_DESCRIPTIONS,
    CONTEXT_SPEC, TokenTracker,
)


class TestTokenTracker:
    def test_token_tracker_init(self):
        tt = TokenTracker(limit=1000000)
        assert tt.limit == 1000000
        assert tt.used == 0
        assert tt.compression_count == 0

    def test_consuming_tokens(self):
        tt = TokenTracker(limit=1000000)
        tt.consume(300000)
        assert tt.used == 300000
        assert tt.pct_used == pytest.approx(0.30)

    def test_needs_compression_at_50pct(self):
        tt = TokenTracker(limit=1000000)
        tt.consume(500000)
        assert tt.needs_compression() is True

    def test_needs_warning_at_85pct(self):
        tt = TokenTracker(limit=1000000)
        tt.consume(850000)
        assert tt.needs_warning() is True

    def test_is_exhausted_at_100pct(self):
        tt = TokenTracker(limit=1000000)
        tt.consume(1000000)
        assert tt.is_exhausted() is True

    def test_compression_resets_used(self):
        tt = TokenTracker(limit=1000000)
        tt.consume(500000)
        tt.compression_count += 1
        # 压缩后 used 重置为摘要估算值（约原历史的 25%）
        tt.used = 125000
        assert tt.needs_compression() is False


class TestRoundtableSession:
    @pytest.fixture
    def mock_llm(self):
        llm = MagicMock()
        llm.chat = AsyncMock(return_value="mock response")
        llm.model_for = MagicMock(side_effect=lambda r: r)
        return llm

    @pytest.fixture
    def session(self, mock_llm):
        return RoundtableSession(
            spark_id=1,
            spark_content="test spark content",
            llm=mock_llm,
            data_access=MagicMock(),
            context_bundles={
                "gen": {"papers": "...", "reports": "...", "notes": "...",
                        "reviews": "...", "deepen": "...", "self_score": 0.7},
                "rev": {"papers": "...", "reports": "...", "notes": "...",
                        "reviews": "...", "deepen": "..."},
                "arb": {"reviews": "...", "deepen": "..."},
            },
        )

    def test_session_has_6_seats(self, session):
        assert len(session.participants) == 6
        names = [p["name"] for p in session.participants]
        assert "deepseek-gen" in names
        assert "deepseek-rev3" in names
        assert "gemini-flash-rev1" in names
        assert "qwen-rev2" in names
        assert "opus-arb1" in names
        assert "gpt5.5-arb2" in names

    def test_gen_has_full_context(self, session):
        gen = [p for p in session.participants if p["seat_id"] == "gen"][0]
        assert gen["context"]["papers"] is not None
        assert gen["context"]["reports"] is not None
        assert gen["context"]["notes"] is not None

    def test_arb_has_no_papers(self, session):
        arb = [p for p in session.participants if p["role"] == "arbiter"][0]
        assert "papers" not in arb["context"] or arb["context"].get("papers") is None

    def test_rev_has_no_self_score(self, session):
        rev1 = [p for p in session.participants if p["seat_id"] == "rev1"][0]
        assert "self_score" not in rev1["context"] or rev1["context"].get("self_score") is None

    @pytest.mark.asyncio
    async def test_ask_round_parallel_responses(self, session):
        results = await session.ask_round(
            question="test question",
            mentioned=["gen", "rev1"],
        )
        assert len(results) >= 2  # 至少两个被指名的回答
        answers = [r for r in results if r["type"] == "answer"]
        assert len(answers) == 2

    @pytest.mark.asyncio
    async def test_interjection_limit_150_chars(self, session):
        session.participants[2]["can_interject"] = True
        result = await session._collect_interjections(
            mentioned=[], round_number=1,
        )
        for r in result:
            if r["type"] == "interjection":
                assert len(r["content"]) <= 150

    @pytest.mark.asyncio
    async def test_force_remove_generates_exit_statement(self, session):
        target = session.participants[2]
        exit_msg = await session.force_remove(target["seat_id"], reason="user_forced")
        assert exit_msg["message_type"] == "exit_statement"
        assert target["state"] == "exited"

    def test_compress_context(self, session):
        target = session.participants[1]
        target["token_tracker"].consume(500000)
        assert target["token_tracker"].needs_compression() is True
        # 压缩逻辑在 ask_round 中自动触发


class TestRoundtableManager:
    @pytest.fixture
    def manager(self):
        return RoundtableManager(llm=MagicMock(), data_access=MagicMock())

    def test_start_roundtable_creates_db_record(self, manager):
        manager._data.insert_roundtable = MagicMock(return_value=1)
        rt_id = manager.start(spark_id=1, spark_content="test",
                              source_refs=[{"type": "paper", "id": 1}])
        assert rt_id == 1

    def test_pause_and_resume(self, manager):
        manager._data.update_roundtable = MagicMock()
        manager.pause(1)
        manager._data.update_roundtable.assert_called_with(1, status="paused")
        manager.resume(1)
        manager._data.update_roundtable.assert_called_with(1, status="active")

    def test_close_generates_final_divergence_report(self, manager):
        manager._data.get_messages = MagicMock(return_value=[])
        manager._data.insert_message = MagicMock()
        manager._data.update_roundtable = MagicMock()
        manager.close(1)
        manager._data.update_roundtable.assert_called_with(1, status="closed")
```

- [ ] **Step 2: 运行测试确认 FAIL**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_roundtable.py -v
```

- [ ] **Step 3: 实现 roundtable.py**

模块结构：

```python
# 坐席定义
SEATS = [
    {"seat_id": "gen",     "model": "deepseek-v4-pro",        "role": "generator",    "token_limit": 1_000_000},
    {"seat_id": "rev3",    "model": "deepseek-v4-pro",        "role": "reviewer_3",   "token_limit": 1_000_000},
    {"seat_id": "rev1",    "model": "gemini-3-flash-preview", "role": "reviewer_1",   "token_limit": None},
    {"seat_id": "rev2",    "model": "qwen3.6-plus",           "role": "reviewer_2",   "token_limit": None},
    {"seat_id": "arb1",    "model": "claude-opus-4-7-max",    "role": "arbiter_1",    "token_limit": 1_000_000},
    {"seat_id": "arb2",    "model": "gpt-5.5-2026-04-24",     "role": "arbiter_2",    "token_limit": None},
]

ROLE_DESCRIPTIONS = {
    "generator":  "你是这个研究火花的创造者。你了解火花的来龙去脉，可以辩护你的推理，也可以承认错误。",
    "reviewer_1": "你是独立审查者。从新颖性、证据支撑度、可行性三个维度评估火花。",
    "reviewer_2": "你是独立审查者+审计者。侧重验证火花的 claim 是否被源文本支撑。",
    "reviewer_3": "你是独立审查者。你与生成者使用同一模型但独立实例，不带创作偏见。",
    "arbiter_1":  "你是资深仲裁者。在审查者产生分歧时给出终裁决。你是圆桌中除用户外最高权力的角色。",
    "arbiter_2":  "你是资深仲裁者+深化者。侧重深度扩展和高价值方向探索。",
}

CONTEXT_SPEC = {
    "generator":   ["papers", "reports", "notes", "reviews", "deepen", "self_score"],
    "reviewer_1":  ["papers", "reports", "notes", "reviews", "deepen"],
    "reviewer_2":  ["papers", "reports", "notes", "reviews", "deepen"],
    "reviewer_3":  ["papers", "reports", "notes", "reviews", "deepen"],
    "arbiter_1":   ["reviews", "deepen"],
    "arbiter_2":   ["reviews", "deepen"],
}


class TokenTracker:
    """每个模型的独立 token 追踪器"""
    def __init__(self, limit: int | None):
        self.limit = limit or 128000
        self.used = 0
        self.compression_count = 0

    @property
    def pct_used(self) -> float:
        return self.used / self.limit if self.limit else 0.0

    def consume(self, tokens: int) -> None:
        self.used += tokens

    def needs_compression(self) -> bool:
        return self.pct_used >= 0.50

    def needs_warning(self) -> bool:
        return self.pct_used >= 0.85

    def is_exhausted(self) -> bool:
        return self.pct_used >= 1.0


class RoundtableSession:
    """单个圆桌会话——管理 6 坐席、对话轮次、token 监控"""

    def __init__(self, *, spark_id, spark_content, llm, data_access, context_bundles):
        self.spark_id = spark_id
        self.spark_content = spark_content
        self._llm = llm
        self._data = data_access
        self.round_number = 0
        self.messages = []  # 完整消息历史
        self.participants = self._init_participants(context_bundles)

    def _init_participants(self, bundles):
        participants = []
        for seat in SEATS:
            ctx_keys = CONTEXT_SPEC[seat["role"]]
            ctx = {k: bundles[seat["role"]].get(k) for k in ctx_keys}
            participants.append({
                **seat,
                "context": ctx,
                "token_tracker": TokenTracker(seat["token_limit"]),
                "state": "online",
                "can_interject": True,
            })
        return participants

    async def ask_round(self, *, question, mentioned) -> list[dict]:
        """执行一轮对话。返回本轮所有消息（回答 + 插话）。"""
        self.round_number += 1
        results = []

        # 1. 写入用户提问
        question_msg = self._record_message(
            sender_type="user", sender_name="user", sender_role=None,
            message_type="question", content=question,
            mentioned_by=mentioned,
        )

        # 2. 被指名模型并行回答
        tasks = []
        for p in self.participants:
            if p["state"] != "online":
                continue
            if p["seat_id"] in mentioned or "all" in mentioned:
                tasks.append(self._model_answer(p, question))
        answers = await asyncio.gather(*tasks, return_exceptions=True)
        for ans in answers:
            if isinstance(ans, Exception):
                continue
            results.append(ans)

        # 3. 未指名模型插话（限 150 字）
        interjections = await self._collect_interjections(mentioned)
        results.extend(interjections)

        # 4. 每轮结束生成分歧报告
        div_report = await self._divergence_scan()
        if div_report:
            results.append(div_report)

        return results

    async def _model_answer(self, participant, question) -> dict:
        """单个模型生成回答"""
        if participant["token_tracker"].needs_compression():
            await self._compress(participant)
        if participant["token_tracker"].is_exhausted():
            return await self._force_exit(participant, "token_exhausted")

        messages = self._build_messages(participant, question)
        raw = await self._llm.chat(model_role=participant["role"], messages=messages, temperature=0.7)
        tokens = self._estimate_tokens(messages, raw)
        participant["token_tracker"].consume(tokens)

        return self._record_message(
            sender_type="model", sender_name=participant["model"],
            sender_role=participant["seat_id"], message_type="answer",
            content=raw, tokens=tokens,
        )

    async def _collect_interjections(self, mentioned) -> list[dict]:
        """收集中插话（未指名模型，限 150 字）"""
        results = []
        for p in self.participants:
            if p["state"] != "online" or not p["can_interject"]:
                continue
            if p["seat_id"] in mentioned:
                continue
            # 插话 prompt: 简短补充，150 字以内
            raw = await self._llm.chat(
                model_role=p["role"],
                messages=[{"role": "user", "content": f"本轮讨论中，如果你有重要补充请发言（限150字）：{self._get_last_question()}"}],
                temperature=0.5,
                max_tokens=200,
            )
            content = raw[:150]
            results.append(self._record_message(
                sender_type="model", sender_name=p["model"],
                sender_role=p["seat_id"], message_type="interjection",
                content=content,
            ))
        return results

    async def force_remove(self, seat_id, reason="user_forced") -> dict:
        """用户强制移除模型"""
        p = self._find_participant(seat_id)
        if not p:
            return None
        p["state"] = "exited"
        history = self._format_history()
        prompt = self._render_prompt("exit_statement", history=history, exit_reason=reason,
                                      model_name=p["model"], model_role=p["role"])
        raw = await self._llm.chat(model_role=p["role"], messages=[{"role":"user","content":prompt}], temperature=0.5)
        return self._record_message(
            sender_type="model", sender_name=p["model"], sender_role=p["seat_id"],
            message_type="exit_statement", content=raw,
        )

    async def _compress(self, participant):
        """自压缩：模型对自己的历史做摘要"""
        history = self._format_history()
        prompt = self._render_prompt("compress_history", history=history)
        summary = await self._llm.chat(
            model_role=participant["role"],
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
        )
        participant["token_tracker"].used = self._estimate_tokens_text(summary)
        participant["token_tracker"].compression_count += 1
        self._record_message(
            sender_type="system", sender_name="system", sender_role=None,
            message_type="compression",
            content=f"{participant['model']} 已自动压缩上下文（第{participant['token_tracker'].compression_count}次）",
        )

    async def _divergence_scan(self):
        """对本轮消息做分歧分析"""
        round_msgs = [m for m in self.messages if m["round_number"] == self.round_number]
        if len(round_msgs) < 2:
            return None
        prompt = self._render_prompt("divergence_scan", round_messages=round_msgs)
        raw = await self._llm.chat(
            model_role="reviewer_1",  # gemini-flash 轻量分析
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return self._record_message(
            sender_type="system", sender_name="system", sender_role=None,
            message_type="divergence_report", content=raw,
        )

    def _record_message(self, **kwargs):
        msg = {**kwargs, "round_number": self.round_number, "created_at": datetime.now().isoformat()}
        self.messages.append(msg)
        return msg

    def _build_messages(self, participant, question):
        """组装发给模型的完整 messages 数组"""
        system_prompt = self._render_prompt("roundtable_model_answer",
            model_name=participant["model"], model_role=participant["role"],
            role_description=ROLE_DESCRIPTIONS[participant["role"]],
            spark_content=self.spark_content,
            context_papers=participant["context"].get("papers"),
            context_reports=participant["context"].get("reports"),
            context_notes=participant["context"].get("notes"),
            context_reviews=participant["context"].get("reviews"),
            context_deepen=participant["context"].get("deepen"),
            discussion_history=self._format_history(),
            question=question,
            mentioned_by_str="...",
        )
        return [{"role": "system", "content": system_prompt}]


class RoundtableManager:
    """圆桌管理器——创建/暂停/恢复/关闭会话"""

    def __init__(self, *, llm, data_access):
        self._llm = llm
        self._data = data_access
        self._sessions: dict[int, RoundtableSession] = {}

    def start(self, *, spark_id, spark_content, source_refs) -> int:
        """创建新圆桌，返回 roundtable_id"""
        bundles = self._assemble_contexts(spark_id, source_refs)
        rt_id = self._data.insert_roundtable(spark_id=spark_id)
        session = RoundtableSession(
            spark_id=spark_id, spark_content=spark_content,
            llm=self._llm, data_access=self._data,
            context_bundles=bundles,
        )
        self._sessions[rt_id] = session
        return rt_id

    def _assemble_contexts(self, spark_id, source_refs) -> dict:
        """根据角色组装三层上下文包"""
        gen = {"papers": ..., "reports": ..., "notes": ..., "reviews": ..., "deepen": ..., "self_score": ...}
        rev = {"papers": ..., "reports": ..., "notes": ..., "reviews": ..., "deepen": ...}
        arb = {"reviews": ..., "deepen": ...}
        return {"generator": gen, "reviewer_1": rev, "reviewer_2": rev, "reviewer_3": rev,
                "arbiter_1": arb, "arbiter_2": arb}
```

- [ ] **Step 4: 运行测试确认 PASS**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_roundtable.py -v
```

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/roundtable.py paperreadagent/modules/ideator/tests/test_roundtable.py
git commit -m "feat(ideator): add roundtable engine with 6-seat, token tracking, compression, exit"
```

---

### Task 4: Data Access 层更新

**Files:**
- Modify: `paperreadagent/modules/ideator/data_access.py`

- [ ] **Step 1: 新增圆桌 CRUD 方法**

```python
def insert_roundtable(self, spark_id: int) -> int:
    """创建圆桌记录，返回 ID"""
    cur = self._core.db.conn.execute(
        "INSERT INTO ideator_roundtables (spark_id) VALUES (?)", (spark_id,)
    )
    self._core.db.conn.commit()
    return cur.lastrowid

def update_roundtable(self, rt_id: int, **fields) -> None:
    """更新圆桌状态"""
    sets = [f"{k}=?" for k in fields]
    vals = list(fields.values()) + [rt_id]
    self._core.db.conn.execute(
        f"UPDATE ideator_roundtables SET {','.join(sets)} WHERE id=?",
        vals,
    )
    self._core.db.conn.commit()

def get_roundtable(self, rt_id: int) -> dict | None:
    """获取圆桌记录"""
    row = self._core.db.conn.execute(
        "SELECT * FROM ideator_roundtables WHERE id=?", (rt_id,)
    ).fetchone()
    return dict(row) if row else None

def insert_message(self, **fields) -> int:
    """插入圆桌消息"""
    ...

def get_messages(self, rt_id: int, since_round: int = 0) -> list[dict]:
    """获取圆桌消息列表"""
    ...

def insert_snapshot(self, **fields) -> int:
    """插入模型快照"""
    ...
```

- [ ] **Step 2: Commit**

```bash
git add paperreadagent/modules/ideator/data_access.py
git commit -m "feat(ideator): add roundtable CRUD methods to DataAccess"
```

---

### Task 5: Routes API 端点

**Files:**
- Modify: `paperreadagent/modules/ideator/routes.py`

- [ ] **Step 1: 新增 6 个圆桌 API**

```python
@router.post("/api/sparks/{spark_id}/roundtable/start")
async def start_roundtable(request: Request, spark_id: int):
    """发起圆桌讨论"""
    ...

@router.get("/api/roundtables/{rt_id}")
async def get_roundtable(request: Request, rt_id: int):
    """获取圆桌状态 + 坐席信息"""
    ...

@router.post("/api/roundtables/{rt_id}/ask")
async def ask_round(request: Request, rt_id: int,
                    data: AskRoundRequest):  # {question, mentioned: [str]}
    """提问一轮，返回本轮所有消息"""
    ...

@router.post("/api/roundtables/{rt_id}/remove/{seat_id}")
async def remove_seat(request: Request, rt_id: int, seat_id: str):
    """强制移除坐席"""
    ...

@router.post("/api/roundtables/{rt_id}/pause")
async def pause_roundtable(request: Request, rt_id: int):
    """暂停圆桌"""
    ...

@router.post("/api/roundtables/{rt_id}/close")
async def close_roundtable(request: Request, rt_id: int):
    """结束圆桌"""
    ...

@router.post("/api/roundtables/{rt_id}/supplement")
async def supplement_context(request: Request, rt_id: int,
                             data: SupplementRequest):  # {seat_id, content}
    """中途给指定模型补充资料"""
    ...
```

- [ ] **Step 2: Commit**

```bash
git add paperreadagent/modules/ideator/routes.py
git commit -m "feat(ideator): add 7 roundtable API endpoints"
```

---

### Task 6: 前端 — 聊天弹窗

**Files:**
- Create: `paperreadagent/modules/ideator/templates/roundtable_modal.html`
- Create: `paperreadagent/modules/ideator/static/roundtable.js`
- Create: `paperreadagent/modules/ideator/static/roundtable.css`

- [ ] **Step 1: 创建 roundtable_modal.html**（聊天弹窗模板，6 模型状态栏 + 消息区 + @ 选择标签 + 输入框 + 附资料按钮）

- [ ] **Step 2: 创建 roundtable.js**（前端交互：发起圆桌、提问、轮询新消息、渲染气泡、token 警告、强制移除、暂停/恢复）

- [ ] **Step 3: 创建 roundtable.css**（弹窗样式、气泡颜色、角色色条、状态栏）

- [ ] **Step 4: Commit**

```bash
git add paperreadagent/modules/ideator/templates/roundtable_modal.html \
        paperreadagent/modules/ideator/static/roundtable.js \
        paperreadagent/modules/ideator/static/roundtable.css
git commit -m "feat(ideator): add roundtable chat modal frontend"
```

---

### Task 7: Dashboard 集成

**Files:**
- Modify: `paperreadagent/modules/ideator/templates/dashboard.html`
- Modify: `paperreadagent/modules/ideator/static/ideator.js`

- [ ] **Step 1: dashboard.html — 火花卡片新增"发起圆桌"按钮**

- [ ] **Step 2: ideator.js — 按钮点击事件 + 弹窗触发**

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/templates/dashboard.html \
        paperreadagent/modules/ideator/static/ideator.js
git commit -m "feat(ideator): integrate roundtable button into spark dashboard cards"
```

---

### Task 8: 集成测试

**Files:**
- Modify: `paperreadagent/modules/ideator/tests/test_integration.py`（追加）

- [ ] **Step 1: 编写圆桌集成测试**

```python
class TestRoundtableIntegration:
    @pytest.mark.asyncio
    async def test_full_roundtable_flow(self):
        """完整圆桌流程：start → ask → force_remove → close"""

    @pytest.mark.asyncio
    async def test_context_hierarchy_enforced(self):
        """Gen 有 paper，Arb 没有"""

    @pytest.mark.asyncio
    async def test_token_compression_triggers(self):
        """token 到 50% 触发压缩"""

    @pytest.mark.asyncio
    async def test_parallel_answers(self):
        """被指名模型并行回答"""

    @pytest.mark.asyncio
    async def test_interjection_limit(self):
        """插话不超过 150 字"""
```

- [ ] **Step 2: 运行全部测试**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/ -v
```

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/tests/test_integration.py
git commit -m "test(ideator): add roundtable integration tests"
```

---

### 完成后验证

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/ -v
uv run python -m pytest paperreadagent/modules/thinker/tests/ -v
```
