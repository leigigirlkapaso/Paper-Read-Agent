# Ideator 管道 Token 优化 + S2 工具增强 — 设计文档

## 目标

在不降低火花质量的前提下，大幅节省 ideator 管道的 LLM token 消耗。同时将 S2 火花生成从"纯 prompt 批量产出"升级为"工具增强的单火花深度打磨"。

## 当前问题

1. S2 每组产出 0-4 个火花，总数不可控
2. S2 火花生成无工具调用，LLM 只看标题+摘要，看不到全文
3. S2.25 通过阈值 0.4 的火花全部进入 S3 辩论，量大时成本失控
4. `DEBATE_SEATS` 声明了 flash 模型但实际未使用
5. 每火花至少 15 次 pro LLM 调用，最多 38 次

## 设计决策

| 决策 | 结论 |
|------|------|
| 筛选策略 | 排名制 top 10，不设阈值 |
| 筛选模型 | 全 pro（质量优先） |
| 辩论模型 | 全 pro（质量优先） |
| 辩论轮数 | 保持 5 轮上限 |
| S2 工具调用 | 全 14 个工具可用 |
| S2 每组火花数 | 严格 1 个 |
| 落选火花 | 丢弃（不保存） |
| S2 产出上限 | 靠 spark_pair_limit 间接控制，不额外设硬上限 |

---

## 架构改动

### 三层升级

```
Layer 1: CoreLLM 新增工具调用 API
  core/llm.py — chat_with_tools() / achat_with_tools()
  ↓
Layer 2: ToolExecutor — 工具定义→实际执行
  modules/ideator/tool_executor.py (新建)
  ↓
Layer 3: S2 工具调用循环
  pipeline.py — _generate_sparks() 重写
```

---

## Layer 1: CoreLLM 工具调用 API

### 接口

```python
@stable
def chat_with_tools(
    self,
    messages: list[dict],
    tools: list[dict],
    *,
    tool_choice: str = "auto",
    module: str = "core",
    purpose: str = "chat_with_tools",
    max_tokens: int = 16384,
    temperature: float | None = None,
) -> dict:
    """同步工具调用对话。

    参数:
        messages: OpenAI 格式消息列表
        tools: OpenAI 格式工具定义列表
        tool_choice: "auto" | "none" | "required" | {"type": "function", "function": {"name": "x"}}
        module, purpose: 用量追踪标签
        max_tokens: 最大输出 token
        temperature: 覆盖默认温度

    返回:
        {
            "content": str | None,        # 最终文本回复（无工具调用时）
            "tool_calls": [
                {
                    "id": str,
                    "name": str,
                    "arguments": dict,     # 已解析的 JSON
                }
            ] | None,                      # 工具调用请求（无文本时）
            "usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int},
            "finish_reason": str,
        }
    """
```

```python
@evolving
async def achat_with_tools(
    self,
    messages: list[dict],
    tools: list[dict],
    *,
    tool_choice: str = "auto",
    module: str = "core",
    purpose: str = "chat_with_tools",
    max_tokens: int = 16384,
    temperature: float | None = None,
) -> dict:
    """异步工具调用对话。返回格式同 chat_with_tools。"""
```

### 实现要点

- 向 `chat.completions.create()` 传入 `tools` 和 `tool_choice` 参数
- 解析 `resp.choices[0].message.tool_calls`，将 arguments JSON 字符串解析为 dict
- 自动记录用量到 `core_llm_usage`
- `finish_reason == "tool_calls"` → 返回 tool_calls 列表
- `finish_reason == "stop"` → 返回 content 文本

### 与现有 API 的关系

- `chat()` / `achat()` **不改动**，保持向后兼容
- 新 API 是增量扩展，不走 deprecation 路径
- 两者共享 `_sync_client`、`_track_usage`、API key/base_url 等内部状态

---

## Layer 2: ToolExecutor

### 文件

`paperreadagent/modules/ideator/tool_executor.py`（新建）

### 接口

