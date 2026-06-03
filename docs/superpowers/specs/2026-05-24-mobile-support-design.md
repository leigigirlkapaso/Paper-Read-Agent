# PaperReadAgent V2 — 移动端支持设计

> 2026-05-24 | 设计讨论产出

## 目标

在 V1.0 桌面端基础上，让安卓手机浏览器能完成和电脑上一样的事情（文献调研 pipeline、ideator 火花挖掘、thinker 对话），数据库仍在电脑上，不做数据同步。手机端不显示 PDF，用 AI 分析的 markdown + 笔记替代。

## 配置变更

`config.yaml` 新增 `server` 块：

```yaml
server:
  host: "0.0.0.0"     # 监听地址（127.0.0.1=仅本机，0.0.0.0=局域网可访问）
  port: 8000
  secret_key: ""       # Cookie 签名密钥，留空则首次启动自动生成随机字符串
```

`app.py` 的 `main()` 从 `config.yaml` 读取 `server.host` / `server.port` 替代硬编码 `127.0.0.1:8000`。

## 网络安全架构

```
电脑 (FastAPI, host=0.0.0.0:8000)
  ├── 同一 WiFi → 手机直接访问 http://192.168.x.x:8000
  └── 远程 4G/5G → Tailscale / ZeroTier 加密隧道 → http://<vpn-ip>:8000
```

- `config.yaml` 新增 `server` 配置块（`host` / `port`），默认 `host: 0.0.0.0`
- 不做公网端口暴露（无 nginx 反代、无 DDNS）
- 外网访问依赖 Tailscale 等 VPN 方案，系统本身不提供穿透

## 认证系统

### 概览

单账号制，不允许多用户/注册。密码通过 config.yaml 设定或首次启动时引导设置，bcrypt 哈希存储。

### 数据模型

```sql
-- core_users: 单行用户记录
CREATE TABLE IF NOT EXISTS core_users (
    id INTEGER PRIMARY KEY CHECK (id = 1),  -- 只允许一行
    username TEXT NOT NULL DEFAULT 'admin',
    password_hash TEXT NOT NULL,            -- bcrypt
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- core_login_attempts: 防暴力破解
CREATE TABLE IF NOT EXISTS core_login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_address TEXT NOT NULL,
    attempt_time TEXT NOT NULL DEFAULT (datetime('now')),
    success INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_ip 
    ON core_login_attempts(ip_address, attempt_time);
```

### 登录流程

```
请求任何页面 → 检查 session cookie
  ├── 有效 → 正常访问，request.state.is_authenticated = True
  └── 无效 → 302 /login
                ├── GET → 渲染 login.html（居中卡片）
                └── POST → 验证密码
                      ├── 成功 → set cookie → 302 原始目标
                      └── 失败 → 记录 attempt → 返回错误
```

- Session cookie：Hs256 签名（`{user_id, exp}` + secret_key），有效期 30 天
- Secret key 从 config.yaml 读取，无外部库依赖
- 防暴力破解：`core_login_attempts` 记录每次尝试 → 同 IP 5 分钟内失败 ≥5 次 → 封禁 15 分钟。封禁状态存内存（重启清零），尝试记录持久化用于审计
- 桌面端同样要过登录（启动时自动打开浏览器 → 跳转 `/login`）
- 首次使用：检测 `core_users` 为空 → GET `/login` 显示"设定初始密码"

### 认证中间件

FastAPI middleware，在 `_inject_core_context` 之前执行。白名单路径：
- `/login` — 登录页（GET + POST）
- `/static/*` — 静态资源
- `/thinker/static/*`、`/ideator/static/*` — 模块静态资源

其余全部路径需认证。手机和桌面端使用完全相同的认证逻辑。

## 路由 & 模板策略

### 策略：混合模式

- 大部分页面（dashboard、列表）：同一套模板 + 响应式 CSS
- 差异大的页面（论文详情、thinker）：根据 User-Agent 切换模板

### User-Agent 检测

在认证中间件中设置 `request.state.is_mobile`：

```python
MOBILE_UA_PATTERNS = [
    "Android", "iPhone", "iPad", "iPod", "Mobile", "mobile"
]
# 匹配任一 → request.state.is_mobile = True
```

Jinja2 模板中可用 `{{ request.state.is_mobile }}` 做条件渲染。不引入 `/m/` URL 前缀。

### 页面路由对照

| 页面 | 桌面端 | 手机端 |
|------|--------|--------|
| `/login` | 居中卡片（共用模板） | 居中卡片（共用） |
| `/projects/` | 响应式 grid（共用） | 响应式 grid（共用） |
| `/projects/:id` | 响应式（共用） | 响应式（共用） |
| `/sessions/` | 响应式（共用） | 响应式（共用） |
| `/sessions/:id` | 响应式，含 PDF tab | PDF tab 隐藏，仅显示摘要 |
| `/papers/:id` | split-view（PDF + markdown） | `is_mobile` → 纯 markdown 堆叠 + 笔记 |
| `/thinker/` | 不适用（浮动侧边栏） | 全屏独立页面 |
| `/ideator/` | 火花网格 + 详情面板 | 列表 + 详情占满（响应式） |
| `/ideator/rt/:id` | 圆桌全屏 | 圆桌全屏 + 触控适配 |

