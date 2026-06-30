# PaperReadAgent

> 面向研究者的自动化文献调研与知识挖掘工作台：从研究构想到多平台检索、LLM 精读、综述生成，再到跨论文火花挖掘与多智能体圆桌研讨。

PaperReadAgent 不是一个简单的 paper downloader，也不只是 PDF 总结器。它试图把一次完整的研究工作流串起来：

```text
研究构想
  ↓
关键词提取 → 多平台检索 → 引文滚雪球 → Hybrid 预筛 → LLM 精排
  ↓
PDF / HTML 获取 → 并发精读 → 结构化抽取 → 跨论文对比表
  ↓
综述报告
  ↓
Thinker 思考伙伴 + Ideator 知识挖掘 + Agent Roundtable 圆桌研讨
  ↓
项目大纲 / Project Brief / 项目关系图
```

---

## ✨ 核心能力一览

| 能力 | 说明 |
|------|------|
| **自动文献调研流水线** | 输入研究构想，自动完成关键词提取、检索、筛选、下载、精读、综述生成 |
| **检索增强** | Citation Snowballing（OpenAlex 主 / Semantic Scholar 兜底）+ bge-m3 dense + BM25 RRF Hybrid Pre-filter |
| **并发精读** | 每篇论文独立 LLM 调用；arXiv 优先 ar5iv HTML 保留公式，非 arXiv fallback 到 pymupdf4llm |
| **结构化抽取** | 每篇精读同时产出 Markdown + 7 字段 JSON（problem / methods / datasets / metrics / baselines / limitations / contributions） |
| **跨论文对比表** | Session 级横向比较论文的方法、数据集、关键指标、局限和贡献 |
| **Thinker 思考伙伴** | 对话式研究陪伴、语音输入、记忆召回、承诺追踪、主动提问 |
| **Ideator 知识挖掘引擎** | 从论文、笔记、摘要中挖隐藏关联，生成研究火花（spark） |
| **Agent Roundtable 圆桌系统** | 6 个智能体围绕 spark 进行生成、审查、仲裁；带 facts 注入、流式输出、秘书大纲 |
| **项目关系图** | Cytoscape.js 可交互知识地图：项目 / 论文 / 笔记 / 火花四类节点，五类关系边 |

---

## 🔎 Agent1：检索与筛选

Agent1 负责从研究构想到候选论文池，并尽量提升召回质量。

### 多平台检索

支持多种开放学术数据源：

- **arXiv** — 免费、稳定，适合 CS / AI / 数学 / 物理
- **OpenAlex** — 2.5 亿+ 文献，适合跨学科兜底
- **Semantic Scholar** — 引文网络与语义补充
- **DBLP** — CS 顶会 / 期刊补充
- **Crossref** — DOI / 出版元数据补充
- **OpenReview** — ICLR / NeurIPS 等审稿平台补充
- **Europe PMC** — 生物医学方向补充
- **Papers with Code** — 任务/数据集/benchmark 线索

### Citation Snowballing

传统关键词检索容易漏掉重要论文。PaperReadAgent 支持基于种子论文的引文滚雪球：

```text
LLM 初筛后的高相关论文
  ↓
OpenAlex backward references + forward citations
  ↓
Semantic Scholar 兜底
  ↓
LLM 再筛选
  ↓
加入候选池
```

### Hybrid Pre-filter

在 expensive LLM 精排之前，系统先用便宜信号做粗排：

```text
title + abstract
  ↓
bge-m3 dense embedding
  +
BM25 sparse keyword matching
  ↓
RRF 融合排序
  ↓
Top-K 进入 LLM relevance filter
```

这样可以在大候选池下减少 LLM 调用，同时保留语义相关和关键词匹配两类信号。

---

## 📖 Agent2：并发精读与综述生成

Agent2 负责下载、解析和深入阅读论文。

### PDF / HTML 解析

- arXiv 论文优先使用 **ar5iv HTML**，公式天然保留
- 非 arXiv 论文 fallback 到 **pymupdf4llm** 转 Markdown
- 每篇论文独立处理，支持 asyncio 并发

### 精读输出

每篇论文会产出：

1. **人类可读 Markdown 精读笔记**
2. **fact-card 自检块**
3. **7 字段结构化 JSON**：
   - `problem`
   - `methods`
   - `datasets`
   - `metrics`
   - `baselines`
   - `limitations`
   - `contributions`

这些结构化字段支撑跨论文对比表、圆桌 facts 注入和后续 synthesis。

### 跨论文综合

系统会将高相关论文的精读结果合成为综述报告，并尽量控制上下文长度，避免把过多论文粗暴塞给 LLM。

---

