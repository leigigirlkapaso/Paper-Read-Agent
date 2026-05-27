# Ideator 火花深度讨论圆桌

> 多模型实时圆桌讨论功能。用户对火花高度感兴趣时，发起 6 模型坐席的深度对话。借鉴 ARIS 跨模型对抗协作方法论。

**日期：** 2026-05-13
**状态：** 设计完成
**前置依赖：** 跨模型对抗审查升级（`docs/superpowers/specs/2026-05-13-ideator-cross-model-review.md`）

---

## 一、动机

当前 ideator 管道是自动化流水线——召回→评分→生成→审查→去重→深化。但当用户对某个火花特别感兴趣时，缺少一个让用户直接与所有模型实时交流的场所。圆桌填补这个空白：用户可以点名提问、多模型同时回答、辩论、被挑战——最终形成比自动化管道更深的理解。

---

## 二、模型坐席（6 坐席）

| # | 模型 | 身份 | 圆桌视角 | Token 预算 |
|---|------|------|---------|-----------|
| 1 | deepseek-v4-pro | 生成者 (Gen) | "我创造了这个火花，我的推理是..." | 1M |
| 2 | deepseek-v4-pro | 审查者3 (Rev3) | 独立审查——同模型不同实例，不带创作偏见 | 1M |
| 3 | gemini-3-flash-preview | 审查者1 (Rev1) | "从审查角度看..." | 默认 |
| 4 | qwen3.6-plus | 审查者2 (Rev2) + 审计者 | "证据链显示..." | 默认 |
| 5 | claude-opus-4-7-max | 仲裁者1 (Arb1) | "综合分歧，我的裁决..." | 1M |
| 6 | gpt-5.5-2026-04-24 | 仲裁者2 (Arb2) | "深度扩展视角..." | 默认 |

**Rev3 的特殊性：** Gen 和 Rev3 使用同一 deepseek 模型但**独立实例**（不同 system prompt，无火花创作记忆）。Gen 对火花天然有偏，Rev3 提供同源但无偏见的审查视角。

**硬性不变量：**
- Gen(deepseek) 永远不能当审查者或仲裁者；Rev3(deepseek) 是审查者，非生成者
- gemini 和 qwen 彼此独立，不串联
- 仲裁者始终在场，用户是唯一最高权力

---

## 三、对话机制

### 3.1 基本流程

```
用户提问 + 选择模型（点击标签 / @模型名）
  ↓
被指名模型独立并行回答
  ↓
全部模型（含未指名）看到完整时间线
  ↓
未指名模型可插话（限 150 字）
  ↓
用户看到所有回复，决定下一轮
  ↓
循环直到用户关闭或所有模型退场
```

### 3.2 模型选择方式

- **标签点击**：输入框上方 5 个模型标签 + @全部，点击切换选中/未选中
- **文本 @ 指定**：输入框内 `@deepseek` `@qwen` 等，精确指定
- **两者同时生效**：标签选中的是默认发送对象，文本 @ 可以覆盖

### 3.3 插话规则

- 未指名模型可主动插话，限 150 字
- 插话标记为红色边框，显示 "▲ 插话"
- 插话引用 parent 消息 ID，关联到被插话的轮次

---

## 四、Token 溢出控制

### 4.1 三级阈值

```
50% — 自动自压缩
  → 模型对自己全部历史对话做摘要
  → 摘要 ≤ 原始长度 30%
  → 压缩后上下文 = 系统 prompt + 摘要 + 最近 2 轮完整消息
  → 写入 roundtable_messages（message_type=compression）
  → 快照记录压缩前后 token 数

85% — 黄色预警
  → UI 状态栏显示黄点 + 百分比
  → 用户可决定是否继续 @ 此模型

100% — 强制退场
  → 自动生成离场声明（总结立场 + 未充分回应的问题）
  → UI 灰点 disabled + 系统消息通知
  → 其他模型继续讨论不受影响
```

### 4.2 用户强制退场

- 鼠标悬停模型状态标签 → "移除"按钮
- 退场时同样生成离场声明
- 写入 roundtable_messages（message_type=exit_statement）

### 4.3 自压缩实现

调用被压缩模型自身对历史做摘要（保持视角一致性），提示词：

> "请将以下讨论历史压缩为一份简洁摘要，保留所有关键论点、分歧和证据引用。摘要不超过原文 30%。"

---

## 五、上下文分层

每个坐席进入圆桌时，系统根据角色自动组装上下文包：

| 资料 | Gen | Rev1 | Rev2 | Rev3 | Arb1 | Arb2 |
|------|:---:|:----:|:----:|:----:|:----:|:----:|
| 火花内容 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 源论文全文 (Markdown) | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| AI 精读报告 | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| 用户笔记 | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| 管道 S3 审查记录 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| S5 深化结果 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| Gen 自评分数 | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |

**设计逻辑：**
- **Gen** 看全部——它是创造者，需要引用原文辩护。唯一能看到自评分的人
- **Rev1/2/3** 看论文+笔记+审查——足够审查证据链。不看自评分，保持独立
- **Arb1/2** 只看火花+审查+深化——不看原文防止带入生成者视角，保持裁决独立

