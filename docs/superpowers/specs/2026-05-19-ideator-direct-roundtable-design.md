# Direct Roundtable: 无需火花的直接圆桌讨论

**日期**: 2026-05-19
**状态**: 已批准

## 概述

火花页面新增「直接发起圆桌」入口。用户输入研究内容，内容发给 gen 模型作为上下文，用户以普通 user 身份参与 6 坐席圆桌讨论。无需依赖火花管道。

## 设计

### 前端

**入口**：火花页面顶部「全量挖掘」按钮旁加「直接发起圆桌」。

**输入 modal**：点击弹出小型 modal：
- Textarea 输入研究内容（必填）
- 「发起」按钮 + 取消

**圆桌界面**：完全复用现有 modal，无差异化——6 坐席不变，@mention 不变，所有交互不变。

### 后端

**新路由**：`POST /ideator/api/roundtable/direct`

```json
// Request
{"content": "用户的研究内容..."}

// Response
{"roundtable_id": 123, "status": "active"}
```

处理流程：
1. 创建圆桌（`spark_id=NULL`），6 坐席全保留
2. `spark_content` = 用户内容
3. 自动调用 `start_round(question=content, mentioned=["gen"])`，gen 先回应
4. 返回 `roundtable_id`，前端打开圆桌 modal 进入正常轮询

**AgentTeamManager.create_team()** 新增参数：
- `spark_content_override: str | None = None` — 直接圆桌时传入用户内容

**v8 migration**：`ideator_roundtables.spark_id` 改为可空（去掉 NOT NULL），同时 `ideator_team_memory.spark_id` 也改为可空。

### 与火花圆桌对比

| | 火花圆桌 | 直接圆桌 |
|---|---|---|
| 入口 | 火花卡片 | 页面按钮 |
| 坐席 | 6 | 6（完全相同） |
| 内容来源 | spark | 用户输入 |
| spark_id | 真实 id | NULL |
| 首轮 | 用户手动提问 | 自动发内容 @gen |
| 后续交互 | 相同 | 相同 |

### 不做什么

- 不修改坐席配置
- 不修改 AgentTeam / agent identity prompt
- 不修改圆桌 modal 交互
- 不自动生成火花

## 修改清单

| 文件 | 操作 | 改动量 |
|------|------|--------|
| `modules/ideator/static/ideator.js` | 修改 | 新增按钮 + 输入 modal |
| `modules/ideator/routes.py` | 修改 | 新增 `POST /direct` 端点 |
| `modules/ideator/agent_team.py` | 修改 | `create_team()` 加 `spark_content_override` 参数 |
| `modules/ideator/schema.py` | 修改 | v8 migration：两个表 spark_id 可空 |
| `modules/ideator/tests/test_agent_team.py` | 修改 | 新增 direct 模式测试 |
| `modules/ideator/tests/test_schema.py` | 修改 | LATEST_VERSION=8 + v8 测试 |
