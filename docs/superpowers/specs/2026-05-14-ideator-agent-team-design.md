# Ideator Agent Team 架构升级设计

> **目标：** 将火花模块从"管道编排+被动圆桌"升级为真正的 Agent Team——6 个模型角色自主协作、共享上下文、调用工具、产生并迭代研究火花。

> **核心理念：** 管道生产火花 → Agent Team 协作讨论 → 结构化记忆持久化。用户从外部指挥官变为团队平等参与者。

---

## 1. 关键设计决策（9 项已确认）

| # | 决策 | 选择 | 要点 |
|---|------|------|------|
| 1 | Agent 自主权 | 完全自主 | 可查论文、读笔记、建火花、触发增量召回（系统内） |
| 2 | 底层模型 | 全部 deepseek-v4-pro | 6 坐席同模型，互不知晓，角色伪装成独立个体 |
| 3 | 发言规则 | 完全开放 + Arbiter 动态调控字数 | Agent 随时发言，Arbiter 根据上下文水位动态分配每人本轮配额 |
| 4 | 上下文压缩 | Arbiter 裁量 + 三层毕业 | 热/温/冷三层，信息毕业不丢弃，Agent 可 fetch 冷层 |
| 5 | 工具分配 | 按角色 + Arbiter 临时授权 | Gen 生成类 / Rev 审查类 / Arb 管理类 |
| 6 | 管道与团队 | 管道先生产 → 团队后讨论 | 讨论中可触发增量召回，仅限系统内已有论文（跨项目） |
| 7 | 增量召回约束 | 异步不等待 · 最多 2 次 · 精准锚定 | Arbiter 批准时写明搜索方向，用户可随时叫停 |
| 8 | 字数限制 | Arbiter 动态调控 | 每轮结束后评估水位，自适应调整下轮配额 |
| 9 | 搜索范围 | 仅系统内已有论文 | 不触发外部检索 |

---

## 2. 架构全景

```
                      管道层（S0-S4）
  CrossRecall → LinkScorer → SparkGenerator → SparkStore → Deepener
       ↑                                                    ↓
       │             定时全量(凌晨3点) + 事件增量 + 手动      │
       │                                                    ↓
       │              火花推入 ideator_sparks + core_notes    │
       │                                                    ↓
       │         ┌──────────────────────────────────────────┐│
       │         │        Agent Team 圆桌讨论               ││
       │         │                                          ││
       │         │  Gen ←→ Rev1 ←→ Rev2 ←→ Rev3            ││
       │ 增量召回 │         ↕           ↕                    ││
       │ (系统内) │  Arb1 ←→ Arb2 ←→ User                   ││
       │         │                                          ││
       │         │  ┌────── 共享上下文 ──────┐              ││
       │         │  │ 🔥热层~300K  当前讨论   │              ││
       │         │  │ 🌤温层~200K  历史摘要   │              ││
       │         │  │ ❄冷层 DB    完整快照   │              ││
       │         │  └────────────────────────┘              ││
       │         │                                          ││
       │         │  结构化团队记忆 (9类)                     ││
       │         │  工具注册表 (RBAC)                        ││
       │         └──────────────────────────────────────────┘│
       └─────────────────────────────────────────────────────┘
```

---

## 3. 6 坐席定义

所有坐席底层使用 deepseek-v4-pro，但 System Prompt 伪装为独立身份。

| Seat ID | 角色标签 | 身份伪装要点 | 工具权限 | 上下文预算 |
|---------|----------|-------------|----------|-----------|
| gen | 创意生成者 | 跨领域联想专家，不知有其他审查者存在 | 查论文、读笔记、embedding 搜索、创建火花、触发增量召回 | 40% |
| rev1 | 独立审查者 Alpha | 以批判性思维著称，不受创作偏见影响 | 查论文、embedding 搜索、查历史快照 | 25% |
| rev2 | 独立审查者 Beta | 证据导向的审计专家，不信任未验证主张 | 查论文、溯源审计、查历史快照 | 25% |
| rev3 | 独立审查者 Gamma | 独立复核者，与生成者无关联 | 查论文、读笔记、查历史快照 | 25% |
| arb1 | 资深仲裁者 Alpha | 圆桌最高权威，负责裁决 + 调控 + 毕业决策 | 查历史快照、团队成员记忆读写、上下文调控、临时授权 | 20% |
| arb2 | 资深仲裁者 Beta | 深度扩展专家，负责火花深化 + 方向探索 | 查历史快照、团队成员记忆读写、深化火花 | 20% |
| user | 人类参与者 | — | 发言、@mention、裁决、附资料、叫停 | 不限 |

