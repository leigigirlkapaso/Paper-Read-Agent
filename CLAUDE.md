# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

自动化文献调研多智能体系统：**输入研究构想 → 关键词提取 → 多平台检索 → LLM 相关性筛选 → 多源 PDF 下载 → 并发精读 → 综述报告**。

## 运行命令

```bash
uv sync                          # 安装依赖
uv run python main.py            # CLI 交互式运行
uv run uvicorn paperreadagent.web.app:app --reload --port 8000  # Web GUI
uv run python -m pytest paperreadagent/tests/   # 运行全部测试
uv run python -m pytest paperreadagent/tests/test_pdf_parser.py -v  # 单文件测试
uv run python -m pytest paperreadagent/modules/thinker/tests/ -v   # thinker 模块测试
uv run python -m pytest paperreadagent/modules/ideator/tests/ -v   # ideator 模块测试
uv run python -m pytest paperreadagent/modules/thinker/tests/test_voice.py -v  # 单测试文件
```

## 目录结构

```
PaperReadAgent/
├── config.yaml              # 所有参数（LLM、研究构想、检索、下载、prompt）
├── main.py                  # 根入口 → 委托 paperreadagent/main.py
├── pyproject.toml           # uv 依赖 + pytest pythonpath 配置
├── paperreadagent/          # 全部项目代码
│   ├── main.py              # CLI 主流程（项目选择、模式分发、pipeline 编排）
│   ├── agent1/              # AGENT1: 检索 + 筛选
│   │   ├── keyword_extractor.py   # A: LLM 三层策略提取关键词/query
│   │   ├── arxiv_searcher.py      # arxiv API 检索 + PaperMeta dataclass 定义
│   │   ├── semantic_scholar_searcher.py
│   │   ├── pwc_searcher.py
│   │   ├── openalex_searcher.py
│   │   └── paper_filter.py        # B: LLM 批量打分筛选 + 边界重试 + 质量加权
│   ├── agent2/              # AGENT2: 精读
│   │   ├── paper_reader.py        # 单篇 LLM 精读（prompt-aware 缓存）
│   │   └── parallel_runner.py     # asyncio.Semaphore 并发调度
│   ├── db/                  # SQLite 持久化（WAL 模式，版本化迁移）
│   │   ├── schema.py        # DDL + MIGRATIONS 字典 (v1→v4)
│   │   └── database.py      # Database 类，全部 CRUD + FTS5
│   ├── utils/
│   │   ├── llm_client.py          # OpenAI 兼容 LLM 客户端（同步+异步，usage 追踪）
│   │   ├── arxiv_downloader.py    # arXiv + 直接 URL 下载
│   │   ├── multi_downloader.py    # 多源级联下载（arXiv → URL → Unpaywall → S2 → Sci-Hub）
│   │   ├── pdf_parser.py          # pymupdf4llm PDF→Markdown，章节感知截断
│   │   └── local_scanner.py       # 本地 PDF 扫描（DB 感知，复用历史评分）
│   ├── web/                 # FastAPI Web GUI
│   │   ├── app.py                # 应用工厂，静态文件，DB 生命周期
│   │   ├── template_config.py    # Jinja2 模板引擎配置
│   │   ├── routes/
│   │   │   ├── projects.py       # Dashboard、项目 CRUD
│   │   │   ├── sessions.py       # 会话创建、pipeline 启动、SSE 进度、导出
│   │   │   └── papers.py         # 论文详情、PDF 服务、上传、搜索、多源重试
│   │   ├── templates/            # Jinja2 模板（base, session_detail, paper_detail 等）
│   │   └── static/css/app.css    # 分栏布局、PDF 容器样式
│   ├── core/                 # 平台核心层（Core、KnowledgeLayer、EventBus、CoreScheduler、CoreFrontend、CoreLLM）
│   │   ├── __init__.py        # Core 类（模块注册、路由挂载、配置管理）
│   │   ├── llm.py             # CoreLLM — 统一 LLM 入口（chat/achat/chat_stream/embed/load_prompt）
│   │   ├── voice.py           # CoreVoice — 统一语音入口（STT/TTS，OpenAI 兼容 Audio API）
│   │   ├── knowledge.py       # KnowledgeLayer — core_notes CRUD + embedding 搜索 + 矛盾检测
│   │   ├── embedding.py       # 余弦相似度 + 向量打包/解包
│   │   ├── event_bus.py       # EventBus — 模块间事件通信
│   │   ├── scheduler.py       # CoreScheduler — APScheduler 后台任务
│   │   ├── frontend.py        # CoreFrontend — 全局组件注册 + JS/CSS 注入
│   │   ├── decorators.py      # @stable / @evolving / @internal 稳定性标注
│   │   ├── config.py          # 配置合并工具
│   │   ├── database.py        # CoreDatabase — 核心层 DB + 模块迁移
│   │   └── schema.py          # core_notes + core_llm_usage DDL
│   ├── modules/               # 平台化模块
├── projects/                # 项目数据（projects/<name>/sessions/<id>_<ts>/）
├── outputs/                 # 旧版兼容目录
└── paperreadagent.db        # SQLite 数据库
```

