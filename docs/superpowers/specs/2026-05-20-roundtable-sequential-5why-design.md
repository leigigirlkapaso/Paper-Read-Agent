# 圆桌顺序回答 + 思维升级

**日期**: 2026-05-20
**状态**: 已批准

## 概述

圆桌讨论改为三段式串行回答，Gen 引入 5-Why 深度思考，Rev/Arb 加入建设性导向。

## 设计

### 1. 回答顺序

每轮分三段执行：

```
用户提问
  → ① gen 单独回答（全量历史 + 本轮提问）
  → ② rev1/rev2/rev3 并行回答（全量历史 + 本轮提问 + 本轮 gen 回答）
  → ③ arb1/arb2 并行回答（全量历史 + 本轮提问 + 本轮 gen 回答 + 本轮 rev 回答）
```

- 同一段内并行，段间串行
- gen 不可见本轮 rev/arb 回答
- rev 之间互不可见本轮对方回答
- arb 能看到本轮所有内容

### 2. Rev/Arb prompt

每个 rev/arb 的 agent identity prompt 末尾追加：

> 你的角色是帮助生成者改进这个研究idea，而非否定它。你可以质疑其可行性、指出逻辑漏洞或证据不足，但你的目标始终是推动这个idea变得更好，引导生成者深入思考，而不是劝他放弃。如果发现缺陷，请提供建设性的改进方向。

### 3. Gen 5-Why prompt

gen 的 agent identity prompt 末尾追加格式要求：

> 你的回答格式：
> 1. 先逐条列出本轮 rev 和 arb 向你提出的问题
> 2. 对每个问题，用 5-Why 方法深入回答：先给出直接回答，然后逐层追问自己 5 次"为什么"
> 格式示例：
> **问题1：xxx**
> - 直接回答：xxx
> - Why 1 → xxx
> - Why 2 → xxx
> - Why 3 → xxx
> - Why 4 → xxx
> - Why 5 → xxx（根因）

### 4. 历史去上限

`_format_recent_history` 中 `self.messages[-30:]` → 全量历史。

## 修改清单

| 文件 | 改动 |
|------|------|
| `agent_team.py` | `start_round` 三段式串行 + `_format_recent_history` 去上限 + 新增 `_get_round_context` 按角色提供上下文 |
| `prompts/agent_identity_gen.jinja2` | 追加 5-Why 回答格式 |
| `prompts/agent_identity_rev1.jinja2` | 追加建设性导向 |
| `prompts/agent_identity_rev2.jinja2` | 追加建设性导向 |
| `prompts/agent_identity_rev3.jinja2` | 追加建设性导向 |
| `prompts/agent_identity_arb1.jinja2` | 追加建设性导向 |
| `prompts/agent_identity_arb2.jinja2` | 追加建设性导向 |
| `tests/test_agent_team.py` | 更新 round 交互测试 |