**身份保密规则：**
- System Prompt 绝不出现 "deepseek-v4-pro" 或任何模型标识
- 讨论历史中消息来源显示角色标签（"创意生成者"），不显示模型名
- 每个坐席收到不同的角色描述和思维风格提示
- 审查者被暗示"与生成者来自不同背景"

---

## 4. Agent 工具层

### 4.1 工具定义

| 工具名 | 功能 | 默认角色 |
|--------|------|---------|
| `search_papers` | 在系统论文库中 embedding 搜索 | Gen, Rev1, Rev2, Rev3 |
| `read_paper` | 获取论文全文（指定 arxiv_id + 章节） | Gen, Rev1, Rev2, Rev3 |
| `read_note` | 获取指定笔记/报告 | Gen, Rev3 |
| `audit_claim` | 溯源审计：验证主张是否被源文本支撑 | Rev2 |
| `create_spark` | 创建新火花（draft 状态） | Gen（Rev 可提议但需 Gen 执行） |
| `update_spark` | 修改火花内容（记录演化历史） | Gen |
| `check_duplicate` | 检查火花是否与已有火花重复 | Gen |
| `trigger_recall` | 触发增量交叉召回（系统内） | Gen（需 Arbiter 批准） |
| `fetch_snapshot` | 从冷层取回历史讨论快照 | 全部 |
| `write_memory` | 写入结构化团队记忆 | Arb1, Arb2 |
| `read_memory` | 读取团队记忆（指定类别） | 全部 |
| `report_watermark` | 报告当前上下文水位 | Arb1 |
| `adjust_quota` | 调整下轮某角色字数配额 | Arb1 |
| `grant_tool` | 临时授权某 Agent 额外工具 | Arb1 |

### 4.2 授权流程

```
Agent 请求工具 X → Arb1 评估 → 
  ├─ 批准 → grant_tool(agent, tool, reason, duration)
  ├─ 拒绝 → 附带理由写入消息
  └─ 需人类裁决 → @User 请求决定
```

---

## 5. 上下文管理：三层毕业制

### 5.1 三层架构

| 层 | 容量 | 内容 | 生命周期 |
|----|------|------|---------|
| 🔥 热层 | ~300K tokens | 当前轮完整讨论 + 上一轮 Arbiter 摘要 + 角色指令 | 每轮结束 → Arbiter 决定保留/压缩/丢弃 |
| 🌤 温层 | ~200K tokens | 历史轮次的结构化摘要 + 9 类团队记忆全量 | 每轮 Arbiter 更新，旧摘要可再次压缩 |
| ❄ 冷层 | DB 无限 | 每轮完整讨论原文快照 | 存入 `ideator_roundtable_snapshots`，上下文只存引用 ID |

### 5.2 毕业流程（Arbiter 主导，每轮结束）

1. **评估本轮价值** — 有新共识/分歧/决策？还是纯重复？
2. **提取结构化记忆** — 共识→共识清单，分歧→分歧清单，决策→决策日志...
3. **写入冷层快照** — 本轮完整原文 → `ideator_roundtable_snapshots`
4. **生成温层摘要** — 替换为结构化摘要 + DB 引用 ID
5. **报告水位** — "热层 45% | 温层 60% | 建议下轮收紧/放宽"
6. **调整下轮配额** — >60% 收紧到 0.7×，<30% 放宽到 1.5×

### 5.3 冷层取回

Agent 需要查看历史完整讨论时，调用 `fetch_snapshot(round_number)` → 内容临时进入热层 → 本轮结束后再次毕业。

### 5.4 Token 估算

- 统一使用 tiktoken（deepseek-v4-pro 兼容 tokenizer）
- 替代当前 `chars // 2` 启发式
- 模型统一后 tokenizer 统一，估算准确