### Thinker 路由变更

新增 `/thinker/` 页面路由（对应 `modules/thinker/templates/fullscreen.html`）。桌面端访问 `/thinker/` 时重定向回 `/projects/` 并用 JS 打开侧边栏；手机端正常渲染全屏页。

## 手机端 UI

### 底部 Tab 导航

```
┌──────────────────┐
│                  │
│   页面内容区      │
│                  │
├──────────────────┤
│ 🏠│ 💬│ 💡│ ⚙ │
│ 首页│Thinker│Ideator│设置
└──────────────────┘
```

- `position: fixed; bottom: 0`，高度 56px
- 首页 = Dashboard（`/projects/`）
- Thinker = `/thinker/`
- Ideator = `/ideator/`
- 设置 = `/settings/`（修改密码、查看连接状态）
- 通过 `request.state.is_mobile` 注入，桌面端不显示

### 关键页面手机适配

**论文详情（`/papers/:id`）**：
- 顶部：返回箭头 + 论文标题（sticky header）
- 正文：markdown 全宽渲染，无 PDF 面板
- 底部：笔记编辑区（textarea），保存按钮

**论文列表（`/sessions/:id`）**：
- 顶部：session 信息 + 进度条
- 正文：单列卡片流（无 PDF tab）
- SSE 进度条 + 5 秒轮询兜底

**Thinker（`/thinker/`）**：
- 全屏页面，`height: 100dvh`
- 消息区占满上方，输入区固定在底部
- 录音按钮放大（移动端语音输入场景多）
- 关联笔记隐藏在折叠面板中

**Ideator（`/ideator/`）**：
- 火花列表（单列），点击展开详情
- 详情模态填满全屏
- 圆桌页面：消息气泡 + 输入框贴底

## CSS 响应式策略

### 全局规则（`app.css` 新增）

```css
@media (max-width: 768px) {
  .split-view { flex-direction: column; }
  .paper-grid { grid-template-columns: 1fr; }
  /* 隐藏桌面专属元素 */
  .desktop-only { display: none !important; }
  /* 显示移动专属元素 */
  .mobile-only { display: block; }
}

@media (min-width: 769px) {
  .mobile-only { display: none !important; }
  .desktop-only { display: block; }
}
```

### 新增组件样式

- `.mobile-nav` — 底部固定 tab 栏（`position: fixed; bottom: 0; height: 56px; z-index: 9000`）
- `.mobile-tab-item` — tab 图标 + 文字（flex column, 居中）
- `.mobile-page` — 带底部 padding 的页面容器（`padding-bottom: 56px`）
- `.mobile-header` — 简化顶栏（返回箭头 + 标题）
- Thinker 全屏面板覆盖桌面 `.thinker-panel` 的 `position: fixed` 为 `position: relative`

### 字体 & 触控

- 正文 `font-size: 15px`，输入框 `font-size: 16px`（防 iOS 缩放）
- 按钮最小触摸区域 `44x44px`
- 间距适当收紧（`p-4` → `p-3`，减小留白）
- 颜色/字体家族沿用桌面端，不做独立设计

## 数据层

### 原则

数据库全程在电脑端。手机浏览器通过 HTTP API 读写，不做任何数据同步、离线存储或数据库分离。

```
手机浏览器 ──HTTP──▶ FastAPI ──SQLite──▶ paperreadagent.db (电脑本地)
```

### 新增数据结构

仅 `core_users` + `core_login_attempts` 两张表（见上文），属于 core schema 迁移。不影响任何现有数据表。

## 范围

### V2 包含

- 网络配置（`config.yaml` → `server.host` / `server.port`）
- 密码认证 + 防暴力破解（`core_users` + `core_login_attempts`）
- 移动端响应式 CSS（`@media` 断点，`.mobile-nav`，`.mobile-only`/`.desktop-only`）
- 独立移动模板（论文详情纯 markdown、thinker 全屏页面）
- 手机底部 tab 导航（通过 `is_mobile` 注入）
- SSE 进度 + 5 秒轮询兜底（`/sessions/:id` 页面）
- 设置页（改密码、连接状态）

### V2 不做

- 手机端显示 PDF
- 多用户 / 注册系统
- PWA / Service Worker / 离线缓存
- 数据库分离或同步
- 更换前端框架
- 公网反向代理 / DDNS / 穿透（交给 Tailscale）
- CDN 离线化（依赖仍走 CDN，首次加载需网络）

## 实现阶段

| 阶段 | 内容 | 预估 |
|------|------|------|
| Phase 1 | 网络配置 + 认证系统（中间件、登录页、登录逻辑） | 核心 |
| Phase 2 | 响应式 CSS + 底部导航 + 移动端适配 | 核心 |
| Phase 3 | thinker 全屏页 + ideator 适配 | 核心 |
| Phase 4 | 论文详情移动模板 + SSE 兜底 | 核心 |
| Phase 5 | 设置页 + 封禁管理 + 收尾 | 收尾 |

---

## 待定

- 暂无。所有关键决策已在上文确认。