## 🧠 Thinker：思考伙伴

Thinker 是一个独立的研究思考模块，适合在读论文、写想法、准备演讲时使用。

能力包括：

- 对话式研究陪伴
- 移动端语音输入 / STT / TTS
- 多路记忆召回
- 用户画像提取
- 承诺追踪（pending / in_progress / done / cancelled）
- 闲置念头整理
- 演讲预演与问答训练

Thinker 不直接依赖 Ideator，但会通过事件机制把高价值摘要提供给知识层。

---

## 💡 Ideator：知识挖掘引擎

Ideator 是 PaperReadAgent 的知识挖掘模块，目标是：

> 从已有论文、笔记、摘要和项目中发现隐藏关联，生成可继续发展的研究火花。

### 全量挖掘 Pipeline

Ideator 的 full mining pipeline 是一个多阶段流程：

```text
S0  CrossRecall
    6 路召回：跨论文、跨笔记、跨项目、idea-level embedding 等

S1  Per-pair Score
    对候选关联逐对 LLM 打分

S2  Spark Generation
    按关联组生成研究火花

S2.25 Lightning Filter
    轻量快速评审，过滤低价值火花

S2.5 Deepen
    将火花深化为较完整的研究草稿

S3  Multi-Round Debate
    多坐席辩论审查：初评 → 辩论 → 重评 → 终裁 → 简报

S4  Dedup Save
    LanceDB ANN 去重，避免重复火花

S6  Audit
    审计火花的证据支撑与风险
```

### Idea-level embedding 召回

Ideator 不只把整篇笔记当作一个 embedding。对于长笔记，它会分块提取多个独立 idea，并对每个 idea 单独建立 embedding：

```text
note / summary
  ↓
IdeaExtractor
  ↓
idea_1, idea_2, idea_3, ...
  ↓
bge-m3 embedding
  ↓
MaxSim 聚合召回
```

这让系统可以发现更细粒度的跨文献联系。

---

## 🪑 Agent Roundtable：智能体圆桌

圆桌系统是 Ideator 的交互式研究研讨层。它不是普通聊天室，而是一个围绕研究火花展开的多智能体审查系统。

### 6 个坐席

| 坐席 | 角色 |
|------|------|
| **Generator** | 为研究火花辩护、补充推理、吸收反馈 |
| **Reviewer α** | 饱和度审查：这个想法是否已被已有工作覆盖？真正增量在哪？ |
| **Reviewer β** | 反例审查：是否有论文结果、局限或实验事实削弱这个想法？ |
| **Reviewer γ** | 补足审查：缺少哪些 baseline、数据集、指标、消融或失败分析？ |
| **Arbiter α** | 仲裁分歧、控制讨论方向 |
| **Arbiter β** | 深化高价值方向，推动项目化 |

### 圆桌特色

#### 1. 论文 facts 注入

圆桌启动时，系统会把 spark 直接关联论文的结构化抽取结果注入到 agent prompt 中：

```text
论文 P1: problem / methods / datasets / metrics / limitations / contributions
论文 P2: ...
```

Reviewer 必须基于这些事实给出建设性反馈，避免空泛评价。

#### 2. Reviewer 三段式输出

Reviewer 回复被约束为：

```text
【问题点】一句话指出问题
【事实依据】引用论文事实或 agent 发言
【建议修复】给出可执行改进
```

目标不是否定想法，而是帮助 Generator 把方案打磨得更可执行。

#### 3. SSE 流式输出

圆桌不再等所有模型生成完才显示。系统通过 Server-Sent Events 把 token 级输出实时推到前端：

```text
AgentTeam._agent_speak
  ↓ chat_stream
RoundtableStreamHub
  ↓ SSE
Browser EventSource
```

用户可以看到多个坐席的内容实时出现。

#### 4. Secretary Agent 实时大纲

每轮讨论结束后，秘书 agent 会读取：

- 上一轮大纲
- 本轮所有 agent 回复
- 论文 facts_block
- spark 内容

然后更新一份 7 节项目大纲：

1. 研究问题
2. 核心假设
3. 方法设计
4. 实验计划
5. 风险清单
6. 行动项
7. 开放问题 / 分歧

大纲通过 `outline_update` SSE 事件实时显示在圆桌页面侧边栏。

#### 5. 自动生成 Project Brief

关闭圆桌时，系统会把秘书最新大纲注入到 `ProjectBriefService`，自动生成 6 维度项目可行性书：

- feasibility
- theory
- experiment_plan
- expected_results
- risk_assessment
- differentiation

这让圆桌讨论自然沉淀为可执行项目文档。

---

## 🗺️ 项目关系图

