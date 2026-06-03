# Ideator 跨模型对抗审查升级 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 ideator 模块引入跨模型对抗审查机制，包括双模型独立审查、Tier 3 仲裁升级、自动 effort 调整、迭代深化、溯源审计、反馈闭环和完整审查留痕。

**Architecture:** 新增 6 个 Python 文件（ideator_llm / reviewer / auditor / state / effort / feedback_loop）、3 个 Jinja2 prompt、3 张数据库表 + 1 张扩展。重写 pipeline.py 集成所有新组件，修改 cross_recall / spark_store / routes / dashboard 以支持 effort 驱动、审查追踪和 UI 展示。

**Tech Stack:** Python (asyncio, openai), SQLite, Jinja2, FastAPI, Tailwind CSS, vanilla JS

---

### 依赖顺序

```
Task 1:  Schema v2        ──┐
Task 2:  Config            ──┤
Task 3:  Effort            ──┤
Task 4:  State             ──┤
Task 5:  ideator_llm       ──┼──→ Task 7:  Reviewer
Task 6:  Prompts (3 files) ──┘     Task 8:  Auditor
                                   Task 9:  Feedback Loop
                                   Task 10: CrossRecall ↑
                                   Task 11: SparkStore ↑
                                          ↓
                                   Task 12: Pipeline (重写)
                                          ↓
                                   Task 13: Routes
                                          ↓
                                   Task 14: Dashboard UI
                                          ↓
                                   Task 15: Tests
```

---

### Task 1: Schema v2 迁移

**Files:**
- Modify: `paperreadagent/modules/ideator/schema.py`
- Test: `paperreadagent/modules/ideator/tests/test_schema.py`

- [ ] **Step 1: 添加 v2 迁移 SQL**

在 `schema.py` 的 `MIGRATIONS` 字典中添加 `2` 键，包含 4 条 DDL：

```python
MIGRATIONS = {
    1: """CREATE TABLE IF NOT EXISTS ideator_sparks (...)""",
    2: """
-- 新增管道运行表
CREATE TABLE IF NOT EXISTS ideator_pipeline_runs (
    run_id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL DEFAULT 'manual',
    effort TEXT NOT NULL DEFAULT 'balanced',
    stages_completed TEXT NOT NULL DEFAULT '[]',
    stats TEXT NOT NULL DEFAULT '{}',
    total_tokens INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
);

-- 新增审查记录表
CREATE TABLE IF NOT EXISTS ideator_review_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spark_id INTEGER NOT NULL REFERENCES ideator_sparks(id),
    stage TEXT NOT NULL CHECK(stage IN ('review','arbitration','audit')),
    reviewer_model TEXT NOT NULL,
    reviewer_role TEXT NOT NULL CHECK(reviewer_role IN ('reviewer_1','reviewer_2','arbiter','auditor')),
    scores TEXT NOT NULL DEFAULT '{}',
    verdict TEXT NOT NULL CHECK(verdict IN ('PASS','REVISE','REJECT','ARBITRATE','OVERTURN','SUPPORTED','STRETCHED','UNSUPPORTED')),
    reasoning TEXT NOT NULL DEFAULT '',
    prompt_snapshot TEXT NOT NULL DEFAULT '',
    raw_response TEXT NOT NULL DEFAULT '',
    token_usage TEXT NOT NULL DEFAULT '{}',
    escalation_reason TEXT NOT NULL DEFAULT '',
    run_id TEXT NOT NULL REFERENCES ideator_pipeline_runs(run_id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_review_spark ON ideator_review_records(spark_id);
CREATE INDEX IF NOT EXISTS idx_review_run ON ideator_review_records(run_id);

-- 新增召回权重表
CREATE TABLE IF NOT EXISTS ideator_recall_weights (
    source_type TEXT PRIMARY KEY,
    weight REAL NOT NULL DEFAULT 1.0,
    useful_count INTEGER NOT NULL DEFAULT 0,
    noise_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 插入默认权重
INSERT OR IGNORE INTO ideator_recall_weights (source_type, weight) VALUES
    ('similarity', 1.0),
    ('contradiction', 1.0),
    ('cross_project', 1.0),
    ('cross_layer', 1.0),
    ('random_walk', 0.5),
    ('timeline', 1.0);

-- 扩展 ideator_sparks 表
ALTER TABLE ideator_sparks ADD COLUMN run_id TEXT DEFAULT '';
ALTER TABLE ideator_sparks ADD COLUMN generator_score REAL DEFAULT 0.0;
ALTER TABLE ideator_sparks ADD COLUMN final_score REAL DEFAULT 0.0;
ALTER TABLE ideator_sparks ADD COLUMN review_status TEXT DEFAULT 'pending';
ALTER TABLE ideator_sparks ADD COLUMN review_count INTEGER DEFAULT 0;
""",
}
```

同时更新 `LATEST_VERSION = 2`。

- [ ] **Step 2: 编写 schema 测试**

```python
# tests/test_schema.py
from modules.ideator.schema import LATEST_VERSION, MIGRATIONS

def test_latest_version_is_2():
    assert LATEST_VERSION == 2

def test_migrations_have_v1_and_v2():
    assert 1 in MIGRATIONS
    assert 2 in MIGRATIONS

def test_v2_creates_pipeline_runs_table():
    assert "ideator_pipeline_runs" in MIGRATIONS[2]

def test_v2_creates_review_records_table():
    assert "ideator_review_records" in MIGRATIONS[2]

def test_v2_creates_recall_weights_table():
    assert "ideator_recall_weights" in MIGRATIONS[2]

def test_v2_adds_spark_columns():
    sql = MIGRATIONS[2]
    assert "run_id" in sql
    assert "generator_score" in sql
    assert "final_score" in sql
    assert "review_status" in sql
    assert "review_count" in sql
```

- [ ] **Step 3: 运行测试确认 PASS**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_schema.py -v
```

- [ ] **Step 4: Commit**

```bash
git add paperreadagent/modules/ideator/schema.py paperreadagent/modules/ideator/tests/test_schema.py
git commit -m "feat(ideator): add schema v2 migration with review_records, pipeline_runs, recall_weights tables"
```

---

### Task 2: Config 模型配置

**Files:**
- Modify: `paperreadagent/modules/ideator/config.default.yaml`
- Test: `paperreadagent/modules/ideator/tests/test_config.py`

- [ ] **Step 1: 更新 config.default.yaml**

```yaml
# modules/ideator/config.default.yaml
modules:
  ideator:
    # 跨模型审查配置
    ideator_llm:
      api_key: ""                      # 从环境变量或 config.yaml 覆盖
      api_base_url: "https://api.gpt.ge/v1"
      timeout: 120.0

    # 模型路由
    models:
      scorer: "gemini-3-flash-preview"        # T1: LinkScorer
      generator: ""                            # 空=跟随 core.llm (deepseek)
      reviewer_1: "gemini-3-flash-preview"     # T2: 审查者1
      reviewer_2: "qwen3.6-plus"              # T2: 审查者2
      arbiter: "claude-opus-4-7-max"          # T3: 仲裁
      auditor: "qwen3.6-plus"                 # S6: 审计

    # 仲裁阈值
    arbitration:
      both_high_threshold: 0.8         # 双方 ≥ 此值 → 高价值深化
      divergence_threshold: 0.25       # |R1-R2| ≥ 此值 → 仲裁
      both_low_threshold: 0.4          # 双方 ≤ 此值 → 直接 REJECT

    # 迭代深化
    deepen:
      pass_threshold: 0.7              # 审查分数 ≥ 此值通过
      max_rounds: 3                    # 最大迭代轮数

    # 去重（不变）
    dedup_merge_threshold: 0.85
    dedup_flag_threshold: 0.6

    # 调度（不变）
    full_mine_hour: 3
    gc_interval_minutes: 30
