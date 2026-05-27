# PaperReadAgent

自动化文献调研多智能体系统：**输入研究构想 → 关键词提取 → 多平台检索 → LLM 相关性筛选 → 多源 PDF 下载 → 并发精读 → 综述报告**。

内置 Thinker（思考伙伴）和 Ideator（知识挖掘引擎）两个智能模块，支持语音对话和跨论文火花发现。

## 系统要求

- **Python 3.12+**（推荐 3.13）
- **uv** 包管理器（[安装指南](https://docs.astral.sh/uv/getting-started/installation/)）
- DeepSeek API Key（[免费注册](https://platform.deepseek.com/)）或其他兼容 OpenAI 格式的 API
- 操作系统：Windows / macOS / Linux 均可

## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/leigigirlkapaso/Paper-Read-Agent.git
cd Paper-Read-Agent
# （克隆后的目录名即为 Paper-Read-Agent）
```

### 2. 创建虚拟环境并安装依赖

项目使用 `uv` 管理 Python 虚拟环境和依赖。克隆后执行：

```bash
uv sync
```

这条命令会：
1. 自动下载项目指定的 Python 版本（如未安装）
2. 在项目根目录创建 `.venv/` 虚拟环境
3. 安装 `pyproject.toml` 中声明的全部依赖

**主要依赖说明：**

| 依赖 | 用途 |
|------|------|
| `fastapi` + `uvicorn` | Web 服务框架 |
| `openai` | LLM API 客户端（兼容 OpenAI 格式） |
| `jinja2` | HTML 模板引擎 |
| `aiohttp` | 异步 HTTP 请求（检索 + 下载） |
| `pymupdf` / `pymupdf4llm` | PDF 解析为 Markdown |
| `sentence-transformers` | 本地 embedding 向量生成 |
| `arxiv` | arxiv API 检索 |
| `apscheduler` | 后台定时任务调度 |
| `pyyaml` | 配置文件解析 |
| `tqdm` | CLI 进度条 |
| `numpy` | 向量相似度计算 |

**虚拟环境说明：**
- `.venv/` 目录已被 `.gitignore` 排除，不会推送到 GitHub
- 每次打开新终端时需要激活：`source .venv/bin/activate`（Linux/macOS）或 `.venv\Scripts\activate`（Windows）
- 使用 `uv run python ...` 可以自动使用虚拟环境，无需手动激活
- 如果依赖安装出问题，删除 `.venv/` 后重新 `uv sync` 即可

### 3. 创建配置文件

```bash
cp config.example.yaml config.yaml
```

### 4. 填写 API Key

编辑 `config.yaml`，将三处 `sk-YOUR_API_KEY_HERE` 替换为你的真实 API Key：

```yaml
# 顶层 LLM 配置（AGENT1 检索筛选 + AGENT2 精读使用）
llm:
  api_base_url: "https://api.deepseek.com/v1"
  api_key: "sk-YOUR_API_KEY_HERE"        # ← 替换为你的 DeepSeek Key
  model_name: "deepseek-v4-pro"

# 核心层 LLM 配置（Thinker + Ideator 模块使用）
core:
  llm:
    api_base_url: "https://api.deepseek.com/v1"
    api_key: "sk-YOUR_API_KEY_HERE"      # ← 替换为你的 DeepSeek Key

  # 语音功能（可选，不需要可跳过）
  voice:
    api_base_url: "https://api.gpt.ge/v1"
    api_key: "sk-YOUR_API_KEY_HERE"      # ← 替换为你的 Voice API Key
```

> **只用 DeepSeek？** 把上面两处 `llm.api_key` 填好即可。语音功能暂时不管它，不影响核心文献调研流程。

> **也用其他模型？** `api_base_url` 支持任何兼容 OpenAI 格式的接口（OpenAI、Gemini、通义千问、SiliconFlow 等），只需修改 `api_base_url`、`api_key`、`model_name` 为对应的值。

### 5. 修改研究构想

编辑 `config.yaml` 中 `research.topic`，替换为你的研究问题。这是整个系统的输入——系统会围绕这个主题去检索、筛选、精读论文。

```yaml
research:
  topic: |
    你的研究构想写在这里。
    可以是多行，越详细检索效果越好。
    ...（建议 200-500 字）
```

其他可调参数（保持默认即可跑通第一次）：
- `sources.openalex: true` — OpenAlex 免费，250M+ 文献，建议保留开启
- `sources.semantic_scholar: false` — 需要免费 API Key，默认关闭
- `max_search_results: 200` — 每条 query 返回的候选文献数
- `relevance_threshold: 0.7` — 相关性筛选阈值（0-1），低于此值的论文被过滤

### 6. 启动 Web 界面

```bash
uv run uvicorn paperreadagent.web.app:app --reload --port 8000
```

浏览器打开 `http://localhost:8000`，首次访问会提示设置登录密码。

### 7. 开始第一次文献调研

1. 在首页点击 **"新建项目"**
2. 填写项目名称
3. 点击 **"开始检索"**
4. 系统自动执行：关键词提取 → 多平台搜索 → LLM 筛选 → PDF 下载 → 并发精读
5. 完成后在项目页面查看报告

## 模块功能

### Thinker — 思考伙伴

全局浮动侧边栏对话助手，支持语音输入（移动端录音 + Whisper 转写 + Gemini TTS 朗读）。自动追踪你的研究思路、提取承诺、生成闲置念头。

### Ideator — 知识挖掘引擎

跨论文、跨笔记、跨项目发现隐藏关联，生成研究火花。六阶段管道：交叉召回 → 逐对评分 → 火花生成 → 闪电筛选 → 多轮辩论审查 → 去重入库。支持圆桌讨论（6 模型坐席辩论）。

## 架构概览

```
用户输入研究构想
    │
    ▼
AGENT1: 关键词提取（LLM 三层策略）
    │
    ├── arxiv API 检索
    ├── Semantic Scholar API
    ├── Papers With Code API
    └── OpenAlex API（免费 2 亿+ 文献）
    │
    ▼
AGENT1-B: LLM 批量打分筛选（相关性 0-1 分）
    │
    ▼
多源 PDF 下载（arXiv → Unpaywall → Semantic Scholar → Sci-Hub）
    │
    ▼
AGENT2: asyncio 并发 LLM 精读（每篇独立调用）
    │
    ▼
综述报告 + Thinker 思考伙伴 + Ideator 火花挖掘
```

## 配置参考

完整配置项见 `config.example.yaml`，每个参数都有中文注释。

关键配置块：
- `llm` — 大语言模型 API
- `research` — 研究构想 + 检索参数
- `sources` — 四平台开关（arxiv / semantic_scholar / papers_with_code / openalex）
- `downloader` — 多源 PDF 下载（Unpaywall 需邮箱，Sci-Hub 默认关闭）
- `core.voice` — 语音功能（STT + TTS）
- `concurrency` — AGENT2 最大并发数

## 命令行运行

除了 Web 界面，也可以通过 CLI 运行：

```bash
uv run python main.py
```

交互式菜单依次选择：项目 → 模式（在线检索 / 本地 PDF 扫描）→ 确认参数 → 执行。

## 测试

```bash
# 运行全部测试
uv run python -m pytest paperreadagent/ -v

# 单模块测试
uv run python -m pytest paperreadagent/modules/thinker/tests/ -v
uv run python -m pytest paperreadagent/modules/ideator/tests/ -v

# 单文件测试
uv run python -m pytest paperreadagent/core/tests/test_database.py -v
```

## 常见问题

### Q: 启动报错 "config.yaml not found"

复制 `config.example.yaml` 为 `config.yaml` 并填写 API Key。

### Q: DeepSeek API 返回 401

检查 `config.yaml` 中 `api_key` 是否正确填写，确保没有多余空格或引号。

### Q: arxiv 检索很慢

这是正常的。arxiv API 要求请求间隔 ≥3 秒，四平台并行检索后总耗时约等于最慢的平台。可以暂时关掉不需要的平台：

```yaml
sources:
  arxiv: true
  semantic_scholar: false
  papers_with_code: false
  openalex: false
```

### Q: 下载的 PDF 文件在哪？

在 `projects/<项目名>/sessions/<会话ID>/papers/` 目录下。

### Q: 语音功能不工作

检查 `core.voice` 配置块中的 `api_key` 和 `api_base_url`。语音使用独立的 API（默认是第三方中转），不经过 DeepSeek。

### Q: 我能用其他 LLM 吗？

可以。任何兼容 OpenAI Chat Completions 格式的 API 都可以。修改 `config.yaml` 中的 `api_base_url`、`api_key`、`model_name` 即可。已知可用：
- DeepSeek（`api_base_url: https://api.deepseek.com/v1`）
- OpenAI（`api_base_url: https://api.openai.com/v1`）
- SiliconFlow 硅基流动
- 阿里云百炼
- 各种 OpenAI 兼容中转站

### Q: 为什么登录后 30 天不需要重新输密码？

系统使用 PBKDF2-HMAC-SHA256 哈希密码 + HMAC 签名 session cookie，有效期 30 天。这是为了使用方便设计的安全机制，不会在服务器上明文存储密码。