## 核心架构

### PaperMeta 数据类（`agent1/arxiv_searcher.py`）

贯穿全系统的数据载体：
- 标识：`arxiv_id`（主去重键）、`doi`（多源下载键）
- 元数据：`title`, `authors`, `published`, `abstract`, `venue`, `citation_count`
- 流程状态：`relevance_score`（AGENT1-B 填充）、`source_platform`、`pdf_url`、`code_url`

### 数据库（`db/` + `core/` + 各模块 `schema.py`）

三层 Schema 体系：
- **Legacy DB** (`db/schema.py`): v4 — projects/sessions/papers/summaries/notes + FTS5
- **Core DB** (`core/schema.py`): core_notes + core_llm_usage + core_scheduled_jobs
- **模块 Schema**: thinker v2, ideator v9 — 各自维护 MIGRATIONS 字典

全部 SQLite WAL 模式，`check_same_thread=False`。
- `papers_fts` — FTS5 全文索引（title + abstract）
- DB 作为 FastAPI app.state 单例，通过 lifespan 管理生命周期

### PDF 下载策略

1. **arXiv** — 有 arxiv_id 时优先（免费稳定）
2. **直接 URL** — 搜索结果中的开放获取链接
3. **Unpaywall** — 通过 DOI 查找 OA 版本（需 `downloader.unpaywall_email`）
4. **Semantic Scholar OA** — Graph API 通过 DOI 查找 `openAccessPdf`
5. **Sci-Hub** — 最后兜底（默认关闭，需 `enable_scihub: true`）

所有下载经 `%PDF` 魔术字节校验。旧下载器 `arxiv_downloader.py` 仍保留同步接口。

### LLM 调用模式

- `LLMClient` 封装 OpenAI 兼容 API，返回 `(text, LLMUsage)` 元组
- AGENT1-A 关键词提取：单次调用，三层策略 prompt，JSON 容错解析
- AGENT1-B 文献筛选：批量（batch_size 篇/次），边界论文（阈值 -0.1 内）最多重试 2 次
- AGENT2 精读：每篇独立调用（核心设计约束），asyncio.Semaphore(100) 并发，最多重试 2 次（指数退避）

### 缓存与可复现性

- AGENT2 文件缓存路径含 prompt hash：`{arxiv_id}_{prompt_short}.md`
- 三层读取：文件缓存 → DB summaries 表 → LLM 调用
- Session 启动时保存 `config_snapshot.yaml`，计算 `config_hash` 存入 DB

### Web GUI 技术栈

FastAPI + Jinja2 + HTMX + Alpine.js + Tailwind CSS (CDN)。关键页面：
- Session 详情：SSE 实时进度条 + 论文卡片网格
- 论文详情：左 55% PDF.js 渲染 + 右 45% 服务端 Markdown 渲染
- 配置编辑：Alpine.js 动态表单，预填用户默认值
- Pipeline 通过 `asyncio.to_thread` 在后台执行，进度通过共享字典 + SSE 推送

## config.yaml 配置块

```yaml
llm:            # API 地址/Key/模型/温度
research:       # 研究构想、检索参数、筛选阈值、排序、年份
concurrency:    # max_concurrent (AGENT2 并发)
sources:        # 四平台开关（arxiv/semantic_scholar/papers_with_code/openalex）
downloader:     # unpaywall_email、enable_scihub、scihub_mirrors
pdf:            # max_chars（论文文本截断上限）
summary_prompt: # AGENT2 精读提示词模板
core:           # 核心层配置
  voice:        # 语音 API（api_key、api_base_url、stt_model、tts_model）
```

## 关键开发约束