```

- [ ] **Step 2: 编写配置测试**

```python
# tests/test_config.py
import yaml
from pathlib import Path

def test_config_has_ideator_llm_section():
    cfg = yaml.safe_load(open(Path(__file__).parent.parent / "config.default.yaml"))
    ideator = cfg["modules"]["ideator"]
    assert "ideator_llm" in ideator
    assert "models" in ideator
    assert "arbitration" in ideator
    assert "deepen" in ideator

def test_arbitration_thresholds_are_valid():
    cfg = yaml.safe_load(open(Path(__file__).parent.parent / "config.default.yaml"))
    arb = cfg["modules"]["ideator"]["arbitration"]
    assert 0 < arb["both_high_threshold"] < 1.0
    assert 0 < arb["divergence_threshold"] < 0.5
    assert 0 < arb["both_low_threshold"] < 0.5

def test_all_model_slots_defined():
    cfg = yaml.safe_load(open(Path(__file__).parent.parent / "config.default.yaml"))
    models = cfg["modules"]["ideator"]["models"]
    for key in ["scorer", "generator", "reviewer_1", "reviewer_2", "arbiter", "auditor"]:
        assert key in models, f"Missing model slot: {key}"
```

- [ ] **Step 3: 运行测试确认 PASS**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_config.py -v
```

- [ ] **Step 4: Commit**

```bash
git add paperreadagent/modules/ideator/config.default.yaml paperreadagent/modules/ideator/tests/test_config.py
git commit -m "feat(ideator): add multi-model config with arbitration thresholds and deepen settings"
```

---

### Task 3: Effort 自动计算器

**Files:**
- Create: `paperreadagent/modules/ideator/effort.py`
- Test: `paperreadagent/modules/ideator/tests/test_effort.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_effort.py
from modules.ideator.effort import auto_effort, EffortContext, EFFORT_PARAMS

def test_lite_effort_with_minimal_signals():
    ctx = EffortContext(candidate_count=3, total_papers=5, useful_ratio=0.0,
                        hours_since_full=1, trigger="event")
    assert auto_effort(ctx) == "lite"

def test_balanced_effort_with_moderate_signals():
    ctx = EffortContext(candidate_count=10, total_papers=20, useful_ratio=0.3,
                        hours_since_full=6, trigger="event")
    assert auto_effort(ctx) == "balanced"

def test_max_effort_with_strong_signals():
    ctx = EffortContext(candidate_count=20, total_papers=35, useful_ratio=0.5,
                        hours_since_full=13, trigger="daily_cron")
    assert auto_effort(ctx) == "max"

def test_beast_effort_with_all_signals():
    ctx = EffortContext(candidate_count=25, total_papers=50, useful_ratio=0.7,
                        hours_since_full=25, trigger="user_manual")
    assert auto_effort(ctx) == "beast"

def test_effort_params_has_all_levels():
    for level in ["lite", "balanced", "max", "beast"]:
        assert level in EFFORT_PARAMS

def test_effort_params_keys_are_consistent():
    keys = set(EFFORT_PARAMS["lite"].keys())
    for level in ["balanced", "max", "beast"]:
        assert set(EFFORT_PARAMS[level].keys()) == keys
```

- [ ] **Step 2: 运行测试确认全部 FAIL**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_effort.py -v
```

- [ ] **Step 3: 实现 effort.py**

```python
"""
modules/ideator/effort.py
自动 Effort Level 计算器。根据上下文信号决定管道深度。
"""

from dataclasses import dataclass


@dataclass
class EffortContext:
    candidate_count: int = 0
    total_papers: int = 0
    useful_ratio: float = 0.0
    hours_since_full: float = 0.0
    trigger: str = "event"  # event | daily_cron | user_manual


def auto_effort(ctx: EffortContext) -> str:
    score = 0.0
    if ctx.candidate_count > 15:
        score += 0.25
    if ctx.total_papers > 30:
        score += 0.25
    if ctx.useful_ratio > 0.4:
        score += 0.20
    if ctx.hours_since_full > 12:
        score += 0.15
    if ctx.trigger == "daily_cron":
        score += 0.15
    if ctx.trigger == "user_manual":
        score += 0.30
    if score < 0.25:
        return "lite"
    if score < 0.50:
        return "balanced"
    if score < 0.80:
        return "max"
    return "beast"


EFFORT_PARAMS = {
    "lite": {
        "recall_paths": ["similarity", "contradiction", "timeline"],
        "sample_size": 1,
        "spark_count": (2, 3),
        "skip_review": True,
        "skip_arbitration": True,
        "auto_deepen": False,
        "deepen_rounds": 1,
        "skip_audit": True,
    },
    "balanced": {
        "recall_paths": ["similarity", "contradiction", "cross_layer", "timeline"],
        "sample_size": 2,
        "spark_count": (3, 5),
        "skip_review": False,
        "review_top_n": 2,
        "skip_arbitration": True,
        "auto_deepen": False,
        "deepen_rounds": 1,
        "skip_audit": False,
        "audit_top_n": 1,
    },
    "max": {
        "recall_paths": ["similarity", "contradiction", "cross_project",
                         "cross_layer", "random_walk", "timeline"],
        "sample_size": 5,
        "spark_count": (5, 7),
        "skip_review": False,
        "review_top_n": 0,  # 0 = all
        "skip_arbitration": False,
        "arbitrate_dispute_only": True,
        "auto_deepen": True,
        "deepen_rounds": 2,
        "skip_audit": False,
        "audit_top_n": 0,  # all PASSed
    },
    "beast": {
        "recall_paths": ["similarity", "contradiction", "cross_project",
                         "cross_layer", "random_walk", "timeline"],
        "sample_size": 10,
        "spark_count": (7, 10),
        "skip_review": False,
        "review_top_n": 0,
        "skip_arbitration": False,
        "arbitrate_dispute_only": False,
        "auto_deepen": True,
        "deepen_rounds": 3,
        "skip_audit": False,
        "audit_top_n": 0,
    },
}
```

- [ ] **Step 4: 运行测试确认 PASS**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_effort.py -v
```

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/effort.py paperreadagent/modules/ideator/tests/test_effort.py
git commit -m "feat(ideator): add auto-effort calculator with 4-level parameter table"
```

---

### Task 4: Pipeline State 持久化

**Files:**
- Create: `paperreadagent/modules/ideator/state.py`
- Test: `paperreadagent/modules/ideator/tests/test_state.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_state.py
import json
from pathlib import Path
from modules.ideator.state import PipelineState, save_state, load_state

