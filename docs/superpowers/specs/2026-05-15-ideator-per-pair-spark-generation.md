# S2 逐组火花生成设计

**日期：** 2026-05-15
**范围：** `pipeline.py` S2 火花生成阶段 + `prompts/spark_generate.jinja2`

---

## 问题

当前 S2 `_generate_sparks` 将所有高分关联对（≤15 对）一次性塞给 LLM，LLM 在全局视野下跨对合成火花。结果是：

- 火花溯源模糊——一个火花可能引用 4-6 个来源，圆桌讨论时坐席看到的信息过散
- 弱对被淹没——非显而易见的洞察在批量中得不到 LLM 的专属注意力
- 圆桌讨论虽然为单火花设计，但拿到的火花本身就是"综述型"的

## 目标

- 每个火花绑定到精确的来源集合（最多一个组内的所有来源）
- 同一中心源的关联对聚在一起让 LLM 综合思考，孤对独立深挖
- 弱对自然过滤（LLM 对无价值关联返回空数组）
- 圆桌讨论代码零改动

---

## 架构

### 分组策略：C1 贪心共享源分组

```
输入关联对 → 统计每个 source 出现次数 → 按出现次数降序，贪心建组
```

规则：
1. 统计 `{source_id: 出现次数}`
2. 按出现次数降序遍历 source
3. 对于每个 source，收集所有**未被之前组分配**的含该 source 的对，建为一个组
4. 一轮遍历后将剩余未分配的对组合到出现最多的 source 的组中
5. 每组 ≤5 对（硬上限，超出则按 quality_score 截断）

### 生成流程

```
scored_links (top N by quality_score)
    │
    ├── _group_by_shared_source()
    │     组1: [A×B, A×C, A×D]     (3对，围绕 A 发散)
    │     组2: [B×E]               (1对，孤对深挖)
    │     组3: [F×G]               (1对，孤对深挖)
    │
    ├── asyncio.gather(Semaphore(5))
    │     _generate_group(组1) → [火花1, 火花2, 火花3]
    │     _generate_group(组2) → [火花4]
    │     _generate_group(组3) → []          ← 弱对自然过滤
    │
    └── 展平 + 注入精确 source_refs → 进入 S3
```

### Prompt 设计

单个模板 `spark_generate.jinja2`，根据组内关联对数量（1 对 vs ≥2 对）自动适配指导语：

- **单对组**：要求深度审视这一对关系，产出 0-2 个聚焦火花
- **多对组**：要求在组内关联中发现模式，产出 1-4 个火花，火花可跨对合成但来源限定在组内

两组共用相同的输出格式。

---

## 数据模型

### 输入：关联对（CrossRecall 产出，不变）

```python
{
    "source_a": {"type": "paper|core_note", "id": int, "content": str},
    "source_b": {"type": "paper|core_note", "id": int, "content": str},
    "recall_path": "similarity|contradiction|...",
    "relevance_score": 0.0-1.0,
    "reasoning": str,
}
```

### 输出：火花（格式不变，source_refs 精度提升）

```python
{
    "content": str,
    "source_type": str,       # recall_path
    "quality_score": 0.0-1.0,
    "source_refs": [          # 精确：仅含本组涉及的来源（最大 ≤5 对×2=10 个来源）
        {"type": "paper|core_note", "id": int},
        ...
    ],
}
```

### 分组结果

```python
list[list[dict]]  # 每组是关联对列表
```

---

## 实现细节

### `_group_by_shared_source(links, max_per_group=5)`

```python
def _group_by_shared_source(self, links, max_per_group=5):
    # 1. 统计每个 source_id 的出现次数
    source_count = {}
    for l in links:
        for src in (l["source_a"], l["source_b"]):
            key = f"{src['type']}:{src['id']}"
            source_count[key] = source_count.get(key, 0) + 1

    # 2. 按出现次数降序排列 source
    sorted_sources = sorted(source_count.keys(), key=lambda k: source_count[k], reverse=True)

    # 3. 贪心建组
    assigned = set()  # link indices
    groups = []
    for src_key in sorted_sources:
        group = []
        for i, l in enumerate(links):
            if i in assigned:
                continue
            if len(group) >= max_per_group:
                break
            a_key = f"{l['source_a']['type']}:{l['source_a']['id']}"
            b_key = f"{l['source_b']['type']}:{l['source_b']['id']}"
            if a_key == src_key or b_key == src_key:
                group.append(l)
                assigned.add(i)
        if group:
            groups.append(group)

    # 4. 未分配的对，各成一组
    for i, l in enumerate(links):
        if i not in assigned:
            groups.append([l])

    return groups
```