- `uv run` 管理 Python 环境，不依赖系统 Python
- AGENT2 每篇独立 LLM 调用——不要将多篇论文合并到一次调用
- 相关性筛选阈值用 `>=`（含等于），LLM 打分后**全部候选论文**的分值写入 DB（不仅是筛选通过的）
- PDF 解析用 `pymupdf4llm` 输出 Markdown（含 LaTeX），正则匹配需兼容 `## **1 Introduction**` 格式
- `check_same_thread=False` 是必须的（DB 在主线程和后台线程间共享）
- 修改 schema 时增量添加 `MIGRATIONS[vN]`，不修改已有迁移
- `BASE_DIR` 指向项目根（`paperreadagent/` 包的上一层），所有文件路径相对于项目根
- 搜索器和下载器使用 `aiohttp` + `asyncio.Semaphore` 异步并发
- Web pipeline 在 `asyncio.to_thread` 中运行，内部 `asyncio.run()` 创建独立事件循环（合法模式）
- **Ideator LLM 适配器**：`IdeatorLLM` 封装 `core.llm` 的 API key/base_url/model，提供 `chat(model_role, messages, ...)` 异步接口。所有 ideator 内部代码调用 `self._llm.chat()` 而非直接调 `core.llm`。这是设计决定，不走 core.llm 统一入口是因为 ideator 需要 `response_format`、`max_tokens` 等高级参数，而 core.llm 的 `chat()` 是简化同步接口。

## 模块化开发规范（平台化核心契约）

项目正从单体演进为模块化知识工作台。以下规则是所有模块开发的强制契约，**每一条都必须遵守**。

### 一、模块目录契约

每个模块必须在 `paperreadagent/modules/<name>/` 下保持标准结构：

```
modules/<name>/
├── __init__.py          # 唯一对外接口：def register(core: Core) -> ModuleInfo
├── routes.py            # FastAPI APIRouter（纯后台模块可无）
├── schema.py            # 本模块独立 DDL + MIGRATIONS 字典
├── models.py            # Pydantic / dataclass（可选）
├── templates/           # Jinja2 模板（可选）
├── static/              # 静态文件（可选）
└── tests/               # 模块测试（必须，最少 3 个）
```

**强制规则：**
- `__init__.py` 必须暴露 `def register(core)` 作为唯一入口，禁止模块间直接 import
- `register()` 必须返回 `ModuleInfo(name, version, schema_version, routes)` namedtuple
- `schema_version` 必须填写，哪怕模块没有自己的表（填 0）
- 模块表删除 = `DROP TABLE IF EXISTS {module}_*` + `DELETE FROM core_notes WHERE source_module = '{module}'`
- 测试必须放在模块自己的 `tests/` 目录下

### 二、Core API 稳定性等级

核心层（`paperreadagent/core/`）暴露的所有公共方法必须标注稳定性：

| 装饰器 | 含义 | 示例 |
|--------|------|------|
| `@stable` | 签名冻结，不会变 | `core.llm.chat()`, `core.db.execute()` |
| `@evolving` | 可能加可选参数，向后兼容 | `core.voice.transcribe()`, `core.knowledge.search()` |
| `@internal` | 模块禁止直接调用 | core 内部实现 |

**强制规则：**
- 模块只能调用 `@stable` 和 `@evolving` 的方法
- 模块直接调用 `@internal` 方法视为违规
- 删除 `@stable` 方法必须经过 deprecation 周期（先标记 `@evolving` → 等一个版本 → 删除）

### 三、数据库命名与所有权

**表命名强制前缀：**

```
模块表：   {module}_{entity}       例：thinker_messages、literature_papers
核心表：   core_{entity}           例：core_notes、core_llm_usage、core_scheduled_jobs
```

**所有权规则：**
- 模块表由模块完全拥有，其他模块禁止直接 JOIN
- 跨模块数据交换必须通过 `core_notes` 或事件总线
- 核心表（`core_notes`）所有模块可读可写，但必须遵守统一字段约定
- 模块内部 Schema 迁移沿用 `MIGRATIONS` 字典模式（`{version: sql}`），放在 `schema.py` 中
- 核心层在模块注册时自动检查并执行迁移

### 四、知识层契约（`core_notes` 统一笔记表）

所有模块的笔记、摘要、洞察统一存入 `core_notes`，这是跨模块知识关联的基础。

