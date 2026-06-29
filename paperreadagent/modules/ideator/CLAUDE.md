# Ideator Module — 知识挖掘引擎

跨论文、跨笔记、跨项目发现隐藏关联，产生研究火花。**深度绑定系统数据。**

## 管道流程 (V1.0)

```
S0 CrossRecall (6路) → S1 Per-Pair Score (逐对 LLM)
→ S2 Per-Group Spark Gen (C1 共享源分组)
→ S2.25 Lightning Filter (3 flash Rev, threshold≥0.4)
→ S2.5 Deepen (完整研究草稿)
→ S3 Multi-Round Debate (6 坐席: 初评→辩论→重评→终裁→简报)
→ S4 Dedup Save (LanceDB ANN 去重) → S6 Audit
→ [用户发起圆桌: AgentTeam 3 阶段 gen→rev并行→arb并行]
```

## Idea 级 Embedding 召回 (v2: 全量覆盖)

```
笔记写入 → IdeaExtractor.get_or_extract_ideas()
  ├── 查 ideator_note_ideas 缓存
  ├── 短笔记（≤3000字）：flash LLM 单次提取
  ├── 长笔记：semantic_chunk(3000/500) → 逐块 flash LLM 提取
  ├── 跨 chunk 去重（embedding 余弦 ≥0.90 合并）
  ├── 每个 idea 独立 bge-m3 embedding (1024维)
  └── 写入缓存

CrossRecall S0:
  ├── 每条笔记提取 N 个 idea
  ├── 每个 idea 独立搜索 (LanceDB ANN)
  ├── MaxSim 聚合: pair_score = max(cos(ai, bj))
  └── 按 (max_similarity, idea_match_count) 排序
```

## 核心组件

| 文件 | 职责 |
|------|------|
| `idea_extractor.py` | flash LLM 提取独立 idea + embedding + 缓存 |
| `cross_recall.py` | 6 路召回，idea 级 + note 级搜索 + MaxSim |
| `debate_engine.py` | 6 坐席多轮辩论（初评→辩论→重评→终裁→简报） |
| `agent_team.py` | 3 阶段圆桌：gen→rev 并行→arb 并行，含 5-Why |
| `arbiter.py` | 毕业决策 + 配额 + 工具授权 |
| `pipeline.py` | S0→S1→S2→S2.25→S2.5→S3→S4→S6 全管道编排 |
| `spark_store.py` | LanceDB ANN 去重（0.85 merge / 0.60 flag） |
| `graduation.py` | Hot/Warm/Cold 三层上下文生命周期 |
| `data_access.py` | 统一数据适配层（legacy + core DB 桥接 + LanceDB spark 索引） |

## 数据库表 (v10)

- `ideator_sparks` — 火花（LanceDB ANN 去重）
- `ideator_note_ideas` — idea 级 embedding 缓存，UNIQUE(note_source, note_id, idea_index)
- `ideator_cross_links` / `ideator_review_records` / `ideator_pipeline_runs` / `ideator_recall_weights`
- `ideator_roundtables` + `ideator_roundtable_messages` + `ideator_roundtable_snapshots` + `ideator_team_memory`

## 关键设计决策

- `IdeatorLLM`：适配 CoreLLM 到 `chat(model_role, messages, ...)` 接口。CoreLLM 不支持 model 覆盖，所有坐席统一使用 deepseek-v4-pro。
- `_chat_with_retry`：3 次重试（含 JSON 解析失败），错误反馈注入对话历史
- `_clean_json`：去 markdown 代码块再解析；`response_format={"type":"json_object"}` 已移除
- Semaphore(2) debating, Semaphore(5) scoring/spark_gen
- `reasoning_content` 耗尽检测与告警
- arb_control 失败时 fail-open (CONTINUE)
- 每条笔记录级 GraduationManager 消除跨圆桌状态污染
- core_notes 召回过滤 `source_module='literature'` 隔离 thinker 闲聊
- 搜索全部走 `KnowledgeLayer` → LanceDB ANN（core_notes + sparks 双表）
- Embedding: `core.llm.embed()` → bge-m3 1024 维

## 事件

**订阅：** `core:note:created`（→ 增量挖掘）, `thinker:summary:generated`（→ 增量挖掘）
**发出：** 无（仅通过 SparkStore 写入 DB，不主动推送事件）

## 跨模块接口

- **依赖 Thinker：** `thinker:summary:generated` 事件（无直接 import）
- **依赖 Core：** `core.llm`, `core.knowledge`, `core.scheduler`, `core.event_bus`, `core.legacy_db`
- **公开 API：** `get_roundtable_manager()`（模块级单例，供 routes.py 使用）
- `DataAccess` 通过 `Core.legacy_db` 桥接 legacy 论文数据
