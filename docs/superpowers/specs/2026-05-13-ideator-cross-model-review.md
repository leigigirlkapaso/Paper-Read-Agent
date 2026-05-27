# Ideator 跨模型对抗审查升级

> 为 ideator 模块引入跨模型对抗审查机制，借鉴 ARIS (Auto Research In Sleep) 的 Reviewer Independence Protocol 和 cross-model adversarial collaboration 方法论。

**日期：** 2026-05-13
**状态：** 设计完成

---

## 一、动机

当前 ideator 模块是单模型 4 阶段管道（召回→评分→生成→去重），同一个 deepseek 模型既做关联评分又做火花生成又做深化。根据 ARIS 的分析，单模型自审存在系统性盲区——类比 bandit 问题，单模型是 stochastic bandit（噪声可预测），跨模型审稿是 adversarial bandit（审稿者主动探测执行者未预料的弱点）。

核心升级目标：**在不引入外部文献检索的前提下，通过跨模型对抗审查机制提升火花质量和可信度。**

---

## 二、模型资源

ideator 模块专用模型（与 core.llm 的 deepseek 独立）：

| 模型 | Tier | baseurl |
|------|------|---------|
| deepseek-v4-pro | 生成者（不变） | 现有 core.llm |
| gemini-3-flash-preview | T1 评分 + T2 审查者1 | https://api.gpt.ge/v1 |
| qwen3.6-plus | T2 审查者2 + 审计者 | https://api.gpt.ge/v1 |
| claude-opus-4-7-max | T3 仲裁/深化 | https://api.gpt.ge/v1 |
| gpt-5.5-2026-04-24 | T3 仲裁/深化（备选） | https://api.gpt.ge/v1 |

API Key: 配置于 config.yaml → `core.voice` 同源 (`https://api.gpt.ge/v1`)，由 `ideator_llm.py` 读取

---

## 三、升级后管道

```
Effort Level（自动计算）控制一切参数
│
├── Stage 0: CrossRecall（6路，effort 控制路径数+广度+权重）
├── Stage 1: LinkScorer（T1: gemini-flash 评分）
├── Stage 2: SparkGenerator（deepseek 生成火花 + 自评）
├── Stage 3: SparkReviewer ★新增
│   ├── Reviewer 1: gemini-flash（独立评分+质疑）
│   ├── Reviewer 2: qwen3.6-plus（独立评分+质疑）
│   ├── 两个审查者互不知晓对方评分
│   └── 仲裁触发判断
├── Stage 4: SparkStore（去重入库 + 审查/审计字段）
├── Stage 5: SparkDeepen ★改为迭代循环
│   └── deepseek 深化 → reviewer 审查 → 修改 → Re-review（最多3轮）
├── Stage 6: SparkAudit ★新增
│   └── qwen3.6-plus 独立验证火花是否被源文本支撑
│
├── PipelineState ★管道状态持久化
└── FeedbackLoop ★反馈→召回权重调整
```

---

## 四、三模型审查与仲裁

### 4.1 审查者独立性（Hard Invariant）

- deepseek 生成的火花必须由非 deepseek 模型审查
- gemini 和 qwen 同时审查，但互不知晓对方评分
- 审查 prompt 中不传递 deepseek 的自评分数
- deepseek 的自评不参与任何仲裁判断

### 4.2 仲裁触发条件

输入：R1=gemini-flash 评分, R2=qwen3.6-plus 评分（各 0.0-1.0，含三维度 novelty/evidence/feasibility）

| 条件 | 动作 |
|------|------|
| R1 ≥ 0.8 AND R2 ≥ 0.8 | 高价值火花 → Tier 3(claude-opus) 深度扩展 |
| \|R1 - R2\| ≥ 0.25 | 审查者分歧 → Tier 3(claude-opus) 终裁决 |
| R1 ≤ 0.4 AND R2 ≤ 0.4 | 双否定 → REJECT（不浪费 Tier 3） |
| 其他 | REVISE → PASS（根据审查反馈自动微调火花内容后标记 PASS，不升级 Tier 3） |

### 4.3 审查评分维度

每个审查者对火花评估三个维度（各 0.0-1.0）：
- **新颖性 (novelty):** 这个火花是否提出了新的视角/关联/假设？
- **证据支撑度 (evidence):** 火花的主张在源文本中有多强的支撑？
- **可行性 (feasibility):** 这个研究方向是否有实际可操作的下一步？

---

## 五、Effort Level 自动调整

### 5.1 自动决策因子

```
auto_effort(ctx) → effort:
    score = 0.0
    + 0.25 if candidate_count > 15
    + 0.25 if total_papers > 30
    + 0.20 if useful_ratio > 0.4
    + 0.15 if hours_since_full > 12
    + 0.15 if trigger == "daily_cron"
    + 0.30 if trigger == "user_manual"

    if score < 0.25 → lite
    if score < 0.50 → balanced
    if score < 0.80 → max
    else          → beast
```