**Token 策略：** deepseek 和 claude-opus 开最大 token 限制（1M）。论文全文加载，token 溢出靠 50% 自压缩机制兜底。

**Arb 补资料：** 讨论中涉及原文引用时，用户通过"附资料"按钮手动给 Arb 附上原文片段。

**中途补充：** 用户可在对话中随时通过"附资料"按钮给任意模型补充上下文，不影响其他模型。

---

## 六、与管道的关系

- 圆桌在 S5 deepen **之后**，基于 deepen 后的火花发起
- 圆桌不依赖管道实时状态——管道已完成的火花随时可开圆桌
- 圆桌结果独立存储，不影响管道原有的 review_records
- `ideator_sparks` 新增可选字段 `roundtable_id`，指向活跃/最近圆桌

---

## 七、数据库设计

### 6.1 新增表: ideator_roundtables

| 列名 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 主键 |
| spark_id | INTEGER FK | 关联火花 |
| status | TEXT | active / paused / closed |
| participants | TEXT (JSON) | [{model, role, state, token_used, token_limit, compression_count}] |
| round_count | INTEGER | 当前轮次 |
| started_at | TEXT | 开始时间 |
| closed_at | TEXT | 结束时间 |

### 6.2 新增表: ideator_roundtable_messages（核心）

| 列名 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 主键 |
| roundtable_id | INTEGER FK | 关联圆桌 |
| round_number | INTEGER | 第几轮 |
| sender_type | TEXT | user / model / system |
| sender_name | TEXT | user / deepseek / gemini-flash / qwen3.6-plus / claude-opus / gpt-5.5 |
| sender_role | TEXT | gen / reviewer_1 / reviewer_2 / arbiter / null |
| message_type | TEXT | question / answer / interjection / compression / exit_statement / divergence_report |
| content | TEXT | 消息正文（Markdown） |
| word_count | INTEGER | 字数 |
| mentioned_by | TEXT (JSON) | 被 @ 的模型名列表 |
| parent_id | INTEGER FK | 插话时引用的消息 ID |
| metadata | TEXT (JSON) | {token_used, compression_triggered, ...} |
| created_at | TEXT | 时间戳 |

### 6.3 新增表: ideator_roundtable_snapshots

| 列名 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 主键 |
| roundtable_id | INTEGER FK | 关联圆桌 |
| message_id | INTEGER FK | 关联消息 |
| model_name | TEXT | 模型名 |
| model_role | TEXT | 角色 |
| round_number | INTEGER | 轮次 |
| prompt_sent | TEXT | 完整 messages 数组（JSON） |
| raw_response | TEXT | 模型原始返回 |
| tokens_input | INTEGER | 输入 token |
| tokens_output | INTEGER | 输出 token |
| tokens_total | INTEGER | 总 token |
| token_pct_used | REAL | 累计使用百分比 |
| compression_triggered | INTEGER | 0/1 |
| compression_summary | TEXT | 压缩后摘要 |
| exit_reason | TEXT | token_exhausted / user_forced / null |
| created_at | TEXT | 时间戳 |

### 6.4 扩展表: ideator_sparks

新增列：`roundtable_id INTEGER` — 指向活跃/最近圆桌。

---

## 八、自动分歧分析

每轮结束后，用 gemini-flash 扫描本轮所有消息：
- 识别分歧点（哪个模型在什么问题上与谁不同）
- 识别共识点（哪些结论被多模型认可）
- 分歧强度：minor / moderate / major
- 写入 roundtable_messages（message_type=divergence_report，sender_type=system）

---

## 九、文件变更清单

| 文件 | 操作 | 复杂度 |
|------|------|--------|
| `modules/ideator/roundtable.py` | **新建** — 圆桌引擎 | 高 |
| `modules/ideator/schema.py` | 修改 — v3 迁移（3 新表 + 1 列） | 中 |
| `modules/ideator/data_access.py` | 修改 — 圆桌 CRUD | 中 |
| `modules/ideator/prompts/compress_history.jinja2` | **新建** — 自压缩 prompt | 低 |
| `modules/ideator/prompts/exit_statement.jinja2` | **新建** — 离场声明 prompt | 低 |
| `modules/ideator/prompts/divergence_scan.jinja2` | **新建** — 分歧扫描 prompt | 低 |
| `modules/ideator/prompts/roundtable_model_answer.jinja2` | **新建** — 圆桌回答系统 prompt | 低 |
| `modules/ideator/templates/roundtable_modal.html` | **新建** — 弹窗模板 | 中 |
| `modules/ideator/static/roundtable.js` | **新建** — 圆桌前端 | 高 |
| `modules/ideator/static/roundtable.css` | **新建** — 弹窗样式 | 低 |
| `modules/ideator/routes.py` | 修改 — 圆桌 API 端点 | 中 |
| `modules/ideator/templates/dashboard.html` | 修改 — 火花卡片 + 圆桌按钮 | 低 |
| `modules/ideator/static/ideator.js` | 修改 — 圆桌弹窗触发 | 低 |
| `modules/ideator/tests/test_roundtable.py` | **新建** — 圆桌测试 | 高 |