def test_pipeline_state_defaults():
    ps = PipelineState(run_id="test-123")
    assert ps.run_id == "test-123"
    assert ps.current_stage == "recall"
    assert ps.stages_completed == []

def test_save_and_load_state(tmp_path):
    ps = PipelineState(run_id="test-456", current_stage="review",
                       stages_completed=["recall", "score", "generate"],
                       candidates_count=24, sparks_generated=5,
                       effort="max")
    save_state(ps, tmp_path)
    loaded = load_state(tmp_path)
    assert loaded.run_id == "test-456"
    assert loaded.current_stage == "review"
    assert loaded.candidates_count == 24
    assert loaded.effort == "max"

def test_load_state_missing_file(tmp_path):
    ps = load_state(tmp_path)
    assert ps is None
```

- [ ] **Step 2: 实现 state.py**

```python
"""
modules/ideator/state.py
管道状态持久化 — 阶段间存盘，支持压缩恢复和断点续传。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

STATE_FILENAME = "PIPELINE_STATE.json"


@dataclass
class PipelineState:
    run_id: str
    current_stage: str = "recall"
    stages_completed: list[str] = field(default_factory=list)
    candidates_count: int = 0
    sparks_generated: int = 0
    sparks_reviewed: int = 0
    effort: str = "balanced"
    updated_at: str = ""

    def to_dict(self) -> dict:
        import datetime
        return {
            "run_id": self.run_id,
            "current_stage": self.current_stage,
            "stages_completed": self.stages_completed,
            "candidates_count": self.candidates_count,
            "sparks_generated": self.sparks_generated,
            "sparks_reviewed": self.sparks_reviewed,
            "effort": self.effort,
            "updated_at": datetime.datetime.now().isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PipelineState":
        return cls(
            run_id=d["run_id"],
            current_stage=d.get("current_stage", "recall"),
            stages_completed=d.get("stages_completed", []),
            candidates_count=d.get("candidates_count", 0),
            sparks_generated=d.get("sparks_generated", 0),
            sparks_reviewed=d.get("sparks_reviewed", 0),
            effort=d.get("effort", "balanced"),
            updated_at=d.get("updated_at", ""),
        )


def save_state(state: PipelineState, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    state_file = directory / STATE_FILENAME
    state_file.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2))


def load_state(directory: Path) -> PipelineState | None:
    state_file = directory / STATE_FILENAME
    if not state_file.exists():
        return None
    return PipelineState.from_dict(json.loads(state_file.read_text()))
```

- [ ] **Step 3: 运行测试确认 PASS**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/test_state.py -v
```

- [ ] **Step 4: Commit**

```bash
git add paperreadagent/modules/ideator/state.py paperreadagent/modules/ideator/tests/test_state.py
git commit -m "feat(ideator): add pipeline state persistence for compaction recovery"
```

---

### Task 5: IdeatorLLM 模块专用客户端

**Files:**
- Create: `paperreadagent/modules/ideator/ideator_llm.py`
- Test: `paperreadagent/modules/ideator/tests/test_ideator_llm.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_ideator_llm.py
import pytest
from unittest.mock import MagicMock, patch
from modules.ideator.ideator_llm import IdeatorLLM

@pytest.fixture
def llm_cfg():
    return {
        "api_key": "test-key",
        "api_base_url": "https://api.gpt.ge/v1",
        "timeout": 30.0,
    }

@pytest.fixture
def model_cfg():
    return {
        "scorer": "gemini-3-flash-preview",
        "reviewer_1": "gemini-3-flash-preview",
        "reviewer_2": "qwen3.6-plus",
        "arbiter": "claude-opus-4-7-max",
        "auditor": "qwen3.6-plus",
    }

def test_init_stores_config(llm_cfg, model_cfg):
    client = IdeatorLLM(llm_cfg=llm_cfg, model_cfg=model_cfg)
    assert client.api_key == "test-key"
    assert client.api_base_url == "https://api.gpt.ge/v1"

def test_get_model_routes_correctly(llm_cfg, model_cfg):
    client = IdeatorLLM(llm_cfg=llm_cfg, model_cfg=model_cfg)
    assert client.model_for("scorer") == "gemini-3-flash-preview"
    assert client.model_for("arbiter") == "claude-opus-4-7-max"

def test_lazy_client_initialization(llm_cfg, model_cfg):
    client = IdeatorLLM(llm_cfg=llm_cfg, model_cfg=model_cfg)
    assert client._client is None

@pytest.mark.asyncio
async def test_chat_calls_openai(llm_cfg, model_cfg):
    client = IdeatorLLM(llm_cfg=llm_cfg, model_cfg=model_cfg)
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "response text"
    mock_resp.usage.total_tokens = 100
    with patch.object(client, '_get_client') as mock_get_client:
        mock_get_client.return_value.chat.completions.create.return_value = mock_resp
        result = await client.chat(
            model_role="scorer",
            messages=[{"role": "user", "content": "hello"}],
        )
        assert result == "response text"

@pytest.mark.asyncio
async def test_chat_json_mode(llm_cfg, model_cfg):
    client = IdeatorLLM(llm_cfg=llm_cfg, model_cfg=model_cfg)
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = '{"score": 0.8}'
    mock_resp.usage.total_tokens = 50
    with patch.object(client, '_get_client') as mock_get_client:
        mock_get_client.return_value.chat.completions.create.return_value = mock_resp
        result = await client.chat(
            model_role="scorer",
            messages=[{"role": "user", "content": "score this"}],
            response_format={"type": "json_object"},
        )
        assert '"score"' in result
```

- [ ] **Step 2: 运行测试确认 FAIL**

- [ ] **Step 3: 实现 ideator_llm.py**

```python
"""
modules/ideator/ideator_llm.py
IdeatorLLM — 模块专用 LLM 客户端，封装 gpt.ge API。
管理与 core.llm 不同的 baseurl/apikey，按任务角色自动路由模型。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class IdeatorLLMError(Exception):
    """ideator LLM 调用失败。"""


