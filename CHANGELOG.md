# PaperReadAgent 版本日志

## V1.0 — 2026-05-23

### 核心特性

**文献调研管道**
- 多平台检索：arXiv、Semantic Scholar、Papers With Code、OpenAlex 四源并行
- LLM 三层策略关键词提取 + 批量相关性打分筛选（0-1 精度）
- 多源 PDF 级联下载：arXiv → 直接 URL → Unpaywall → Semantic Scholar OA → Sci-Hub
- 并发 AI 精读：asyncio.Semaphore 控制，支持 100 路并发 LLM 调用
- 结构化综述报告自动生成

**Ideator 知识挖掘引擎**
- 6 路交叉召回：similarity / contradiction / cross_project / cross_layer / random_walk / timeline
- Idea 级 embedding：flash LLM 拆分笔记为独立 idea → 各自 BGE embedding → MaxSim 聚合召回
- 8 坐席多轮辩论审查：3 审查者初评 → 辩论回合 → 重评 → 终裁 → 书记员简报
- 闪电筛选：3 flash reviewer 廉价评分，阈值过滤，只让有价值火花进入昂贵辩论
- AgentTeam 三阶段顺序圆桌：gen → rev 并行 → arb 并行，5-Why 深度分析法
- 直接圆桌：无需火花即可发起 AI 团队讨论
- 知识去重：基于 embedding 余弦相似度的火花合并 / 标记 / 入库
- 毕业机制：Hot/Warm/Cold 三层上下文生命周期管理

**Thinker 思考伙伴**
- 浮动侧边栏对话，HTMX + Alpine.js 内嵌
- 5 路记忆召回管线：语义相似 / 未完成承诺 / 最近摘要 / 用户画像 / 闲置念头
- 用户画像提取（指数滑动平均）
- 承诺追踪（pending/in_progress/done/cancelled）
- 主动提问队列（定时生成 → 侧边栏推送）
- CoreVoice 语音对话：STT（whisper-large-v3）+ TTS（Gemini TTS）

**平台化架构**
- CoreLLM 统一 LLM 入口（同步/异步/流式/embedding/prompt 加载）
- CoreVoice 统一语音入口
- KnowledgeLayer 统一知识库（core_notes 表，embedding 语义搜索 + 矛盾检测）
- EventBus 模块间通信（asyncio.Event + 回调）
- CoreScheduler 后台任务调度（APScheduler + SQLite job store）
- CoreFrontend 全局组件注册（JS/CSS 注入）
- 模块化开发规范：register() 唯一入口、@stable/@evolving/@internal 稳定性标注、表命名前缀

**Web GUI**
- FastAPI + Jinja2 + HTMX + Alpine.js + Tailwind CSS
- Dashboard 项目总览 → 会话列表 → 论文详情（PDF.js + Markdown 渲染）
- SSE 实时进度推送
- 火花卡片网格 + 详情面板（S3 简报 + 辩论回合 + 研究草稿 + 圆桌记录）
- 圆桌讨论独立页面（坐席面板 + 消息区 + 记忆侧栏）

### 数据模型
- SQLite WAL 模式，版本化迁移 v1→v9
- Schema v9: 28 张表（核心 3 张 + thinker 6 张 + ideator 14 张 + 文献 5 张）
- core_notes 统一笔记表（跨模块知识关联，embedding 向量 JSON 存储）
- FTS5 全文索引（论文标题 + 摘要）

### 技术决策
- Python 3.13 + uv 包管理
- OpenAI 兼容 API（不绑定特定模型供应商）
- 本地 BGE 中文 embedding 模型（BAAI/bge-large-zh-v1.5）
- IDE 模块只依赖 core_notes 表（不直接 JOIN 其他模块表）
- 前端浮层内嵌 base.html（非 iframe）

---

## 里程碑

| 版本 | 日期 | 变更 |
|------|------|------|
| V1.0 | 2026-05-23 | 正式版发布 — idea 级 embedding、V9 schema、LLM 并发 100、多轮辩论审查、圆桌讨论 |
| V0.9 | 2026-05-20 | 三阶段顺序圆桌、5-Why 分析、建设性引导 prompt、直接圆桌 |
| V0.8 | 2026-05-14 | AgentTeam 6 坐席架构、闪电筛选、全局截断清理、max_tokens 提升 |
| V0.7 | 2026-05-11 | DebateEngine 8 坐席辩论审查、S2.25 闪电筛选、pipeline 重排 |
| V0.6 | 2026-05-09 | Ideator v2 审查升级 — 双模型交叉审查 + Tier 3 仲裁 + 溯源审计 |
| V0.5 | 2026-05-06 | Ideator 模块初始化 — 4 阶段管道 + CrossRecall 6 路召回 |
| V0.4 | 2026-05-03 | Thinker 模块平台化 — 记忆管线、用户画像、承诺追踪 |
| V0.3 | 2026-04-28 | 多源 PDF 下载（Unpaywall + S2 OA + Sci-Hub）、DOI 数据模型 |
| V0.2 | 2026-04-20 | Web GUI（FastAPI + Jinja2 + HTMX）、SSE 进度、论文详情页 |
| V0.1 | 2026-04-10 | 基础 CLI 管道 — 检索 → 筛选 → 下载 → 精读 → 综述报告 |
