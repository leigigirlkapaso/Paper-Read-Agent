# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 强制前置流程

**在写任何代码之前，必须先调用 `Skill` 工具执行 `code-reuse` skill。** 违反此规则会导致反复造轮子和不一致的实现模式。

## 项目概述

自动化文献调研多智能体系统：**研究构想 → 关键词提取 → 多平台检索 → LLM 筛选 → PDF 下载 → 并发精读 → 综述报告**。现已扩展为模块化知识工作台（Thinker 思考伙伴 + Ideator 知识挖掘引擎）。

## 运行命令

```bash
uv sync                          # 安装依赖
uv run python main.py            # CLI
uv run uvicorn paperreadagent.web.app:app --reload --port 8000  # Web
uv run python -m pytest paperreadagent/tests/                    # 全部测试
uv run python -m pytest paperreadagent/modules/<name>/tests/ -v  # 单模块测试
```

## 目录结构（关键路径）

```
PaperReadAgent/
├── config.yaml                  # 所有配置（含 API key，已 gitignore）
├── config.example.yaml          # 公开示例（占位符 key）
├── pyproject.toml               # uv 依赖
├── paperreadagent/
│   ├── agent1/                  # 检索 + 筛选
│   ├── agent2/                  # 并发精读
│   ├── core/                    # Core, CoreLLM, CoreVoice, KnowledgeLayer, EventBus, Scheduler
│   ├── db/                      # Legacy SQLite (v4)
│   ├── utils/                   # LLM client, PDF downloader/parser, local scanner
│   ├── web/                     # FastAPI + Jinja2 + HTMX + Alpine.js
│   └── modules/                 # 平台化模块 (thinker/, ideator/)
├── projects/                    # 项目数据（已 gitignore）
└── data/lancedb/                # LanceDB 向量索引（已 gitignore）
```

## 关键开发约束

- `uv run` 管理 Python 环境；`check_same_thread=False` 必须（DB 多线程共享）
- AGENT2 每篇独立 LLM 调用；搜索/下载用 `aiohttp` + `asyncio.Semaphore`
- 修改 schema 时增量添加 `MIGRATIONS[vN]`，不改已有迁移
- `BASE_DIR` 指向项目根；Web pipeline 在 `asyncio.to_thread` 内运行
- PDF 解析用 `pymupdf4llm` → Markdown；LLM 调用全部走 `core.llm`
- Embedding：`BAAI/bge-m3`（1024 维，多语言）；`core/knowledge.py` v3 语义分块 + LanceDB note_chunks 表
- Voice：统一走 `core.voice`（OpenAI 兼容 Audio API）；前端仅录音上传
- PDF 解析：arXiv 论文优先 ar5iv HTML（公式天然保留），非 arXiv 回退 pymupdf4llm

## ⚠️ 语音格式铁律（已反复踩坑 3 次，禁止再改）

**api.gpt.ge 的 Whisper API 接受：flac, m4a, mp3, mp4, mpeg, mpga, oga, ogg, wav。不接受 webm 和 opus。**

```
Chrome: audio/webm;codecs=opus → _normalize_format → "wav" → pyav 转码
iOS:    audio/mp4              → 直接透传
Firefox: audio/ogg;codecs=opus → 直接透传
```

关键文件：`voice.py:16-48`（normalize + pyav 转换）、`routes.py:352-355`（格式白名单）、`core/voice.py:25`（默认值）。
配置：`stt_language: ""`（空 = 自动检测语言），`_DEFAULT_AUDIO_FORMAT = "webm"`（名义值，会被 normalize）。

**禁止：** 改 `_DEFAULT_AUDIO_FORMAT` 为 "opus"/"wav"；硬编码 `_stt_format`；移除 `av` (pyav) 依赖。

## ⚠️ PDF 公式提取（已踩坑，禁止再试 MinerU）

**MinerU/magic-pdf 在 Windows 不可用。** detectron2 不支持 Windows；UniMERNet 权重是 HF gated repo。正确方案：arXiv 论文用 ar5iv HTML（`https://ar5iv.labs.arxiv.org/html/{arxiv_id}`），公式完美保留。非 arXiv 用 pymupdf4llm。

## Git 分支策略 + 安全

- **`master`**（私有）← → **`public`**（开源, orphan 分支, push to GitHub `main`）
- 同步方式：cherry-pick，每次确认无 API key 泄露
- **绝对禁止：** 代码/注释/commit 写真实 key；`config.yaml`/`*.db`/`projects/` 提交到 public
- config.example.yaml 用 `sk-YOUR_API_KEY_HERE` 占位；`.claude/` 永不提交
- gitignore 保护：`config.yaml` `*.db` `projects/` `data/` `.venv` `__pycache__/` `.claude/` `outputs/` `*.log`

## 认证系统

首次访问自动设定密码（PBKDF2-HMAC-SHA256, 600k 迭代）。session cookie (HMAC-SHA256, 30 天)。5 次失败/5min → 15min IP 封锁。改密码后所有旧 cookie 失效。

## 模块开发契约

每个模块 `paperreadagent/modules/<name>/` 必须：
```
__init__.py          # def register(core: Core) -> ModuleInfo  ← 唯一对外接口，禁止模块间直接 import
routes.py            # FastAPI APIRouter
schema.py            # 独立 DDL + MIGRATIONS 字典
tests/               # ≥3 单元测试 + ≥1 集成测试
```

**铁律：**
- 表命名：`{module}_{entity}`（模块表）、`core_{entity}`（核心表）。跨模块数据交换走 `core_notes` 或 EventBus。
- LLM/语音调用全部走 `core.llm` / `core.voice`，模块禁止自建 client。
- Core API 标注 `@stable` / `@evolving` / `@internal`；模块只能调前两者。
- 路由：`/{module}/` `/api/` `/static/`；CSS scope：`[data-module="{name}"]`
- Background 任务用 CoreScheduler，事件用 EventBus（`{module}:{entity}:{action}`）
- 配置：模块提供 `config.default.yaml`，启动时合并 → 模块不感知优先级

## 现有模块

- **Thinker** ([`modules/thinker/`](paperreadagent/modules/thinker/))：思考伙伴 + 演讲预演。
- **Ideator** ([`modules/ideator/`](paperreadagent/modules/ideator/))：知识挖掘引擎。

详细架构见各模块目录下的 `CLAUDE.md`。

## 跨模块接口

所有模块通过 Core API 和事件总线通信，**禁止模块间直接 import**。

```
Thinker ── thinker:summary:generated ──→ Ideator (增量挖掘)
Core ──── core:note:created ──────────→ Thinker, Ideator (双订阅)
```

| 模块 | 公开 API | 发出事件 | 订阅事件 |
|------|----------|----------|----------|
| Thinker | 无（仅 routes） | `thinker:message:sent`, `thinker:summary:generated`, `thinker:resolution:extracted` | `core:note:created` |
| Ideator | `get_roundtable_manager()` | 无 | `core:note:created`, `thinker:summary:generated` |