### 5.2 四级控制表

| 维度 | lite | balanced | max | beast |
|------|------|----------|-----|-------|
| 召回路径 | 3路 | 4路 | 6路 | 6路×2 |
| 采样量 | 各1条 | 各2-3条 | 各5条 | 各10条 |
| 火花生成 | 2-3条 | 3-5条 | 5-7条 | 7-10条 |
| 双审查 | 跳过 | top-2 | 全部 | 全部 |
| Tier 3 仲裁 | 不走 | 不走 | 仅争议 | 争议+高价值 |
| 火花深化 | 手动触发 | 手动触发 | PASS自动 | 全部自动 |
| 深化迭代轮数 | 1轮 | 1轮 | 最多2轮 | 最多3轮 |
| 溯源审计 | 跳过 | top-1抽查 | 全部PASS | 全部 |

### 5.3 硬性不变量（不随 effort 变化）

- **生成者 ≠ 审查者:** deepseek 生成的火花必须由非 deepseek 模型审查
- **双审查独立性:** gemini 和 qwen 审查时互不知晓对方评分
- **溯源不可跳过:** beast 模式强制审计全部火花

---

## 六、深度深化迭代循环（S5）

```
deepseek 生成深化草案
    ↓
reviewer (qwen/gemini) 审查草案
    检查：推理是否过度？证据是否充分？
    ↓
score ≥ 0.7 → done（写入，状态=deep_done）
score < 0.7 → 根据反馈修改草案，重新审查
    最多 3 轮，第 3 轮直接接受
```

---

## 七、溯源审计（S6）

独立模型（qwen3.6-plus，≠ deepseek 生成者）验证火花：

**输入：** 火花内容 + source_refs 指向的原始文本（截取 500 字/条）

**三种判决：**
- **SUPPORTED:** 火花的每个 claim 都能在源文本中找到证据 → quality_score +0.1
- **STRETCHED:** 部分关联牵强但方向合理 → 标记 metadata.audit_flag="stretched"
- **UNSUPPORTED:** 主要 claim 在源文本中找不到支撑 → quality_score -0.3, review_status="flagged"

---

## 八、反馈闭环

用户反馈（useful/duplicate/noise）→ 按 source_type 聚合 → 调整 6 路召回权重

| source_type | 默认权重 | 调整 |
|-------------|---------|------|
| similarity | 1.0 | useful +0.05 / noise -0.1 |
| contradiction | 1.0 | 同上 |
| cross_project | 1.0 | 同上 |
| cross_layer | 1.0 | 同上 |
| random_walk | 0.5 | 同上（默认权重较低） |
| timeline | 1.0 | 同上 |

权重范围 [0, 2.0]。权重 < 0.2 → 该路径暂时关闭。

---

## 九、留痕系统（数据库设计）

### 9.1 新增表: ideator_pipeline_runs

| 列名 | 类型 | 说明 |
|------|------|------|
| run_id | TEXT PK | UUID 唯一运行标识 |
| trigger | TEXT | event / cron / manual |
| effort | TEXT | lite / balanced / max / beast（auto） |
| stages_completed | TEXT (JSON) | 完成的阶段列表 |
| stats | TEXT (JSON) | 统计信息 |
| total_tokens | INTEGER | 总 token 消耗 |
| error | TEXT | 异常中断原因 |
| started_at | TEXT | 开始时间 |
| finished_at | TEXT | 结束时间 |

### 9.2 新增表: ideator_review_records

| 列名 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 主键 |
| spark_id | INTEGER FK | 被审查的火花 |
| stage | TEXT | review / arbitration / audit |
| reviewer_model | TEXT | 模型名 |
| reviewer_role | TEXT | reviewer_1 / reviewer_2 / arbiter / auditor |
| scores | TEXT (JSON) | {"novelty":0.7, "evidence":0.8, "feasibility":0.6} |
| verdict | TEXT | PASS / REVISE / REJECT / ARBITRATE / OVERTURN |
| reasoning | TEXT | 审查理由（Markdown） |
| prompt_snapshot | TEXT | 完整 prompt |
| raw_response | TEXT | 模型原始返回 |
| token_usage | TEXT (JSON) | {"input":N, "output":M} |
| escalation_reason | TEXT | 升级到仲裁的原因 |
| run_id | TEXT FK | 关联的管道运行 |
| created_at | TEXT | 时间戳 |

### 9.3 扩展表: ideator_sparks