```python
class ToolExecutor:
    """将 ToolRegistry 的工具定义映射到可执行函数。

    依赖:
        - data_access (DataAccess) — 访问论文、笔记、火花、roundtable
        - core_llm (CoreLLM) — audit_claim 需要 LLM 验证
        - tool_registry (ToolRegistry) — 工具定义
    """

    def __init__(self, *, data_access, core_llm, tool_registry):
        ...

    async def execute(self, tool_name: str, arguments: dict) -> str:
        """执行单个工具调用，返回结果字符串。

        不抛异常：执行失败时返回错误描述字符串，让 LLM 自行处理。
        """

    def to_openai_tools(self) -> list[dict]:
        """将 ToolRegistry 中注册的工具转换为 OpenAI tools 参数格式。
        所有工具都可用（S2 生成阶段无 RBAC 限制）。
        """
```

### 14 个工具的执行逻辑

| 工具 | 执行逻辑 | 返回内容 |
|------|---------|---------|
| `search_papers` | `core.knowledge.search_by_embedding(query, top_k)` | 匹配论文列表（标题+摘要+ID） |
| `read_paper` | `data.get_paper(arxiv_id)` → 全文 Markdown | 论文全文（截断到 8000 字） |
| `read_note` | `data.get_user_note(paper_id)` | 用户笔记内容 |
| `create_spark` | S2 阶段不可用 | 返回 "此工具仅在圆桌讨论中可用" |
| `update_spark` | S2 阶段不可用 | 返回 "此工具仅在圆桌讨论中可用" |
| `check_duplicate` | `data.find_similar_sparks(content)` — embedding 余弦搜索 | 相似火花列表或无 |
| `trigger_recall` | S2 阶段不可用 | 返回 "此工具仅在圆桌讨论中可用" |
| `audit_claim` | `core_llm.chat()` 验证声明 | 验证结果（支持/不支持/不确定） |
| `fetch_snapshot` | S2 阶段不可用 | 返回 "此工具仅在圆桌讨论中可用" |
| `write_memory` | S2 阶段不可用 | 返回 "此工具仅在圆桌讨论中可用" |
| `read_memory` | S2 阶段不可用 | 返回 "此工具仅在圆桌讨论中可用" |
| `report_watermark` | S2 阶段不可用 | 返回 "此工具仅在圆桌讨论中可用" |
| `adjust_quota` | S2 阶段不可用 | 返回 "此工具仅在圆桌讨论中可用" |
| `grant_tool` | S2 阶段不可用 | 返回 "此工具仅在圆桌讨论中可用" |

S2 阶段实际可用：`search_papers`、`read_paper`、`read_note`、`check_duplicate`、`audit_claim`（5 个）。其余 9 个是圆桌讨论专属，调用时返回不可用提示。

---

## Layer 3: S2 工具调用循环

### 新流程

```
_generate_sparks(scored_links, params):
    top_links = sorted by relevance_score[:spark_pair_limit]
    groups = _group_by_shared_source(top_links)

    for each group:
        messages = [
            {"role": "system", "content": SPARK_GEN_SYSTEM_PROMPT},
            {"role": "user", "content": format_group_context(group)},
        ]

        for round in 1..5:
            resp = await core.llm.achat_with_tools(
                messages=messages,
                tools=tool_executor.to_openai_tools(),
                tool_choice="auto",
            )

            if resp["tool_calls"]:
                for tc in resp["tool_calls"]:
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [tc],
                    })
                    result = await tool_executor.execute(tc["name"], tc["arguments"])
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })
                continue

            if resp["content"]:
                spark = parse_spark_from_content(resp["content"])
                if spark:
                    spark["source_refs"] = group_refs
                    spark["source_type"] = group[0]["recall_path"]
                    yield spark
                break

        else:
            # 5 轮后仍未产出 → 降级为纯 prompt 调用
            spark = await fallback_generate(group)
            if spark:
                yield spark
```

### System Prompt 核心要求

```
你是研究火花生成器，拥有论文查阅和文献搜索能力。

对给定的关联对：
1. 使用 read_paper 查阅相关论文全文，获取比摘要更深入的信息
2. 使用 search_papers 搜索已有文献，验证你的研究方向
3. 使用 check_duplicate 检查产生的火花是否与已有火花重复
4. 最终生成 1 个高质量研究火花（一句话假说/研究方向）

要求：
- 只生成 1 个火花
- 火花必须具体、可研究
- 如果关联太弱或信息不足以支撑火花，返回 null
- 返回纯 JSON（不要 markdown 代码块）：
  {"content": "火花正文", "quality_score": 0.0-1.0}
```