---

## 6. 结构化团队记忆（9 类）

| # | 记忆类别 | 内容 | 更新者 |
|---|---------|------|--------|
| 1 | 共识清单 | 所有人同意的点 + 达成时间 | Arb1/Arb2 |
| 2 | 分歧清单 | 各方立场 + 分歧程度 + 是否已解决 | Arb1/Arb2 |
| 3 | 决策日志 | 谁决定什么 + 时机 + 依据 | Arb1/Arb2 |
| 4 | 火花演化树 | spark v1→vN 变更历史 + 每版本评分 | Gen |
| 5 | 证据索引 | 关键引用论文/笔记 + 引用位置 | Rev2 |
| 6 | 用户反馈 | 人类表态 + 偏好 + 约束 | Arb1（记录） |
| 7 | 开放问题 | 搁置待解问题 + 搁置原因 | Arb1/Arb2 |
| 8 | 假设记录 | 讨论中的隐含前提 + 可检验条件 | Rev1/Rev3 |
| 9 | 水位标记 | 每轮热/温层使用率 + 压缩历史 | Arb1 |

**存储：** 新建表 `ideator_team_memory`，每类记忆按 `spark_id + memory_type` 索引。温层加载时全量读入。冷层不存记忆（记忆本身就是压缩产物）。

---

## 7. 讨论生命周期

```
1.  管道产出火花 → 推入 ideator_sparks
2.  用户发起圆桌 → 创建 Team Session
    加载：火花 + 来源论文 + 历史团队记忆 + 角色指令
3.  讨论循环（无固定轮次）：
    a. Agent 自由发言，调用工具
    b. Arbiter 动态调控每人本轮配额
    c. 用户随时插话/@mention/裁决/附资料
    d. 本轮自然结束（Arbiter 判断讨论充分 或 用户手动进入压缩）
4.  Arbiter 毕业决策：
    a. 提取共识/分歧/决策/假设 → 写团队记忆
    b. 完整讨论原文 → 冷层快照 (ideator_roundtable_snapshots)
    c. 生成讨论摘要 → 温层
    d. 报告水位 + 调整下轮配额
5.  如需新素材 → Arbiter 批准增量召回（精准锚定，最多 2 次）
    → 后台异步执行 → 新素材推入共享上下文
6.  重复 3-5
7.  用户关闭 或 Arbiter 判定讨论充分 → 终止
    全部记忆持久化，火花评分更新，冷层归档
```

---

## 8. 增量召回约束

讨论中 Agent 发现信息不够时，由 Arbiter 评估是否触发：

**触发条件：** Agent 明确请求 + Arbiter 判断有必要 + 当前火花未超 2 次上限

**执行方式：**
- 后台异步运行，团队不等待
- 限定范围：仅系统内已有论文（可跨项目），不触发外部检索
- Arbiter 须写明精准搜索方向（如"需要 2019 年后关于振动触觉渲染延迟补偿的实验论文"）
- 避免：模糊描述 → 拒绝执行

**防循环：**
- 每个火花最多 2 次增量召回
- 超过 → Arbiter 标注"证据不足"结束本轮
- 用户可随时叫停

**防漂移：**
- 任何 Agent 发现讨论偏离原始查询方向时可以指出
- 召回结果标注与原始查询的距离

---

## 9. 文件变更清单

### 新建（7 个）

| 文件 | 职责 |
|------|------|
| `modules/ideator/agent_team.py` | Team 会话管理：创建/运行/关闭、共享上下文、发言循环、消息广播 |
| `modules/ideator/team_memory.py` | 9 类结构化记忆 CRUD：写入/读取/查询/合并 |
| `modules/ideator/tool_registry.py` | 工具定义 + RBAC：工具 Schema、角色权限表、授权流程 |
| `modules/ideator/arbiter.py` | 仲裁逻辑：毕业决策、上下文调控、配额分配、临时授权、增量召回审批 |
| `modules/ideator/graduation.py` | 三层生命周期：热层路由、温层压缩、冷层快照、fetch 取回 |
| `modules/ideator/prompts/agent_identity_*.jinja2` | 6 个身份伪装 System Prompt（gen/rev1/rev2/rev3/arb1/arb2） |
| `modules/ideator/prompts/arbiter_graduation.jinja2` | Arbiter 毕业决策 Prompt |