新增列：
- `run_id TEXT` — 产生此火花的管道运行
- `generator_score REAL` — deepseek 自评分数
- `final_score REAL` — 综合审查后最终分数
- `review_status TEXT` — pending / passed / revised / rejected / escalated
- `review_count INTEGER DEFAULT 0` — 关联的审查记录数

### 9.4 新增表: ideator_recall_weights

| 列名 | 类型 | 说明 |
|------|------|------|
| source_type | TEXT PK | 召回路径标识 |
| weight | REAL DEFAULT 1.0 | 当前权重 |
| useful_count | INTEGER DEFAULT 0 | 近30天 useful 数 |
| noise_count | INTEGER DEFAULT 0 | 近30天 noise 数 |
| updated_at | TEXT | 最后更新时间 |

---

## 十、模块专用 LLM 客户端

新建 `ideator_llm.py`，封装 gpt.ge API 的 LLM 调用。

**职责：**
- 管理与 core.llm 不同的 baseurl 和 API key
- 根据任务类型自动路由到对应 Tier 模型
- 调用记录自动写入 `core_llm_usage`

**不暴露给模块外：** 这是 ideator 模块内部组件，不通过 core 暴露。

---

## 十一、管道状态持久化

每个阶段完成后写入 `PIPELINE_STATE.json`（存储在 `.aris/` 或模块数据目录）：

```json
{
  "run_id": "uuid",
  "current_stage": "review",
  "stages_completed": ["recall", "score", "generate"],
  "candidates_count": 24,
  "sparks_generated": 5,
  "sparks_reviewed": 3,
  "effort": "max",
  "updated_at": "ISO timestamp"
}
```

用途：上下文压缩后恢复管道执行、异常中断后的断点续传。

---

## 十二、文件变更清单

| 文件 | 操作 | 复杂度 |
|------|------|--------|
| `modules/ideator/ideator_llm.py` | **新建** — 模块专用 LLM 客户端 | 中 |
| `modules/ideator/reviewer.py` | **新建** — 双审查引擎 + 仲裁触发 | 高 |
| `modules/ideator/auditor.py` | **新建** — 溯源审计 | 低 |
| `modules/ideator/state.py` | **新建** — 管道状态持久化 | 低 |
| `modules/ideator/effort.py` | **新建** — 自动 effort 计算 | 低 |
| `modules/ideator/feedback_loop.py` | **新建** — 反馈→权重调整 | 低 |
| `modules/ideator/schema.py` | 修改 — v2 迁移（4 张表变更） | 中 |
| `modules/ideator/pipeline.py` | **重写** — 集成审查+迭代+审计 | 高 |
| `modules/ideator/cross_recall.py` | 修改 — effort 驱动 + 权重反馈 | 中 |
| `modules/ideator/spark_store.py` | 修改 — 新字段 + 审查追踪 | 中 |
| `modules/ideator/config.default.yaml` | 修改 — 新模型配置 | 低 |
| `modules/ideator/prompts/review_spark.jinja2` | **新建** — 审查 prompt | 低 |
| `modules/ideator/prompts/audit_spark.jinja2` | **新建** — 审计 prompt | 低 |
| `modules/ideator/prompts/arbitrate_spark.jinja2` | **新建** — 仲裁 prompt | 低 |
| `modules/ideator/routes.py` | 修改 — 新 API（审查记录查询、权重面板） | 中 |
| `modules/ideator/templates/dashboard.html` | 修改 — 显示审查状态、置信度、审计标记 | 中 |
| `modules/ideator/static/ideator.js` | 修改 — 审查详情弹窗 | 低 |
| `modules/ideator/tests/` | **大幅扩展** — 覆盖所有新组件 | 高 |

---

## 十三、与 ARIS 方法论对应

| ARIS 概念 | ideator 适配 |
|-----------|-------------|
| Cross-Model Adversarial Review | deepseek vs gemini+qwen 双审查 |
| Reviewer Independence Protocol | 双审互不知晓、不传自评分 |
| Auto-Review Loop (review→fix→re-review) | SparkDeepen 迭代深化（最多 3 轮） |
| Experiment Integrity Protocol | 溯源审计（独立模型验证源文本支撑） |
| Effort Levels (lite/balanced/max/beast) | 自动 effort 计算 + 四级参数表 |
| Review Tracing Protocol | 双表留痕（pipeline_runs + review_records） |
| Meta-Optimize | 反馈闭环（用户反馈→召回权重调整） |

## 十四、不借鉴的 ARIS 内容

以下 ARIS 功能明确**不引入**：
- Novelty Check（外部文献检索）— 用户已有充足论文笔记，不需要
- Research Wiki（持久化知识库）— 已有 core_notes
- Overleaf Sync / Paper Slides / Rebuttal — 不属于 ideator 职责范围
- MCP Server 机制 — ideator 通过模块专用 HTTP client 直连
