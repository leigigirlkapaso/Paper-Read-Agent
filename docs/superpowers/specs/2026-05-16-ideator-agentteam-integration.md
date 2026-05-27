# AgentTeam 接入圆桌设计

**日期：** 2026-05-16
**范围：** 用 AgentTeam 替代旧 RoundtableSession，接入前端圆桌流程

---

## 背景

当前 ideator 模块有两套圆桌实现：

| | RoundtableSession（旧） | AgentTeam（新） |
|------|------|------|
| **模型** | gemini/qwen/claude/gpt 混用 | 统一 deepseek-v4-pro |
| **身份** | 暴露模型名 | 身份伪装 prompt |
| **上下文** | chars//3 估算，每坐席独立压缩 | Arbiter 3 层毕业 |
| **记忆** | 无 | 9 类结构化团队记忆 |
| **配额** | 无 | Arbiter 动态调控 |
| **工具** | 无 | 14 工具 + RBAC |
| **前端接入** | 已接入 | 未接入 |

**目标：** AgentTeam 替代 RoundtableSession，成为前端「发起圆桌」的唯一实现。

---

## 设计决策

### 1. 坐席定义

抽离到 `agent_team.py` 工厂方法 `create_default_seats()`。6 坐席统一 deepseek-v4-pro + 身份伪装 prompt。Arb1/Arb2 工具完全对等。

### 2. 上下文

所有 6 坐席统一看到完整来源上下文（论文标题+摘要+笔记+审查记录+深化内容），不做角色隔离。

System Prompt 结构：`身份伪装 prompt + 火花内容 + 来源上下文 + 团队记忆`

### 3. 毕业时机

热层 >60% 自动触发毕业。Arbiter 裁量：保留/压缩/毕业。

### 4. 前端（完整版）

圆桌弹窗包含以下区域：

**顶部状态栏：**
- 轮次计数器（第 N 轮）
- 上下文水位条：hot ████░░ warm ██░░░░ cold ░░░░░░ 三色分段，悬停显示百分比
- 毕业状态指示（正常/待毕业/已毕业）

**坐席面板（6 个折叠卡片，默认展开）：**
- 每坐席：角色图标 + 名称 + 配额剩余进度条 + 状态（online/exhausted/exited）
- 颜色区分：Gen 绿 / Rev 蓝 / Arb 紫
- 点击展开/折叠该坐席的历史发言
- 右键菜单：强制移除

**消息区：**
- 坐席发言：角色色条左边框 + 内容
- 插话：灰色小字 + `⚡插话` 标记 + 发言者
- 分歧报告：黄色背景 + `⚡分歧` 标记
- 系统消息：灰色居中（压缩/毕业/退场）
- 用户消息：右对齐蓝色气泡

**底部操作栏：**
- 提问输入框 + @ 坐席快捷选择
- 发送按钮
- 手动毕业按钮（无论阈值，随时触发）
- 补充资料按钮（中途注入新上下文）
- 暂停/恢复按钮
- 结束圆桌按钮

**侧边面板（可收起）：**
- 团队记忆查看（9 类分 tab）
- 工具调用历史（谁在什么时候调了什么工具）

---

## 完整管道链路

```
S0 CrossRecall (6路) → S1 Per-Pair Score (逐对LLM) → S2 Per-Group Spark Gen (C1分组)
→ S3 Dual Review → S4 Dedup Save
→ [圆桌: AgentTeam 6坐席讨论 → Arbiter毕业]
→ S5 Deepen (按需) → S6 Audit
```

---

## AgentTeam 架构

```
__init__.py 创建:
  TeamMemory(core.db.conn)
  + GraduationManager(core.db.conn, team_memory)
  + ToolRegistry → create_default_registry()
  + Arbiter(llm=IdeatorLLM, graduation, tool_registry, team_memory)
  + AgentTeamManager(llm, data_access, tool_registry, team_memory, graduation, arbiter)
  → 暴露为模块级单例

前端流程:
  POST /api/sparks/{id}/roundtable/start
  → mgr.create_team(spark_id, spark_content, source_refs)
  → create_default_seats() → AgentTeam

  POST /api/roundtables/{id}/ask
  → team.start_round(question, mentioned)
  → 6 坐席发言 + 插话 + 分歧报告
  → 毕业检查 (hot > 60%)

  POST /api/roundtables/{id}/close
  → team.execute_graduation_cycle() → 清理
```

---

## 文件改动

| 文件 | 操作 | 内容 |
|------|------|------|
| `agent_team.py` | 修改 | 添加 `create_default_seats()`；`_build_agent_system_prompt` 注入来源上下文 |
| `__init__.py` | 修改 | 创建 AgentTeamManager 替代 RoundtableManager |
| `routes.py` | 修改 | 圆桌 API 指向 AgentTeamManager；移除旧 SEATS 定义和 `trigger_graduation` 内联创建 |
| `roundtable.py` | 保留 | 保留但不使用（后续可删除） |
| `ideator.js` | 重写 | 完整圆桌弹窗：状态栏+坐席面板+消息区+操作栏+侧边面板 |
| `ideator.css` | 修改 | 圆桌弹窗样式：坐席色条、水位进度条、消息气泡 |

## API

保持当前路由签名，后端改为 AgentTeamManager 实现：

```
POST /api/sparks/{spark_id}/roundtable/start  → 创建 AgentTeam
POST /api/roundtables/{rt_id}/ask             → team.start_round() 返回消息+水位
GET  /api/roundtables/{rt_id}                 → 完整状态（团队+消息+坐席状态+水位）
POST /api/roundtables/{rt_id}/close           → execute_graduation_cycle() + 关闭
POST /api/roundtables/{rt_id}/graduate        → 手动毕业
POST /api/roundtables/{rt_id}/pause           → 暂停
POST /api/roundtables/{rt_id}/remove/{seat}   → 强制移除坐席
POST /api/roundtables/{rt_id}/supplement      → 中途补充资料
GET  /api/roundtables/{rt_id}/memory          → 团队记忆
GET  /api/roundtables/{rt_id}/watermark       → 上下文水位
GET  /api/roundtables/{rt_id}/tools           → 工具调用历史（新增）
```