### 错误处理

| 场景 | 策略 |
|------|------|
| 工具执行异常 | 返回 `"工具 '{name}' 执行失败: {error}"` 注入 messages，继续循环 |
| LLM API 异常 | 该组丢弃，logger.warning |
| 5 轮后无火花 | 降级为纯 prompt 调用（不传 tools），保持当前行为 |
| content JSON 解析失败 | 重试 1 次（feedback 注入），再失败丢弃 |
| 工具调用超时 | 30s asyncio.wait_for 超时，返回超时提示 |

### Semaphore

保持 `Semaphore(5)` 并发限制（与当前一致）。

---

## S2.25 筛选逻辑改动

### 新逻辑

```
if len(sparks) <= 10:
    top_sparks = sparks      # 全部通过，0 次 LLM 调用
    seed_sparks = []
else:
    scored = await debate_engine.score_sparks(sparks)
    scored.sort(key=lambda s: s.get("_filter_score", 0), reverse=True)
    top_sparks = scored[:10]
    # 剩余直接丢弃
```

### 与旧逻辑对比

| | 旧 | 新 |
|---|---|---|
| 筛选标准 | `_filter_score ≥ 0.4` | 排名 top 10 |
| 最少保留 | `max(通过数, 10)` | 无（≤10 全保留） |
| 落选火花 | `seed_sparks` 保存 | 丢弃 |
| 评分触发 | N > 10 | 同 |
| Reviewer | 3 个 pro | 同（不变） |

### 移除 seed_sparks

- `pipeline.py` 中 `seed_sparks` 变量和相关保存逻辑删除
- 不再有火花标注为 `seed` 类型跳过深化

---

## Token 节省估算

假设 S2 产出 20 火花，辩论平均 2 轮（~20 次调用/火花）：

| 阶段 | 优化前 | 优化后 |
|------|--------|--------|
| S2 生成 | 15 次 pro | 45 次 pro（含工具） |
| S2.25 筛选 | 0（≤10 阈值不触发） | 60 次 pro（20×3） |
| S3 辩论 | 400 次 pro（20×20） | 200 次 pro（10×20） |
| **总计** | **415 次** | **305 次** |
| **节省** | — | **~26%** |

假设 S2 产出 30 火花：

| 阶段 | 优化前 | 优化后 |
|------|--------|--------|
| S2 生成 | 15 次 pro | 45 次 pro |
| S2.25 筛选 | 0 | 90 次 pro（30×3） |
| S3 辩论 | 600 次 pro（30×20） | 200 次 pro（10×20） |
| **总计** | **615 次** | **335 次** |
| **节省** | — | **~46%** |

火花越多，节省比例越大。

---

## 附带修复

`pipeline.py` 第 614 行 `_clean` NameError：在 `_generate_group` 内部函数顶部添加局部 import：

```python
from paperreadagent.utils.json_utils import clean_json as _clean
```

---

## 涉及文件清单

| 文件 | 改动 | 复杂度 |
|------|------|--------|
| `core/llm.py` | 新增 `chat_with_tools()` / `achat_with_tools()` | 中 |
| `modules/ideator/tool_executor.py` | **新建** — 工具执行层 | 高 |
| `modules/ideator/pipeline.py` | `_generate_sparks()` 重写 + S2.25 排名制 + seed_sparks 移除 + _clean 修复 | 高 |
| `modules/ideator/prompts/spark_generate.jinja2` | 重写为工具调用 system prompt | 低 |
| `modules/ideator/tests/test_tool_executor.py` | **新建** | 中 |
| `modules/ideator/tests/test_s2_tool_loop.py` | **新建** | 中 |
| `tests/test_core_llm_tools.py` | **新建** — CoreLLM 工具调用 API 测试 | 低 |

---

## 不做的

- DEBATE_SEATS 模型分级（全 pro，质量优先）
- 辩论轮数缩减（保持 5 轮）
- S2 硬产出上限（spark_pair_limit 已间接控制）
- CoreLLM.chat() / achat() 签名修改（新增独立 API）
- AgentTeam 圆桌讨论的工具调用（保持现有 prompt 注入方式，等本次改完后再评估）
