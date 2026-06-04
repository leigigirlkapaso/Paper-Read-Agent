# Thinker Module — 思考伙伴

独立页面（`/thinker/`），包含对话和演讲预演两大功能。与系统数据隔离，独立运作。

## 核心文件

| 文件 | 职责 |
|------|------|
| `chat.py` | ChatEngine：对话会话管理、流式响应、MemoryPipeline |
| `memory.py` | MemoryPipeline：5 路并行召回 → LLM 重排 → System Prompt 注入 |
| `profile.py` | ProfileManager：用户画像提取、指数滑动平均合并 |
| `resolutions.py` | ResolutionTracker：承诺追踪 (pending/in_progress/done/cancelled) |
| `knowledge_linker.py` | KnowledgeLinker：消息 embedding + 相关笔记检索 |
| `rehearsal.py` | RehearsalEngine：演讲预演全生命周期管理 |
| `voice.py` | VoiceEngine：STT/TTS 封装，WebM→WAV 转码 |
| `questions.py` | QuestionGenerator：主动提问生成 |
| `schema.py` | 数据库 DDL + MIGRATIONS (v3) |
| `routes.py` | API 路由：对话、排练、语音 |

## 数据库表 (v3)

- `thinker_conversations` — 对话会话 (chat/socratic/feynman/kpt/orid)
- `thinker_messages` — 对话消息（含 embedding）
- `thinker_resolutions` — 用户承诺追踪
- `thinker_pending_questions` — 主动提问队列
- `thinker_memory_index` — 记忆索引 (→ core_notes, importance + recall_count)
- `thinker_user_profile` — 用户画像 (单行 id=1)
- `thinker_rehearsals` — 演讲预演 (v3 新增)

## 记忆管道

```
用户消息 → MemoryPipeline.retrieve() — 5 路并行
  ├── 语义相似 (KnowledgeLayer.search_by_embedding)
  ├── 未完成承诺 (thinker_resolutions)
  ├── 最近摘要 (thinker_memory_index + core_notes)
  ├── 用户画像 (thinker_user_profile)
  └── 闲置念头 (thinker_memory_index spark 类型)
      ↓
  MemoryPipeline.rerank() → LLM 重排 (≤6 跳过, >6 Haiku)
      ↓
  MemoryPipeline.inject() → System Prompt
      ↓
  LLM 回复 → fire-and-forget 编码写回
```

## 演讲预演状态机

```
preparing → presenting → qa → summarizing → completed
               ↑           ↑        ↑
               └─── preparing ───────┘  (允许回退)
```

- STT: Chrome WebM → `_normalize_format()` → WAV → pyav 转码 → api.gpt.ge
- TTS: 独立于 STT，硬编码 mp3 输出
- Q&A 最少 3 轮；总结 6 维度 (核心发现/内容评估/Q&A表现/改进清单/语法纠正/建议)

## 事件

## 事件

**发出：** `thinker:message:sent`, `thinker:summary:generated`, `thinker:resolution:extracted`
**订阅：** `core:note:created`（记录事件，未来用于知识关联）, `thinker:summary:generated`（→ memory_index）

## 跨模块接口

- **被 Ideator 依赖：** `thinker:summary:generated` → Ideator 增量挖掘
- **依赖 Core：** `core.llm`, `core.voice`, `core.knowledge`, `core.scheduler`, `core.event_bus`
- **不依赖 Ideator**（隔离设计）
- `VoiceEngine` 非公开 API，仅供本模块 routes.py 使用

## 调度

`inactivity_check` (interval), `resolution_check` (cron 每日 9 点)