**字段约定：**
```sql
core_notes (
    id, source_module, source_ref,    -- 来源追踪
    content TEXT NOT NULL,             -- 纯 Markdown 正文
    embedding TEXT,                    -- JSON float array
    content_type TEXT,                 -- 'note' | 'insight' | 'resolution' | 'spark' | 'hypothesis' | 'connection'
    tags TEXT,                         -- JSON string array
    metadata TEXT,                     -- JSON 模块自定义侧车数据
    created_at
)
```

**强制规则：**
- `content` 必须是 Markdown 格式（人类可读正文）
- embedding 基于 `content` 生成，不包含 metadata
- 结构化数据放 `metadata`（JSON），不污染正文
- 模块删除时级联删除 `WHERE source_module = '{module}'`

### 五、前端集成规范

**全局 UI 注入：** 模块需要全局浮层（如聊天窗口）时，通过核心层注入，禁止直接修改 `base.html`。

```python
core.register_global_component(
    name="thinker-panel",
    template="thinker/panel.html",
    mount_point="body-end",
    init_script="thinker/panel.js",
)
```

**CSS 隔离：** 模块全局组件必须以 `[data-module="{name}"]` 为 scope：
```css
[data-module="thinker"] .chat-bubble { ... }
```

**路由命名空间：**
```
/{module}/           → 页面路由
/{module}/api/       → API 端点
/{module}/static/    → 静态文件
```

**强制规则：**
- 全局浮层内嵌到 base.html（不用 iframe）
- 模块 JS/CSS 通过核心层注册，自动注入到所有页面
- 模块间样式互不污染

### 六、LLM 调用规范

**统一入口：** 所有 LLM 调用必须通过 `core.llm`，禁止模块自行创建 OpenAI client。

```python
# ✓ 正确
response = await core.llm.chat(messages, temperature=0.7, stream=True)

# ✗ 禁止 — 模块自行创建 client
client = OpenAI(base_url=..., api_key=...)
```

**Prompt 管理：**
- 简易版：写在模块 `config.default.yaml` 中
- 生产版：放在模块 `prompts/` 目录下的 `.md` 或 `.jinja2` 文件
- 核心层提供 `core.llm.load_prompt(module, name, **vars)` 统一加载渲染

**Usage 追踪：** 每次 LLM 调用自动记录（时间戳、模块、用途标签、token 用量）到 `core_llm_usage` 表。模块无需手动记录。

**强制规则：**
- 模块不允许绕过 `core.llm` 调用任何外部 API
- LLM 配置（baseurl/apikey/model）由核心层统一管理，模块不持有自己的 API key

**语音调用同样统一：** 所有 STT/TTS 必须通过 `core.voice`，禁止模块自行创建语音 client。

```python
# ✓ 正确
text = await core.voice.transcribe(audio_bytes)
audio = await core.voice.synthesize(text)

# ✗ 禁止 — 模块自行调用语音 API
import openai
client = openai.OpenAI(...)
client.audio.transcriptions.create(...)
```

语音 API 配置（`core.voice`）独立于 LLM 配置，使用单独的 API key 和 base URL。

### 七、后台任务规范

**任务注册：**
```python
core.scheduler.add(
    module="thinker",
    name="inactivity_check",
    func=check_inactivity,       # async callable
    trigger="interval",
    minutes=5,
    on_error="retry",            # retry | skip | pause_module
)
```

**强制规则：**
- 任务函数必须幂等（多次执行不产生副作用）
- 任务内部异常由调度器捕获，不影响其他模块任务
- 暂停/休眠逻辑在任务函数内部判断（如 `snooze_until`），不过滤调度器
- 长时间任务（>30s）自行管理超时
- 后台任务使用 `asyncio.to_thread` 包装同步 LLM 调用

### 八、事件规范

**命名模式：** `{module}:{entity}:{action}`

```
thinker:message:sent
thinker:resolution:extracted
literature:paper:imported
core:note:created
```

**事件载荷最小字段：** `event_id`（uuid）、`timestamp`、`source_module`

**强制规则：**
- 模块只能订阅核心事件和显式声明依赖的其他模块事件（白名单模式）
- 模块不能拦截另一个模块的关键流程
- 事件总线基于 `asyncio.Event` + 回调列表，不引入消息队列

### 九、配置结构

```yaml
core:                          # 核心配置（跨模块共享）
  llm: ...
  voice:
    api_key: "sk-..."
    api_base_url: "https://api.gpt.ge/v1"
    stt_model: "whisper-large-v3"
    tts_model: "gemini-2.5-pro-preview-tts"
  knowledge:
    embedding_model: "text-embedding-3-small"
  scheduler:
    timezone: "Asia/Shanghai"

modules:                       # 模块配置（按模块名隔离）
  literature:
    sources: ...
  thinker:
    inactivity_timeout_minutes: 10
    personality: "friend"
```