### `_generate_sparks` 核心

```python
async def _generate_sparks(self, scored_links, params=None):
    max_pairs = params.get("spark_pair_limit", 10) if params else 10
    top_links = sorted(scored_links, key=lambda x: x.get("relevance_score", 0), reverse=True)[:max_pairs]

    groups = self._group_by_shared_source(top_links)

    sem = asyncio.Semaphore(5)

    async def _generate_group(group):
        async with sem:
            links_data = [
                {"a": l["source_a"]["content"][:200],
                 "b": l["source_b"]["content"][:200],
                 "reason": l.get("reasoning", ""),
                 "link_type": l.get("recall_path", ""),
                 "quality_score": l.get("relevance_score", 0)}
                for l in group
            ]
            prompt = self.core.llm.load_prompt(
                "ideator", "spark_generate", links=links_data,
            )
            try:
                raw, _ = await self.core.llm.achat(
                    user_prompt=prompt, module="ideator", purpose="spark_generate",
                )
                sparks = json.loads(raw)
                if not isinstance(sparks, list):
                    return []
                # 注入精确 source_refs
                refs = []
                seen = set()
                for l in group:
                    for src in (l["source_a"], l["source_b"]):
                        key = f"{src['type']}:{src['id']}"
                        if key not in seen:
                            seen.add(key)
                            refs.append({"type": src["type"], "id": src["id"]})
                for s in sparks:
                    s["source_refs"] = refs
                    s["source_type"] = group[0].get("recall_path", "cross_layer")
                return sparks
            except Exception:
                return []

    results = await asyncio.gather(*[_generate_group(g) for g in groups])
    all_sparks = []
    for r in results:
        all_sparks.extend(r)
    return all_sparks
```

### Effort 参数扩展

`EFFORT_PARAMS` 中将 `spark_count`（元组 min/max）替换为 `spark_pair_limit`：

```python
# 之前（批量模式）
"spark_count": (3, 5),   # → batch_size = max(spark_max * 3, 8) = 15

# 之后（逐组模式）
"spark_pair_limit": 10,  # 直接控制取多少对高分关联进入分组生成
```

| effort | spark_pair_limit | 说明 |
|--------|-----------------|------|
| lite | 5 | 最小计算 |
| balanced | 10 | 默认 |
| max | 15 | 全面 |
| beast | 20 | 极端 |

---

## 错误处理

- **LLM 调用失败**：该组返回 `[]`，不影响其他组
- **JSON 解析失败**：返回 `[]`（安全降级）
- **全组无产出**：正常情况，S3 收到空列表 → S4 无入库 → pipeline 正常结束
- **Semaphore 排队**：5 并发上限，超出排队等待（不影响正确性）

---

## 测试

### 单元测试

| 测试 | 覆盖 |
|------|------|
| `test_group_by_shared_source_happy_path` | A×B, A×C, B×E → 2 组 |
| `test_group_by_shared_source_single_pair` | A×B → 1 组 |
| `test_group_by_shared_source_empty` | [] → [] |
| `test_group_by_shared_source_orphan` | A×B, C×D (无共享源) → 2 组 |
| `test_group_max_5_limit` | 6 对共享源 → 截断为 5 |
| `test_generate_sparks_weak_pair_returns_empty` | 弱对 → LLM 返回 [] |
| `test_generate_sparks_source_refs_injected` | 验证火花 source_refs 精确 |
| `test_generate_sparks_parallel_failure_isolated` | 一组失败，其他组正常产出 |

### 集成测试

| 测试 | 覆盖 |
|------|------|
| `test_pipeline_s2_to_s3_flow` | S2 产出 → S3 逐个审查 |
| `test_roundtable_unchanged_with_new_sparks` | 新火花 → 圆桌讨论正常工作 |

---

## 不改动的文件

- `cross_recall.py` — 召回不变
- `reviewer.py` — 审查不变
- `auditor.py` — 审计不变
- `spark_store.py` — 入库不变
- `roundtable.py` — 圆桌不变
- `agent_team.py` — Agent Team 不变
- `routes.py` — API 不变
- `schema.py` — 表结构不变