### 修改（7 个）

| 文件 | 变更 |
|------|------|
| `modules/ideator/roundtable.py` | 会话管理逻辑迁移到 agent_team.py，保留消息记录和 TokenTracker 基本功能 |
| `modules/ideator/pipeline.py` | 添加管道桥接方法：`push_sparks_to_team()`、`run_targeted_recall()` |
| `modules/ideator/cross_recall.py` | 暴露单路召回方法供 Agent 工具调用 |
| `modules/ideator/data_access.py` | 新增团队记忆 CRUD（9 方法）、冷层快照存取 |
| `modules/ideator/schema.py` | v4 迁移：`ideator_team_memory` 表 |
| `modules/ideator/routes.py` | 更新 API：讨论循环改为 SSE 流式、WebSocket 可选 |
| `modules/ideator/static/ideator.js` + `templates/dashboard.html` | UI 适配：实时讨论流、Agent 发言动画、工具调用状态提示 |

### 保留不变

| 文件 | 说明 |
|------|------|
| `ideator_llm.py` | **删除。** 统一使用 core.llm（deepseek API） |
| `auditor.py` | 保留，作为 Rev2 的 `audit_claim` 工具后端 |
| `effort.py` | 保留，管道仍使用 auto-effort |
| `feedback_loop.py` | 保留，用户反馈权重调整 |
| `spark_store.py` | 保留，管道火花去重入库 |
| `state.py` | 保留，管道状态持久化 |
| `reviewer.py` | 保留，管道 S3 双审查逻辑 |

---

## 10. 关键约束与规则

### 10.1 不可逾越

- **不修改 thinker 代码**
- **不触发外部检索**（讨论中搜索仅限系统内论文库）
- **身份保密**：Agent 绝不知道其他坐席使用同一底层模型
- **用户可随时叫停**：增量召回、发言循环、整个讨论
- **管道和团队的关系不变**：管道先生产火花，团队后讨论

### 10.2 设计规则

- 统一使用 core.llm（deepseek API），不再有独立 IdeatorLLM
- Token 计数使用 tiktoken 精确估算
- 上下文压缩由 Arbiter 统一决策，不允许 Agent 各自压缩
- 冷层数据永久可追溯

---

## 11. 数据模型（v4 Schema）

```sql
-- 团队记忆表
CREATE TABLE IF NOT EXISTS ideator_team_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    roundtable_id INTEGER NOT NULL,
    spark_id INTEGER NOT NULL,
    memory_type TEXT NOT NULL,  -- consensus/disagreement/decision/spark_evolution/evidence/user_feedback/open_question/assumption/watermark
    content TEXT NOT NULL,      -- Markdown 正文
    metadata TEXT,              -- JSON 侧车
    round_number INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (roundtable_id) REFERENCES ideator_roundtables(id),
    FOREIGN KEY (spark_id) REFERENCES ideator_sparks(id)
);

CREATE INDEX idx_team_memory_spark ON ideator_team_memory(spark_id, memory_type);
CREATE INDEX idx_team_memory_rt ON ideator_team_memory(roundtable_id);
```

---

## 12. 测试要求

| 类型 | 最低要求 |
|------|----------|
| 单元测试 | agent_team、team_memory、tool_registry、arbiter、graduation 各 ≥3 个 |
| 集成测试 | 完整讨论生命周期 ≥2 个（创建→讨论→毕业→关闭） |
| 契约测试 | 工具 RBAC 授权流程 ≥3 个 |
| 回归测试 | 现有 pipeline/roundtable 测试全部通过 |

---

## 13. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 同模型审同模型，审查软弱 | System Prompt 强化独立性 + 身份伪装 + 对抗性角色描述 |
| Agent 工具滥用 | RBAC + Arbiter 审批 + 用户叫停权 |
| 上下文仍爆炸 | 三层毕业 + 硬上限 + Arbiter 主动收紧 |
| 讨论陷入循环 | 增量召回上限 2 次 + Arbiter 判定终止 |
| core.llm 并发压力 | deepseek API 统一调用，Semaphore 控制并发 |