**强制规则：**
- 每个模块必须提供 `config.default.yaml`（可配置项 + 默认值）
- 启动时合并：`模块默认值 < config.yaml < 环境变量`
- 模块读到的是合并后的最终配置，不感知优先级

### 十、测试要求

| 类型 | 最低要求 |
|------|----------|
| 单元测试 | ≥3 个（mock LLM 和 DB） |
| 集成测试 | ≥1 个（与核心层注册/调用链路） |
| 契约测试 | `register()` 返回合法的 `ModuleInfo` 对象 |

### 十一、现有模块

项目已有两个平台化模块，以下为各自的关键架构信息。

#### Thinker 模块（`modules/thinker/`）

思考伙伴——浮动侧边栏对话子系统。**与系统数据隔离，独立运作。**

**核心文件：**
- `chat.py` — ChatEngine：管理对话会话、流式响应。MemoryPipeline 替代了旧的 `_get_memory_context()`
- `memory.py` — MemoryPipeline：多路召回 → LLM 重排 → System Prompt 注入
- `profile.py` — ProfileManager：用户画像提取、指数滑动平均合并
- `resolutions.py` — ResolutionTracker：承诺追踪（pending/in_progress/done/cancelled）
- `knowledge_linker.py` — KnowledgeLinker：消息 embedding + 相关笔记检索

**数据库表（schema v2）：**
- `thinker_conversations` — 对话会话（chat/socratic/feynman/kpt/orid）
- `thinker_messages` — 对话消息（含 embedding）
- `thinker_resolutions` — 用户承诺追踪（含 deadline）
- `thinker_pending_questions` — 主动提问队列
- `thinker_memory_index` — 记忆索引（指向 core_notes，含 importance + recall_count）
- `thinker_user_profile` — 用户画像（单行，id=1）

**记忆管道流程：**
```
用户消息 → MemoryPipeline.retrieve() — 5 路并行召回
   ├── 语义相似（KnowledgeLayer.search_by_embedding）
   ├── 未完成承诺（thinker_resolutions）
   ├── 最近摘要（thinker_memory_index + core_notes）
   ├── 用户画像（thinker_user_profile）
   └── 闲置念头（thinker_memory_index spark 类型）
       ↓
   MemoryPipeline.rerank() → LLM 重排（≤6 跳过，>6 用 Haiku）
       ↓
   MemoryPipeline.inject() → 拼入 System Prompt
       ↓
   LLM 生成回复 → fire-and-forget 编码写回
     （摘要 → core_notes insight，画像 → ProfileManager，承诺 → extract_resolutions）
```

**事件：** `thinker:message:sent`、`thinker:summary:generated`、`thinker:resolution:extracted`
**调度：** `inactivity_check`（interval）、`resolution_check`（cron 每日 9 点）
**前端：** 全局浮层（Alpine.js 侧边栏），非 iframe 内嵌
**语音：** CoreVoice (OpenAI 兼容 API) — STT 用 `whisper-large-v3` + TTS 用 Gemini TTS，前端仅录音上传

#### Ideator 模块（`modules/ideator/`）

知识挖掘引擎——跨论文、跨笔记、跨项目发现隐藏关联，产生研究火花。**深度绑定系统数据**。

架构演变：v1（简单管道）→ v2（双模型审查+圆桌）→ v3（Agent Team 6 坐席）→ **V1.0 正式版（idea 级 embedding + MaxSim 聚合召回 + 闪电筛选 + 多轮辩论 + 圆桌讨论）**。

**当前管道流程（V1.0）：**

```
S0 CrossRecall (6路) → S1 Per-Pair Score (逐对LLM, 完整原文)
→ S2 Per-Group Spark Gen (C1共享源分组)
→ S2.25 Lightning Filter (3 flash Rev评分, threshold≥0.4全保留, 不足则top 10)
→ S2.5 Deepen (生成完整研究草稿)
→ S3 Multi-Round Debate (6坐席: 初评→辩论回合→重评→终裁→简报)
→ S4 Dedup Save (embedding去重) + depth_content → S6 Audit
→ [用户发起圆桌: AgentTeam 3阶段顺序讨论 gen→rev并行→arb并行]
```