项目关系图用于展示知识库中的结构关系。新版关系图使用 Cytoscape.js，支持 4 类节点和 5 类边。

### 节点类型

| 节点 | 说明 |
|------|------|
| Project | 研究项目 |
| Paper | 论文 |
| Note | 笔记 |
| Spark | Ideator 生成的研究火花 |

### 边类型

| 边 | 说明 |
|----|------|
| contains | 项目包含论文 |
| shared | 同一 arXiv 论文出现在多个项目中 |
| has_note | 论文有笔记 |
| cites | Spark 引用了论文 |
| cross_link | Ideator 发现的语义关联 |

支持：

- 默认项目 + 论文全景
- 勾选加入笔记 / 火花层
- 单击节点查看详情
- 双击节点展开 1-hop 邻居
- 搜索节点并高亮
- 切换布局（force / circle / hierarchy）

---

## 🚀 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/leigigirlkapaso/Paper-Read-Agent.git
cd Paper-Read-Agent
```

### 2. 安装依赖

项目使用 `uv` 管理 Python 虚拟环境和依赖：

```bash
uv sync
```

### 3. 创建配置文件

```bash
cp config.example.yaml config.yaml
```

### 4. 填写 API Key

编辑 `config.yaml`，将占位符替换为你的真实 API Key：

```yaml
llm:
  api_base_url: "https://api.deepseek.com/v1"
  api_key: "sk-YOUR_API_KEY_HERE"
  model_name: "deepseek-v4-pro"

core:
  llm:
    api_base_url: "https://api.deepseek.com/v1"
    api_key: "sk-YOUR_API_KEY_HERE"
```

> 语音功能使用 `core.voice`，不需要语音时可以暂时不配置。

### 5. 修改研究构想

编辑 `config.yaml` 中的 `research.topic`：

```yaml
research:
  topic: |
    你的研究构想写在这里。
    建议 200-500 字，越具体检索效果越好。
```

### 6. 启动 Web 界面

```bash
uv run uvicorn paperreadagent.web.app:app --reload --port 8000
```

浏览器打开：

```text
http://localhost:8000
```

首次访问会提示设置登录密码。

---

## ⚙️ 配置说明

完整配置见 `config.example.yaml`。

常用配置块：

| 配置 | 说明 |
|------|------|
| `llm` | 主流程 LLM API（Agent1/Agent2） |
| `core.llm` | 模块层 LLM API（Thinker/Ideator/Roundtable） |
| `research` | 研究构想、检索参数、筛选阈值 |
| `sources` | arXiv / OpenAlex / DBLP / PMC / Crossref / OpenReview 等数据源开关 |
| `downloader` | PDF 下载配置 |
| `concurrency` | Agent2 并发精读数量 |
| `core.voice` | 语音功能（可选） |

---

## 🧪 测试

```bash
# 运行全部测试
uv run python -m pytest paperreadagent/ -v

# 单模块测试
uv run python -m pytest paperreadagent/modules/thinker/tests/ -v
uv run python -m pytest paperreadagent/modules/ideator/tests/ -v

# Core 测试
uv run python -m pytest paperreadagent/core/tests/ -v
```

---

## 🔐 隐私与安全

- `config.yaml` 被 `.gitignore` 排除，不会提交到 GitHub
- API Key 只保存在本地配置文件
- 运行数据默认保存在本地 SQLite / `projects/` / `data/` 目录
- `projects/`、`data/`、`*.db`、`.venv/`、`.claude/` 等运行时文件均不应公开提交
- 首次访问 Web UI 会设置本地密码，密码使用 PBKDF2-HMAC-SHA256 哈希存储

---

## FAQ

### Q: 启动报错 `config.yaml not found`

复制示例配置：

```bash
cp config.example.yaml config.yaml
```

然后填写 API Key。

### Q: DeepSeek API 返回 401

检查 `config.yaml` 中 `api_key` 是否正确，确保没有多余空格、引号或换行。

### Q: 为什么第一次 Hybrid Pre-filter 会加载 bge-m3？

系统默认使用 `BAAI/bge-m3` 做本地 embedding。第一次运行会下载/加载模型，后续会复用本地缓存。若机器内存较小，可以临时关闭：

```yaml
research:
  enable_hybrid_prefilter: false
```

### Q: 下载的 PDF 在哪里？

在：

```text
projects/<项目名>/sessions/<会话ID>/papers/
```

### Q: 能用其他 LLM 吗？

可以。任何兼容 OpenAI Chat Completions 格式的 API 都可以。修改：

```yaml
api_base_url
api_key
model_name
```

即可。

---

## License

本项目仍在快速迭代中。请根据仓库 LICENSE 使用。