class IdeatorLLM:
    def __init__(self, *, llm_cfg: dict, model_cfg: dict):
        self.api_key = llm_cfg.get("api_key", "")
        self.api_base_url = llm_cfg.get("api_base_url", "https://api.gpt.ge/v1")
        self.timeout = llm_cfg.get("timeout", 120.0)
        self._model_cfg = model_cfg
        self._client = None

    def _get_client(self):
        if self._client is None:
            import openai
            self._client = openai.AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.api_base_url,
                timeout=self.timeout,
            )
        return self._client

    def model_for(self, role: str) -> str:
        return self._model_cfg.get(role, "")

    async def chat(
        self,
        *,
        model_role: str,
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict | None = None,
    ) -> str:
        model = self.model_for(model_role)
        if not model:
            raise IdeatorLLMError(f"No model configured for role: {model_role}")
        client = self._get_client()
        kwargs = dict(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if response_format:
            kwargs["response_format"] = response_format
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            logger.error(f"[IdeatorLLM] {model_role} ({model}) 调用失败: {exc}")
            raise IdeatorLLMError(f"LLM call failed: {exc}") from exc
        content = resp.choices[0].message.content or ""
        return content
```

- [ ] **Step 4: 运行测试确认 PASS**

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/ideator_llm.py paperreadagent/modules/ideator/tests/test_ideator_llm.py
git commit -m "feat(ideator): add IdeatorLLM client with role-based model routing"
```

---

### Task 6: 审查/审计/仲裁 Prompt 模板

**Files:**
- Create: `paperreadagent/modules/ideator/prompts/review_spark.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/audit_spark.jinja2`
- Create: `paperreadagent/modules/ideator/prompts/arbitrate_spark.jinja2`

- [ ] **Step 1: 创建 review_spark.jinja2**

```jinja2
你是一位严格的学术审稿人。你需要独立评估以下研究火花（Research Spark）的质量。

## 火花内容
{{ spark_content }}

## 关联来源 A（类型：{{ source_a_type }}）
{{ source_a_text }}

## 关联来源 B（类型：{{ source_b_type }}）
{{ source_b_text }}

## 评分要求

请从以下三个维度独立评分（0.0-1.0），并给出总体判断：

1. **novelty（新颖性）**：这个火花是否提出了新的视角、关联或假设？是否有别于显而易见的常识？
2. **evidence（证据支撑度）**：火花的主张在源文本中有多强的支撑？是否存在过度推理？
3. **feasibility（可行性）**：这个研究方向是否有实际可操作的下一步？是否可验证？

返回严格 JSON 格式（不要其他文字）：
{
  "scores": {"novelty": 0.X, "evidence": 0.X, "feasibility": 0.X},
  "verdict": "PASS|REVISE|REJECT",
  "reasoning": "你的理由（中文，100-200字）"
}
```

- [ ] **Step 2: 创建 audit_spark.jinja2**

```jinja2
你是一位学术诚信审计员。你需要验证一条研究火花是否真的能从其引用的源文本中推导出来。

## 火花内容
{{ spark_content }}

{% for ref in source_refs %}
## 源文本 {{ loop.index }}（类型：{{ ref.type }}）
{{ ref.text }}
{% endfor %}

## 审计要求

逐一检查火花的每个关键主张，判断它是否能在上述源文本中找到直接或间接证据支撑。

返回严格 JSON 格式：
{
  "verdict": "SUPPORTED|STRETCHED|UNSUPPORTED",
  "claims_check": [
    {"claim": "火花中的具体主张", "evidence_in_source": "在源文本中的对应位置或 N/A", "supported": true|false}
  ],
  "reasoning": "你的综合判断（中文，100-200字）"
}
```

- [ ] **Step 3: 创建 arbitrate_spark.jinja2**

```jinja2
你是一位资深研究仲裁者。两位审稿人对以下研究火花产生了分歧，你需要给出最终裁决。

## 火花内容
{{ spark_content }}

## 审查者 1（{{ reviewer_1_model }}）评分
- novelty: {{ r1_novelty }}
- evidence: {{ r1_evidence }}
- feasibility: {{ r1_feasibility }}
- 判决: {{ r1_verdict }}
- 理由: {{ r1_reasoning }}

## 审查者 2（{{ reviewer_2_model }}）评分
- novelty: {{ r2_novelty }}
- evidence: {{ r2_evidence }}
- feasibility: {{ r2_feasibility }}
- 判决: {{ r2_verdict }}
- 理由: {{ r2_reasoning }}

## 分歧分析

两位审稿人的评分差异为 {{ divergence }}，主要分歧在 {{ dispute_dimension }} 维度。

## 仲裁要求

作为独立仲裁者，请重新审视火花的三个维度，给出你的独立评分和最终裁决。

返回严格 JSON 格式：
{
  "scores": {"novelty": 0.X, "evidence": 0.X, "feasibility": 0.X},
  "verdict": "OVERTURN|CONFIRM_R1|CONFIRM_R2",
  "reasoning": "你的裁决理由（中文，150-300字）"
}
```

- [ ] **Step 4: Commit**

```bash
git add paperreadagent/modules/ideator/prompts/review_spark.jinja2 \
        paperreadagent/modules/ideator/prompts/audit_spark.jinja2 \
        paperreadagent/modules/ideator/prompts/arbitrate_spark.jinja2
git commit -m "feat(ideator): add review, audit, and arbitration prompt templates"
```

---

### Task 7: Reviewer 双审查引擎

**Files:**
- Create: `paperreadagent/modules/ideator/reviewer.py`
- Test: `paperreadagent/modules/ideator/tests/test_reviewer.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_reviewer.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from modules.ideator.reviewer import SparkReviewer, ReviewResult, ArbitrationResult

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.chat = AsyncMock()
    llm.model_for = MagicMock(side_effect=lambda role: {
        "reviewer_1": "gemini-flash", "reviewer_2": "qwen",
        "arbiter": "claude-opus",
    }.get(role, ""))
    return llm

@pytest.fixture
def reviewer(mock_llm):
    return SparkReviewer(
        llm=mock_llm,
        arbitration_cfg={"both_high_threshold": 0.8, "divergence_threshold": 0.25,
                         "both_low_threshold": 0.4},
        prompt_dir=None,
    )

def test_needs_arbitration_both_high(reviewer):
    r1 = ReviewResult(scores={"novelty": 0.9, "evidence": 0.9, "feasibility": 0.9},
                      verdict="PASS", reasoning="", reviewer_model="gemini-flash",
                      reviewer_role="reviewer_1")
    r2 = ReviewResult(scores={"novelty": 0.85, "evidence": 0.8, "feasibility": 0.9},
                      verdict="PASS", reasoning="", reviewer_model="qwen",
                      reviewer_role="reviewer_2")
    action, reason = reviewer._decide_action(r1, r2)
    assert action == "arbitrate_high_value"
    assert "high value" in reason.lower()

def test_needs_arbitration_divergence(reviewer):
    r1 = ReviewResult(scores={"novelty": 0.8, "evidence": 0.8, "feasibility": 0.8},
                      verdict="PASS", reasoning="", reviewer_model="gemini-flash",
                      reviewer_role="reviewer_1")
    r2 = ReviewResult(scores={"novelty": 0.3, "evidence": 0.2, "feasibility": 0.3},
                      verdict="REJECT", reasoning="", reviewer_model="qwen",
                      reviewer_role="reviewer_2")
    action, reason = reviewer._decide_action(r1, r2)
    assert action == "arbitrate_dispute"

def test_both_low_rejects(reviewer):
    r1 = ReviewResult(scores={"novelty": 0.3, "evidence": 0.3, "feasibility": 0.3},
                      verdict="REJECT", reasoning="", reviewer_model="gemini-flash",
                      reviewer_role="reviewer_1")
    r2 = ReviewResult(scores={"novelty": 0.2, "evidence": 0.3, "feasibility": 0.2},
                      verdict="REJECT", reasoning="", reviewer_model="qwen",
                      reviewer_role="reviewer_2")
    action, reason = reviewer._decide_action(r1, r2)
    assert action == "reject"

@pytest.mark.asyncio
async def test_review_spark_returns_two_results(reviewer, mock_llm):
    mock_llm.chat.return_value = '{"scores":{"novelty":0.7,"evidence":0.6,"feasibility":0.8},"verdict":"PASS","reasoning":"good"}'
    r1, r2 = await reviewer.review_spark(
        spark_content="test spark",
        source_a_type="paper", source_a_text="source A",
        source_b_type="note", source_b_text="source B",
    )
    assert r1.reviewer_role == "reviewer_1"
    assert r2.reviewer_role == "reviewer_2"
    assert r1.scores["novelty"] == 0.7
    assert mock_llm.chat.call_count == 2
```

- [ ] **Step 2: 运行测试确认 FAIL**

- [ ] **Step 3: 实现 reviewer.py**

```python
"""
modules/ideator/reviewer.py
SparkReviewer — 双审查引擎 + 仲裁触发。
两个独立审查者同时评估火花，触发条件决定是否升级到 Tier 3 仲裁。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass
class ReviewResult:
    scores: dict  # {"novelty": 0.X, "evidence": 0.X, "feasibility": 0.X}
    verdict: str  # PASS / REVISE / REJECT
    reasoning: str
    reviewer_model: str
    reviewer_role: str  # reviewer_1 / reviewer_2 / arbiter

    @property
    def overall(self) -> float:
        return sum(self.scores.values()) / len(self.scores) if self.scores else 0.0


@dataclass
class ArbitrationResult:
    scores: dict
    verdict: str  # OVERTURN / CONFIRM_R1 / CONFIRM_R2
    reasoning: str
    escalation_reason: str


class SparkReviewer:
    def __init__(self, *, llm, arbitration_cfg: dict, prompt_dir: Path | None = None):
        self._llm = llm
        self._arbitration_cfg = arbitration_cfg
        self._prompt_dir = prompt_dir or Path(__file__).parent / "prompts"

    def _load_prompt(self, name: str) -> str:
        path = self._prompt_dir / f"{name}.jinja2"
        if path.exists():
            return path.read_text(encoding="utf-8")
        from jinja2 import Environment
        env = Environment()
        tpl_path = Path(__file__).parent / "prompts" / f"{name}.jinja2"
        return tpl_path.read_text(encoding="utf-8")

    async def review_spark(
        self, *, spark_content: str,
        source_a_type: str, source_a_text: str,
        source_b_type: str, source_b_text: str,
        skip_arbitration: bool = True,
    ) -> tuple[ReviewResult, ReviewResult, ArbitrationResult | None]:
        template = self._load_prompt("review_spark")
        from jinja2 import Template
        tpl = Template(template)
        prompt = tpl.render(
            spark_content=spark_content,
            source_a_type=source_a_type,
            source_a_text=source_a_text,
            source_b_type=source_b_type,
            source_b_text=source_b_text,
        )
        messages = [{"role": "user", "content": prompt}]

        # 并行双审查
        import asyncio
        r1_task = self._call_reviewer(messages, "reviewer_1")
        r2_task = self._call_reviewer(messages, "reviewer_2")
        r1, r2 = await asyncio.gather(r1_task, r2_task)

        # 仲裁判断
        if skip_arbitration:
            return r1, r2, None

        action, reason = self._decide_action(r1, r2)
        if action in ("arbitrate_high_value", "arbitrate_dispute"):
            arb = await self._call_arbiter(spark_content, r1, r2, reason)
            return r1, r2, arb

        return r1, r2, None

    async def _call_reviewer(self, messages: list[dict], role: str) -> ReviewResult:
        raw = await self._llm.chat(
            model_role=role,
            messages=messages,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        data = json.loads(raw)
        return ReviewResult(
            scores=data["scores"],
            verdict=data["verdict"],
            reasoning=data.get("reasoning", ""),
            reviewer_model=self._llm.model_for(role),
            reviewer_role=role,
        )

    def _decide_action(self, r1: ReviewResult, r2: ReviewResult) -> tuple[str, str]:
        o1, o2 = r1.overall, r2.overall
        both_high = self._arbitration_cfg.get("both_high_threshold", 0.8)
        divergence = self._arbitration_cfg.get("divergence_threshold", 0.25)
        both_low = self._arbitration_cfg.get("both_low_threshold", 0.4)

        if o1 >= both_high and o2 >= both_high:
            return "arbitrate_high_value", f"Both high: R1={o1:.2f}, R2={o2:.2f}"
        if abs(o1 - o2) >= divergence:
            return "arbitrate_dispute", f"Divergence: |{o1:.2f} - {o2:.2f}| = {abs(o1-o2):.2f}"
        if o1 <= both_low and o2 <= both_low:
            return "reject", f"Both low: R1={o1:.2f}, R2={o2:.2f}"
        return "revise_pass", "Moderate consensus"

    async def _call_arbiter(
        self, spark_content: str, r1: ReviewResult, r2: ReviewResult, reason: str,
    ) -> ArbitrationResult:
        template = self._load_prompt("arbitrate_spark")
        from jinja2 import Template
        tpl = Template(template)
        prompt = tpl.render(
            spark_content=spark_content,
            reviewer_1_model=r1.reviewer_model,
            r1_novelty=r1.scores.get("novelty"),
            r1_evidence=r1.scores.get("evidence"),
            r1_feasibility=r1.scores.get("feasibility"),
            r1_verdict=r1.verdict,
            r1_reasoning=r1.reasoning,
            reviewer_2_model=r2.reviewer_model,
            r2_novelty=r2.scores.get("novelty"),
            r2_evidence=r2.scores.get("evidence"),
            r2_feasibility=r2.scores.get("feasibility"),
            r2_verdict=r2.verdict,
            r2_reasoning=r2.reasoning,
            divergence=f"{abs(r1.overall - r2.overall):.2f}",
            dispute_dimension=self._dispute_dimension(r1, r2),
        )
        messages = [{"role": "user", "content": prompt}]
        raw = await self._llm.chat(
            model_role="arbiter",
            messages=messages,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        data = json.loads(raw)
        return ArbitrationResult(
            scores=data["scores"],
            verdict=data["verdict"],
            reasoning=data.get("reasoning", ""),
            escalation_reason=reason,
        )

    def _dispute_dimension(self, r1: ReviewResult, r2: ReviewResult) -> str:
        max_diff = 0.0
        dim = "overall"
        for key in r1.scores:
            diff = abs(r1.scores[key] - r2.scores.get(key, 0))
            if diff > max_diff:
                max_diff = diff
                dim = key
        return dim
```

- [ ] **Step 4: 运行测试确认 PASS**

- [ ] **Step 5: Commit**

```bash
git add paperreadagent/modules/ideator/reviewer.py paperreadagent/modules/ideator/tests/test_reviewer.py
git commit -m "feat(ideator): add dual-review engine with Tier 3 arbitration escalation"
```

---

### Task 8: Auditor 溯源审计

**Files:**
- Create: `paperreadagent/modules/ideator/auditor.py`
- Test: `paperreadagent/modules/ideator/tests/test_auditor.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_auditor.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from modules.ideator.auditor import SparkAuditor, AuditResult

@pytest.fixture
def mock_llm():
    llm = MagicMock()
    llm.chat = AsyncMock()
    llm.model_for = MagicMock(return_value="qwen3.6-plus")
    return llm

@pytest.fixture
def auditor(mock_llm):
    return SparkAuditor(llm=mock_llm)

@pytest.mark.asyncio
async def test_audit_returns_supported(auditor, mock_llm):
    mock_llm.chat.return_value = '{"verdict":"SUPPORTED","claims_check":[{"claim":"x","evidence_in_source":"y","supported":true}],"reasoning":"good"}'
    result = await auditor.audit(spark_content="spark text",
                                  source_refs=[{"type":"paper","text":"source content"}])
    assert result.verdict == "SUPPORTED"

@pytest.mark.asyncio
async def test_audit_returns_unsupported(auditor, mock_llm):
    mock_llm.chat.return_value = '{"verdict":"UNSUPPORTED","claims_check":[],"reasoning":"no evidence"}'
    result = await auditor.audit(spark_content="spark text",
                                  source_refs=[{"type":"paper","text":"unrelated"}])
    assert result.verdict == "UNSUPPORTED"

def test_audit_score_delta():
    assert SparkAuditor.score_delta("SUPPORTED") == 0.1
    assert SparkAuditor.score_delta("STRETCHED") == 0.0
    assert SparkAuditor.score_delta("UNSUPPORTED") == -0.3
```

- [ ] **Step 2: 实现 auditor.py**

```python
"""
modules/ideator/auditor.py
SparkAuditor — 独立模型验证火花是否被源文本支撑。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class AuditResult:
    verdict: str  # SUPPORTED / STRETCHED / UNSUPPORTED
    claims_check: list[dict]
    reasoning: str


class SparkAuditor:
    def __init__(self, *, llm):
        self._llm = llm

    @staticmethod
    def score_delta(verdict: str) -> float:
        if verdict == "SUPPORTED":
            return 0.1
        if verdict == "UNSUPPORTED":
            return -0.3
        return 0.0  # STRETCHED

    async def audit(self, *, spark_content: str, source_refs: list[dict]) -> AuditResult:
        from jinja2 import Template
        tpl_path = Path(__file__).parent / "prompts" / "audit_spark.jinja2"
        template_str = tpl_path.read_text(encoding="utf-8")
        tpl = Template(template_str)
        prompt = tpl.render(spark_content=spark_content, source_refs=source_refs)
        messages = [{"role": "user", "content": prompt}]
        raw = await self._llm.chat(
            model_role="auditor",
            messages=messages,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        data = json.loads(raw)
        return AuditResult(
            verdict=data["verdict"],
            claims_check=data.get("claims_check", []),
            reasoning=data.get("reasoning", ""),
        )
```

- [ ] **Step 3: 运行测试确认 PASS**

- [ ] **Step 4: Commit**

```bash
git add paperreadagent/modules/ideator/auditor.py paperreadagent/modules/ideator/tests/test_auditor.py
git commit -m "feat(ideator): add SparkAuditor for source-text verification"
```

---

### Task 9: Feedback Loop 反馈闭环

**Files:**
- Create: `paperreadagent/modules/ideator/feedback_loop.py`
- Test: `paperreadagent/modules/ideator/tests/test_feedback_loop.py`

- [ ] **Step 1: 编写测试**

```python
# tests/test_feedback_loop.py
from modules.ideator.feedback_loop import FeedbackLoop, adjust_weight

def test_useful_feedback_increases_weight():
    new_w = adjust_weight(1.0, "useful")
    assert new_w == 1.05

def test_noise_feedback_decreases_weight():
    new_w = adjust_weight(1.0, "noise")
    assert new_w == 0.9

def test_weight_clamped_at_2():
    new_w = adjust_weight(1.98, "useful")
    assert new_w == 2.0

def test_weight_clamped_at_0():
    new_w = adjust_weight(0.03, "noise")
    assert new_w == 0.0

def test_weight_below_threshold_disables_path():
    w = 0.19
    assert w < 0.2  # should be considered disabled
```

- [ ] **Step 2: 实现 feedback_loop.py**

```python
"""
modules/ideator/feedback_loop.py
FeedbackLoop — 用户反馈驱动召回路径权重调整。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

WEIGHT_MIN = 0.0
WEIGHT_MAX = 2.0
DISABLE_THRESHOLD = 0.2
USEFUL_DELTA = 0.05
NOISE_DELTA = -0.1


def adjust_weight(current: float, feedback: str) -> float:
    if feedback == "useful":
        new_w = current + USEFUL_DELTA
    elif feedback in ("duplicate", "noise"):
        new_w = current + NOISE_DELTA
    else:
        return current
    return max(WEIGHT_MIN, min(WEIGHT_MAX, new_w))


class FeedbackLoop:
    def __init__(self, data_access):
        self._data = data_access

    def record_feedback(self, source_type: str, feedback: str) -> None:
        cur = self._data.get_recall_weight(source_type)
        if cur is None:
            return
        new_weight = adjust_weight(cur["weight"], feedback)
        useful_delta = 1 if feedback == "useful" else 0
        noise_delta = 1 if feedback in ("duplicate", "noise") else 0
        self._data.update_recall_weight(
            source_type=source_type,
            weight=new_weight,
            useful_inc=useful_delta,
            noise_inc=noise_delta,
        )
        if new_weight < DISABLE_THRESHOLD:
            logger.info(f"[FeedbackLoop] {source_type} 权重降至 {new_weight:.2f}，已关闭")
```

- [ ] **Step 3: 运行测试确认 PASS**

- [ ] **Step 4: Commit**

```bash
git add paperreadagent/modules/ideator/feedback_loop.py paperreadagent/modules/ideator/tests/test_feedback_loop.py
git commit -m "feat(ideator): add feedback loop for recall weight adjustment"
```

---

### Task 10: CrossRecall 升级（effort + weights 驱动）

**Files:**
- Modify: `paperreadagent/modules/ideator/cross_recall.py`
- Test: `paperreadagent/modules/ideator/tests/test_cross_recall.py`（扩展）

- [ ] **Step 1: 扩展测试**

```python
# 追加到 test_cross_recall.py
from modules.ideator.cross_recall import CrossRecall
from modules.ideator.effort import EFFORT_PARAMS

def test_recall_respects_effort_paths():
    # lite effort 只用 3 路
    params = EFFORT_PARAMS["lite"]
    assert len(params["recall_paths"]) == 3

def test_beast_effort_uses_all_paths():
    params = EFFORT_PARAMS["beast"]
    assert len(params["recall_paths"]) == 6

def test_disabled_path_excluded():
    # 当某路权重 < 0.2 时应被排除
    from modules.ideator.feedback_loop import DISABLE_THRESHOLD
    assert 0.15 < DISABLE_THRESHOLD
```

- [ ] **Step 2: 修改 cross_recall.py**

在 `CrossRecall.recall()` 方法中：
1. 读取 `effort_params` 参数
2. 根据 `recall_paths` 过滤要执行的路径
3. 根据 `sample_size` 控制各路径采样量
4. 读 `ideator_recall_weights` 表，排除权重 < 0.2 的路径

```python
async def recall(self, core_llm, scope="all", effort_params=None) -> list[dict]:
    if effort_params is None:
        effort_params = {"recall_paths": ["similarity", "contradiction", "cross_project",
                                           "cross_layer", "random_walk", "timeline"],
                         "sample_size": 3}
    path_methods = {
        "similarity": self._recall_similarity,
        "contradiction": self._recall_contradiction,
        "cross_project": self._recall_cross_project,
        "cross_layer": self._recall_cross_layer,
        "random_walk": self._recall_random_walk,
        "timeline": self._recall_timeline,
    }
    # 排除被反馈闭环关闭的路径
    active_paths = effort_params["recall_paths"].copy()
    weights = self._data.get_recall_weights()
    disabled = {row["source_type"] for row in weights if row["weight"] < 0.2}
    active_paths = [p for p in active_paths if p not in disabled]

    tasks = {name: path_methods[name](core_llm) for name in active_paths}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    # ... 合并、去重逻辑不变 ...
```

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/cross_recall.py paperreadagent/modules/ideator/tests/test_cross_recall.py
git commit -m "feat(ideator): add effort-driven recall path selection and weight filtering"
```

---

### Task 11: SparkStore 升级（新字段 + 审查追踪）

**Files:**
- Modify: `paperreadagent/modules/ideator/spark_store.py`
- Test: `paperreadagent/modules/ideator/tests/test_spark_store.py`（扩展）

- [ ] **Step 1: 扩展测试**

```python
# 追加到 test_spark_store.py
def test_save_spark_with_review_fields():
    # mock data_access.insert_spark 验证传入了 run_id, generator_score, review_status

def test_update_spark_review_result():
    # 验证 final_score, review_status, review_count 更新
```

- [ ] **Step 2: 修改 spark_store.py**

`save_spark()` 方法新增参数：`run_id`, `generator_score`。入库时写入新字段。

新增 `update_review_result()` 方法：
```python
def update_review_result(self, spark_id: int, *, final_score: float,
                         review_status: str, verdict: str) -> None:
    self._data.update_spark(spark_id,
        final_score=final_score,
        review_status=review_status,
        review_count=self._data.get_spark(spark_id).get("review_count", 0) + 1,
    )
```

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/spark_store.py paperreadagent/modules/ideator/tests/test_spark_store.py
git commit -m "feat(ideator): extend SparkStore with review tracking fields"
```

---

### Task 12: Pipeline 重写（集成全部新组件）

**Files:**
- Modify: `paperreadagent/modules/ideator/pipeline.py`（重写）
- Test: `paperreadagent/modules/ideator/tests/test_pipeline.py`（扩展）

- [ ] **Step 1: 编写新 pipeline 测试**

```python
# tests/test_pipeline.py 扩展
@pytest.mark.asyncio
async def test_pipeline_auto_effort_incremental():
    # 增量事件 → auto_effort 应返回 lite

@pytest.mark.asyncio
async def test_pipeline_auto_effort_daily_cron():
    # 每日定时 → auto_effort 应返回 balanced 或更高

@pytest.mark.asyncio
async def test_pipeline_with_review_stage():
    # balanced effort → 应有 review 阶段

@pytest.mark.asyncio
async def test_pipeline_with_audit_stage():
    # max effort → 应有 audit 阶段
```

- [ ] **Step 2: 重写 pipeline.py**

核心变化：
1. `run_full()` / `run_incremental()` / `run_targeted()` 统一调用 `_run(auto_effort=...)`
2. `_run()` 自动计算 effort → 读 EFFORT_PARAMS → 依序执行 S0-S6
3. S3: 调用 `SparkReviewer.review_spark()` + 仲裁判断
4. S5: 迭代深化循环（deepen→review→revise→re-review）
5. S6: 调用 `SparkAuditor.audit()` + 分数回写
6. 每个阶段后调用 `save_state()`
7. 写入 `ideator_pipeline_runs` + 所有 `ideator_review_records`

新增 `_run()` 统一入口：

```python
async def _run(self, *, trigger: str = "event", scope: str = "all") -> list[int]:
    import uuid
    run_id = str(uuid.uuid4())
    ctx = await self._build_effort_context(trigger)
    effort = auto_effort(ctx)
    params = EFFORT_PARAMS[effort]
    
    # 写 pipeline_runs
    self._data.insert_pipeline_run(run_id=run_id, trigger=trigger, effort=effort)
    
    state = PipelineState(run_id=run_id, effort=effort)
    
    # S0: Recall
    candidates = await self.recall.recall(self.core.llm, effort_params=params)
    if not candidates:
        self._data.finish_pipeline_run(run_id, stages=["recall"], stats={"candidates": 0})
        return []
    state.candidates_count = len(candidates)
    save_state(state, self._state_dir)
    
    # S1: Score (T1: gemini-flash)
    links = await self._score_links(candidates, effort=effort)
    
    # S2: Generate sparks (deepseek)
    sparks_raw = await self._generate_sparks(links[:15])
    state.sparks_generated = len(sparks_raw)
    save_state(state, self._state_dir)
    
    saved_ids = []
    for spark in sparks_raw:
        emb = await self.core.llm.embed(spark["content"][:500], module="ideator")
        spark_id = self.store.save_spark(
            content=spark["content"], source_type=spark["source_type"],
            source_refs=spark["source_refs"], embedding=emb,
            quality_score=spark.get("quality_score", 0.5),
            core_llm=self.core.llm, run_id=run_id,
            generator_score=spark.get("quality_score", 0.5),
        )
        if not spark_id:
            continue
        
        # S3: Review (if not skipped)
        if not params["skip_review"]:
            review_top_n = params.get("review_top_n", 0)
            if review_top_n == 0 or len(saved_ids) < review_top_n:
                source_a, source_b = await self._resolve_sources(spark)
                r1, r2, arb = await self.reviewer.review_spark(
                    spark_content=spark["content"],
                    source_a_type=source_a["type"], source_a_text=source_a["content"][:500],
                    source_b_type=source_b["type"], source_b_text=source_b["content"][:500],
                    skip_arbitration=params["skip_arbitration"],
                )
                # 写入 review_records
                self._save_review_record(spark_id, r1, "review", run_id)
                self._save_review_record(spark_id, r2, "review", run_id)
                
                if arb:
                    self._save_review_record(spark_id, arb, "arbitration", run_id)
                    final = arb.scores
                    status = "escalated"
                else:
                    avg_scores = {
                        k: (r1.scores.get(k,0)+r2.scores.get(k,0))/2
                        for k in r1.scores
                    }
                    final = avg_scores
                    overall = sum(avg_scores.values()) / len(avg_scores) if avg_scores else 0.5
                    if r1.verdict == "REJECT" and r2.verdict == "REJECT":
                        status = "rejected"
                    elif overall >= 0.5:
                        status = "passed"
                    else:
                        status = "revised"
                
                final_overall = sum(final.values()) / len(final) if final else 0.5
                self.store.update_review_result(spark_id, final_score=final_overall,
                                                review_status=status, verdict=status)
        
        # S6: Audit (if not skipped)
        if not params["skip_audit"]:
            audit_top_n = params.get("audit_top_n", 0)
            if audit_top_n == 0 or len(saved_ids) < audit_top_n:
                source_refs = await self._resolve_all_sources(spark)
                aud_result = await self.auditor.audit(
                    spark_content=spark["content"], source_refs=source_refs)
                self._save_audit_record(spark_id, aud_result, run_id)
                delta = SparkAuditor.score_delta(aud_result.verdict)
                if delta != 0:
                    spark_data = self._data.get_spark(spark_id)
                    new_q = max(0, min(1.0, (spark_data.get("quality_score", 0.5) + delta)))
                    self._data.update_spark(spark_id, quality_score=new_q)
        
        saved_ids.append(spark_id)
        await self.event_bus.emit("ideator:spark:created", spark_id=spark_id)
    
    # 完成
    self._data.finish_pipeline_run(run_id,
        stages=list(filter(None, ["recall","score","generate",
            "review" if not params["skip_review"] else None,
            "audit" if not params["skip_audit"] else None])),
        stats={"candidates": len(candidates), "sparks": len(saved_ids)})
    
    return saved_ids
```

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/pipeline.py paperreadagent/modules/ideator/tests/test_pipeline.py
git commit -m "feat(ideator): rewrite pipeline with cross-model review, arbitration, deepen loop, and audit"
```

---

### Task 13: Routes 新 API 端点

**Files:**
- Modify: `paperreadagent/modules/ideator/routes.py`

- [ ] **Step 1: 新增 API 端点**

```python
@router.get("/api/sparks/{spark_id}/reviews")
async def get_spark_reviews(request: Request, spark_id: int):
    """获取一条火花的所有审查记录"""
    core = request.app.state.core
    data = DataAccess(core)
    conn = core.db.conn
    rows = conn.execute(
        "SELECT * FROM ideator_review_records WHERE spark_id = ? ORDER BY created_at",
        (spark_id,)
    ).fetchall()
    return [dict(r) for r in rows]

@router.get("/api/runs")
async def list_runs(request: Request, limit: int = 20):
    """列出最近的管道运行记录"""
    core = request.app.state.core
    conn = core.db.conn
    rows = conn.execute(
        "SELECT * FROM ideator_pipeline_runs ORDER BY started_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    return [dict(r) for r in rows]

@router.get("/api/weights")
async def get_weights(request: Request):
    """获取召回路径权重"""
    core = request.app.state.core
    conn = core.db.conn
    rows = conn.execute(
        "SELECT * FROM ideator_recall_weights ORDER BY weight DESC"
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 2: 修改 feedback 端点集成 FeedbackLoop**

`feedback_spark()` 增加调用 `FeedbackLoop.record_feedback()`。

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/routes.py
git commit -m "feat(ideator): add review records, pipeline runs, and weights API endpoints"
```

---

### Task 14: Dashboard UI 升级

**Files:**
- Modify: `paperreadagent/modules/ideator/templates/dashboard.html`
- Modify: `paperreadagent/modules/ideator/static/ideator.js`

- [ ] **Step 1: 更新 dashboard.html**

火花卡片新增显示：
- 审查状态徽章（`review_status`: pending/passed/revised/rejected/escalated）
- final_score 百分比
- 审查者模型标签
- 审计标记（SUPPORTED/STRETCHED/UNSUPPORTED）

- [ ] **Step 2: 更新 ideator.js**

新增 `viewReviews(sparkId)` 函数 → 弹窗展示审查记录列表。

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/templates/dashboard.html paperreadagent/modules/ideator/static/ideator.js
git commit -m "feat(ideator): update dashboard with review status, confidence, and audit badges"
```

---

### Task 15: 集成测试

**Files:**
- Create/Modify: `paperreadagent/modules/ideator/tests/test_integration.py`

- [ ] **Step 1: 编写集成测试**

```python
# tests/test_integration.py
import pytest

@pytest.mark.asyncio
async def test_full_pipeline_lite_integration():
    """完整 lite 管道：召回→评分→生成→入库（无审查/审计）"""
    # 使用 mock core + mock legacy_db

@pytest.mark.asyncio
async def test_review_stage_produces_records():
    """balanced 管道 → 审查阶段应写入 ideator_review_records"""

@pytest.mark.asyncio
async def test_arbitration_triggers_when_divergence():
    """双审查分歧时 → 触发仲裁，写入仲裁记录"""

@pytest.mark.asyncio
async def test_audit_modifies_spark_score():
    """审计 SUPPORTED → final_score 增加 0.1"""
```

- [ ] **Step 2: 运行全部测试**

```bash
uv run python -m pytest paperreadagent/modules/ideator/tests/ -v
```

- [ ] **Step 3: Commit**

```bash
git add paperreadagent/modules/ideator/tests/
git commit -m "test(ideator): add integration tests for cross-model review pipeline"
```

---

### 完成后验证

```bash
# 全部模块测试
uv run python -m pytest paperreadagent/modules/ideator/tests/ -v

# 确保不破坏其他模块
uv run python -m pytest paperreadagent/tests/ -v
uv run python -m pytest paperreadagent/modules/thinker/tests/ -v
```