**Idea 级 embedding 召回（V1.0 新增）：**
```
笔记写入 → IdeaExtractor.get_or_extract_ideas()
  ├── 查 ideator_note_ideas 缓存 (v9 表)
  ├── 未命中 → deepseek-v4-flash 提取独立 idea
  ├── 每个 idea 独立 BGE embedding
  └── 写入缓存

CrossRecall S0:
  ├── 每条笔记提取 N 个 idea
  ├── 每个 idea 独立搜索 ideator_note_ideas (余弦相似度)
  ├── MaxSim 聚合: pair_score = max(cos(ai, bj)) 跨所有 idea 对
  └── 按 (max_similarity, idea_match_count) 排序返回笔记对
```

**核心组件：**
| 文件 | 职责 |
|------|------|
| `idea_extractor.py` | flash LLM 提取独立 idea + embedding + 缓存 |
| `cross_recall.py` | 6 路召回，支持 note 级和 idea 级两套搜索 + MaxSim |
| `debate_engine.py` | 6 坐席多轮辩论（初评→辩论→重评→终裁→简报） |
| `agent_team.py` | 3 阶段顺序圆桌：gen→rev 并行→arb 并行，含 5-Why |
| `arbiter.py` | 毕业决策 + 配额 + 工具授权 |
| `pipeline.py` | S0→S1→S2→S2.25→S2.5→S3→S4→S6 全管道编排 |
| `spark_store.py` | embedding 余弦去重（0.85 merge / 0.60 flag） |
| `graduation.py` | Hot/Warm/Cold 三层上下文生命周期 |

**数据库表（schema v9）：**
- `ideator_sparks` — source_type 无 CHECK 约束
- `ideator_note_ideas` **(v9)** — idea 级 embedding 缓存，UNIQUE(note_source, note_id, idea_index)
- `ideator_cross_links` / `ideator_review_records` / `ideator_pipeline_runs` / `ideator_recall_weights`
- `ideator_roundtables` + `ideator_roundtable_messages` + `ideator_roundtable_snapshots` + `ideator_team_memory`

**关键设计：**
- `_chat_with_retry`：3 次重试（含 JSON 解析失败重试），错误反馈注入对话历史
- `_clean_json`：去 markdown 代码块再解析 JSON
- `response_format={"type":"json_object"}` 已全部移除（flash 模型兼容性问题）
- Semaphore(2) 限流 debating，Semaphore(5) 限流 scoring/spark_gen
- `IdeatorLLM.chat(model=...)` 支持模型覆盖（flash 模型用 `deepseek-v4-flash`）
- `reasoning_content` 耗尽检测与告警（思考模型 token 预算被 reasoning 吃光）
- arb_control 失败时 fail-open（CONTINUE 而非 STOP）
- 每条笔记录级的 GraduationManager 消除跨圆桌状态污染
- core_notes 召回过滤 `source_module='literature'` 隔离 thinker 闲聊

**防重复踩坑（已知 bug 修复摘要）：**
- duck-typing (`getattr`) 解决 isinstance 失败；save_spark 接受外部 metadata
- `response_format` → prompt 要求纯 JSON；JSON 解析在重试循环内
- 逐对独立评分（非批量）；`skip_review`/`skip_debate` 解耦
- 14 处 `logger.debug` → `logger.warning`；`resp.choices` None 防护
- 全局 max_tokens 提升适配 DeepSeek 思考模型

### 十二、技术决策（已确认，不可推翻）

以下决策在本项目中为最终裁定，后续开发直接遵循：

1. **语音方案**：CoreVoice 统一入口（`core/voice.py`），OpenAI 兼容 Audio API，STT 用 `whisper-large-v3` + TTS 用 Gemini TTS。前端仅录音上传。
2. **后台调度**：APScheduler (`AsyncIOScheduler`) + SQLite job store
3. **Embedding**：使用与现有 LLM 同一 API 的 embedding 模型，向量存 SQLite TEXT（JSON 数组）
4. **主动提问机制**：后端定时生成问题 → 存 `pending_questions` 表 → 前端轮询获取
5. **前端浮层**：内嵌 base.html（非 iframe），HTMX + Alpine.js
6. **模块间通信**：事件总线（`asyncio.Event` + 回调），不引入消息队列
7. **知识关联**：`core_notes` 统一表 + embedding 余弦相似度，不引入向量数据库
8. **现有代码**：`paperreadagent/` 根级代码保持不动，新建 `modules/` 和 `core/` 目录，后续再重构迁移
