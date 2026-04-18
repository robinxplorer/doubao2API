# doubao2API 完整使用指南

> 豆包网页版逆向 API 网关 — BrowserOnly 架构
> 
> 基于 Playwright + Chromium，字节 JS 拦截器自动注入 a_bogus/msToken

---

## 目录

1. [架构概览](#1-架构概览)
2. [环境准备](#2-环境准备)
3. [安装与启动](#3-安装与启动)
4. [获取豆包 sessionid](#4-获取豆包-sessionid)
5. [管理后台操作](#5-管理后台操作)
6. [OpenAI 兼容 API 调用](#6-openai-兼容-api-调用)
7. [Docker 部署](#7-docker-部署)
8. [配置参考](#8-配置参考)
9. [项目结构](#9-项目结构)
10. [常见问题](#10-常见问题)

---

## 1. 架构概览

```
┌──────────────┐     ┌──────────────────────────────────────────┐
│  客户端       │     │  doubao2API (FastAPI, 端口 7861)        │
│  (curl/SDK)  │────→│                                          │
│              │←────│  /v1/chat/completions (OpenAI 兼容)      │
└──────────────┘     │                                          │
                     │  ┌─ DoubaoClient ──────────────────────┐ │
                     │  │  AccountPool → BrowserEngine        │ │
                     │  │      ↓                ↓             │ │
                     │  │  SessionStore   Playwright+Chromium │ │
                     │  │      ↓                ↓             │ │
                     │  │  build_payload  JS fetch(浏览器内)  │ │
                     │  │                       ↓             │ │
                     │  │              字节JS自动注入          │ │
                     │  │              a_bogus/msToken        │ │
                     │  │                       ↓             │ │
                     │  │              doubao.com SSE 流      │ │
                     │  │                       ↓             │ │
                     │  │              DoubaoSSEParser        │ │
                     │  └─────────────────────────────────────┘ │
                     └──────────────────────────────────────────┘
```

**核心原理**：豆包前端的 `window.fetch` 已被字节 JS 劫持，在浏览器内调用 `fetch()` 时，`a_bogus` 和 `msToken` 会被自动注入到 URL query 参数中。因此只需在 Playwright 打开的浏览器页面中执行 fetch，无需自己实现反爬签名。

---

## 2. 环境准备

| 依赖 | 最低版本 | 说明 |
|------|---------|------|
| Python | 3.10+ | 推荐 3.12 |
| Playwright | latest | 自动安装 Chromium |
| 豆包账号 | - | 需要一个已登录的 sessionid |

---

## 3. 安装与启动

### 方式一：一键启动（推荐）

```bash
cd doubao2API
python start.py
```

`start.py` 会自动完成：
1. 安装 Python 依赖 (`fastapi`, `uvicorn`, `pydantic-settings`, `playwright`)
2. 下载 Playwright Chromium 浏览器（首次运行）
3. 启动后端服务在 `http://127.0.0.1:7861`

### 方式二：手动启动

```bash
cd doubao2API

# 1. 安装依赖
pip install -r backend/requirements.txt

# 2. 安装 Playwright Chromium（首次）
python -m playwright install chromium

# 3. 启动服务
PYTHONPATH=. python -m uvicorn backend.main:app --host 0.0.0.0 --port 7861
```

### 启动成功标志

```
==================================================
  doubao2API BrowserOnly Gateway 已上线
  后端 API:     http://127.0.0.1:7861
  API 文档:     http://127.0.0.1:7861/docs
==================================================
```

> ⚠️ 首次启动需要等待 Chromium 下载和浏览器页面初始化，约 30-60 秒。

---

## 4. 获取豆包 sessionid

doubao2API 使用豆包的 Cookie `sessionid` 进行认证，**不是**邮箱/密码/Token。

### 步骤

1. 打开浏览器访问 [https://www.doubao.com](https://www.doubao.com) 并登录
2. 按 `F12` 打开开发者工具
3. 切换到 **Application** (应用程序) 标签页
4. 左侧找到 **Cookies** → `https://www.doubao.com`
5. 在 Cookie 列表中找到 `sessionid`，复制其 **Value**

```
示例 sessionid: 64b2be47...

64b2be47...
```

> ⚠️ sessionid 是敏感信息，请勿泄露给他人。Cookie 有效期约 30 天（见 `sid_guard` 中的过期时间）。

---

## 5. 管理后台操作

所有管理接口需要 `Authorization: Bearer <ADMIN_KEY>` 认证，默认 ADMIN_KEY 为 `admin`。

### 5.1 添加账号

```bash
curl -X POST http://127.0.0.1:7861/api/admin/accounts/add \
  -H "Authorization: Bearer admin" \
  -H "Content-Type: application/json" \
  -d '{"sessionid": "64b2be47...", "name": "doubao_rx"}'
```

响应：
```json
{"status": "ok", "name": "我的豆包账号"}
```

### 5.2 查看账号列表

```bash
curl http://127.0.0.1:7861/api/admin/accounts \
  -H "Authorization: Bearer admin"
```

响应：
```json
{
  "accounts": [
    {
      "sessionid": "64b2be47...",
      "name": "我的豆包账号",
      "status": "valid",
      "status_text": "正常",
      "inflight": 0,
      "consecutive_failures": 0,
      "last_error": ""
    }
  ]
}
```

### 5.3 删除账号

```bash
curl -X POST http://127.0.0.1:7861/api/admin/accounts/remove \
  -H "Authorization: Bearer admin" \
  -H "Content-Type: application/json" \
  -d '{"sessionid": "要删除的sessionid"}'
```

### 5.4 添加 API Key（可选）

如果设置了 API Key，则只有持有有效 Key 的请求才能调用 API：

```bash
curl -X POST http://127.0.0.1:7861/api/admin/apikeys/add \
  -H "Authorization: Bearer admin" \
  -H "Content-Type: application/json" \
  -d '{"key": "sk-doubao-mykey123"}'
```

### 5.5 查看系统状态

```bash
curl http://127.0.0.1:7861/api/admin/status \
  -H "Authorization: Bearer admin"
```

### 5.6 调整并发数

```bash
curl -X POST http://127.0.0.1:7861/api/admin/max_inflight \
  -H "Authorization: Bearer admin" \
  -H "Content-Type: application/json" \
  -d '{"value": 2}'
```

---

## 6. OpenAI 兼容 API 调用

doubao2API 提供与 OpenAI 完全兼容的 `/v1/chat/completions` 接口。

### 6.1 非流式请求

```bash
curl http://127.0.0.1:7861/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer admin" \
  -d '{
    "model": "doubao",
    "messages": [{"role": "user", "content": "你好，介绍一下你自己"}],
    "stream": false
  }'
```

响应（与 OpenAI 格式一致）：
```json
{
  "id": "chatcmpl-a1b2c3d4e5f6",
  "object": "chat.completion",
  "created": 1775994514,
  "model": "doubao",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "你好！我是豆包..."
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 9,
    "completion_tokens": 150,
    "total_tokens": 159
  }
}
```

### 6.2 流式请求

```bash
curl http://127.0.0.1:7861/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer admin" \
  -d '{
    "model": "doubao",
    "messages": [{"role": "user", "content": "写一首关于春天的诗"}],
    "stream": true
  }'
```

响应（SSE 格式）：
```
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":...,"model":"doubao","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":...,"model":"doubao","choices":[{"index":0,"delta":{"content":"春"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":...,"model":"doubao","choices":[{"index":0,"delta":{"content":"风"},"finish_reason":null}]}

data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","created":...,"model":"doubao","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

### 6.3 文生图请求

当用户消息包含画图/生成图片意图时，自动路由到豆包文生图 Agent：

```bash
curl http://127.0.0.1:7861/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer admin" \
  -d '{
    "model": "doubao",
    "messages": [{"role": "user", "content": "画一只可爱的猫咪"}],
    "stream": false
  }'
```

响应中 `content` 包含 Markdown 图片链接，`images` 字段包含原始 URL 列表：
```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "content": "![generated](https://lf-ai-img.bytetos.com/...)\n![generated](https://...)"
    }
  }],
  "images": ["https://lf-ai-img.bytetos.com/...", "https://..."]
}
```

### 6.4 模型名映射

doubao2API 支持多种模型名，均映射到豆包的 bot_id：

| 传入 model | 实际 bot_id | 说明 |
|-----------|------------|------|
| `doubao` | `7338286299411103781` | 默认模型 |
| `doubao-pro` | 同上 | 别名 |
| `gpt-4o` | 同上 | OpenAI 兼容 |
| `gpt-4o-mini` | 同上 | OpenAI 兼容 |
| `claude-3-5-sonnet` | 同上 | Anthropic 兼容 |
| `deepseek-chat` | 同上 | DeepSeek 兼容 |
| `7338286299411103781` | 直接使用 | 纯数字 bot_id |

> 目前所有模型名都映射到默认 bot_id。不同模型的 bot_id 可通过 F12 抓包获取后更新 `BOT_MAP`。

### 6.5 多轮对话

doubao2API 的多轮对话由豆包服务端管理——**客户端不需要发送历史消息**。每次请求只需传当前消息，服务端根据 `conversation_id` 自动加载上下文。

但如果你从 OpenAI SDK 发送带历史消息的请求，doubao2API 也兼容——它只提取**最后一条 user 消息**发送给豆包。

### 6.6 Python SDK 调用示例

```python
from openai import OpenAI

client = OpenAI(
    api_key="admin",  # ADMIN_KEY 或你添加的 API Key
    base_url="http://127.0.0.1:7861/v1"
)

# 非流式
response = client.chat.completions.create(
    model="doubao",
    messages=[{"role": "user", "content": "你好"}],
    stream=False
)
print(response.choices[0].message.content)

# 流式
stream = client.chat.completions.create(
    model="doubao",
    messages=[{"role": "user", "content": "讲个笑话"}],
    stream=True
)
for chunk in stream:
    if chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

### 6.7 鉴权方式

API 支持三种鉴权方式（任选其一）：

```bash
# 1. Authorization Header
-H "Authorization: Bearer admin"

# 2. X-API-Key Header
-H "x-api-key: admin"

# 3. Query Parameter
http://127.0.0.1:7861/v1/chat/completions?key=admin
```

如果未添加任何 API Key，则所有请求均可访问（仅验证 ADMIN_KEY）。

---

## 7. Docker 部署

### 7.1 构建并启动

```bash
cd doubao2API

# 构建镜像（首次较慢，需下载 Chromium）
docker compose up -d --build

# 查看日志
docker compose logs -f
```

### 7.2 添加账号

```bash
curl -X POST http://YOUR_SERVER:7861/api/admin/accounts/add \
  -H "Authorization: Bearer admin" \
  -H "Content-Type: application/json" \
  -d '{"sessionid": "你的sessionid", "name": "production-account"}'
```

### 7.3 数据持久化

`docker-compose.yml` 已配置 `./data` 目录挂载，账号和用户数据会持久保存。

### 7.4 自定义配置

创建 `.env` 文件：

```env
PORT=7861
ADMIN_KEY=your-secure-admin-key
BROWSER_POOL_SIZE=3
MAX_INFLIGHT=1
DEFAULT_BOT_ID=7338286299411103781
```

---

## 8. 配置参考

所有配置可通过环境变量或 `.env` 文件设置：

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `PORT` | `7861` | API 网关监听端口 |
| `ADMIN_KEY` | `admin` | 管理后台鉴权密钥 |
| `REGISTER_SECRET` | `""` | 注册密钥（暂未使用） |
| `ENGINE_MODE` | `browser` | 引擎模式（仅 browser） |
| `BROWSER_POOL_SIZE` | `2` | 浏览器页面池大小 |
| `MAX_INFLIGHT` | `1` | 每账号最大并发请求数 |
| `BASE_URL` | `https://www.doubao.com` | 豆包基础 URL |
| `DEFAULT_BOT_ID` | `7338286299411103781` | 默认模型 bot_id |
| `DEFAULT_FP` | `doubao2api_default_fp` | 设备指纹 |
| `ACCOUNT_MIN_INTERVAL_MS` | `1200` | 同账号最小请求间隔 (ms) |
| `REQUEST_JITTER_MIN_MS` | `120` | 请求抖动延迟下限 (ms) |
| `REQUEST_JITTER_MAX_MS` | `360` | 请求抖动延迟上限 (ms) |
| `RATE_LIMIT_BASE_COOLDOWN` | `600` | 限流基础冷却时间 (s) |
| `RATE_LIMIT_MAX_COOLDOWN` | `3600` | 限流最大冷却时间 (s) |
| `ACCOUNTS_FILE` | `data/accounts.json` | 账号数据文件路径 |
| `USERS_FILE` | `data/users.json` | 用户数据文件路径 |
| `SESSIONS_FILE` | `data/sessions.json` | 会话数据文件路径 |

---

## 9. 项目结构

```
doubao2API/
├── backend/
│   ├── __init__.py
│   ├── main.py                    # FastAPI 入口，lifespan 管理引擎+账号池+客户端
│   ├── requirements.txt           # fastapi, uvicorn, pydantic-settings, playwright
│   ├── api/
│   │   ├── __init__.py
│   │   ├── v1_chat.py             # /v1/chat/completions OpenAI兼容接口（流式+非流式+文生图）
│   │   ├── admin.py               # /api/admin 账号管理+API Key管理+系统状态
│   │   └── probes.py              # /healthz + /readyz 健康检查
│   ├── core/
│   │   ├── __init__.py
│   │   ├── config.py              # Settings + BOT_MAP + resolve_bot_id() + API_KEYS
│   │   ├── database.py            # AsyncJsonDB
│   │   ├── browser_engine.py      # Playwright+Chromium + JS_STREAM_DOUBAO + fetch_chat()
│   │   └── account_pool.py        # Account(sessionid) + AccountPool
│   └── services/
│       ├── __init__.py
│       ├── doubao_client.py       # DoubaoClient: 串联引擎+账号池+SSE解析，支持chat/chat_stream/with_retry
│       ├── sse_parser.py          # DoubaoSSEParser: 7种SSE事件解析→StreamResult
│       └── session_store.py       # SessionStore: 会话状态+build_full_payload()
├── data/
│   ├── accounts.json
│   ├── users.json
│   ├── sessions.json
│   └── api_keys.json
├── start.py                       # 一键启动脚本
├── Dockerfile                     # Python 3.12 + Playwright Chromium
├── docker-compose.yml             # 端口7861
└── .env.example
```

### 数据流

```
用户请求 → v1_chat.py → DoubaoClient.chat_stream()
                          ├─ AccountPool.acquire() → 获取可用账号
                          ├─ SessionStore.build_full_payload() → 构建豆包请求体
                          ├─ BrowserEngine.fetch_chat() → 浏览器内 fetch
                          │   └─ JS_STREAM_DOUBAO → 字节JS自动注入签名 → SSE 流
                          └─ DoubaoSSEParser._stream_sse() → 逐事件解析
                              └─ yield delta/image/error/done → OpenAI 格式 SSE
```

---

## 10. 常见问题

### Q: 启动时 Playwright Chromium 下载失败？

**A:** 设置镜像源后重试：
```bash
set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
python -m playwright install chromium
```

### Q: 请求返回 "No available accounts"？

**A:** 需要先通过管理后台添加豆包 sessionid：
```bash
curl -X POST http://127.0.0.1:7861/api/admin/accounts/add \
  -H "Authorization: Bearer admin" \
  -H "Content-Type: application/json" \
  -d '{"sessionid": "你的sessionid"}'
```

### Q: sessionid 过期了怎么办？

**A:** 豆包 sessionid 有效期约 30 天。过期后需要重新从浏览器获取新的 sessionid，然后：
1. 通过 `/api/admin/accounts/remove` 删除旧账号
2. 通过 `/api/admin/accounts/add` 添加新 sessionid

### Q: 为什么请求很慢？

**A:** doubao2API 使用浏览器模式，每次请求需要：
1. 注入 Cookie → 刷新页面 → 等待 JS 加载
2. 请求间有抖动延迟（120-360ms，防风控）

首次请求可能需要 5-10 秒，后续请求通常 2-5 秒。可通过以下方式优化：
- 增大 `BROWSER_POOL_SIZE`（更多浏览器页面并行）
- 减小 `ACCOUNT_MIN_INTERVAL_MS`（缩短请求间隔，但有风控风险）

### Q: 如何获取不同模型的 bot_id？

**A:** 在豆包网页版切换到目标模型，然后 F12 抓包查看 `/chat/completion` 请求体中的 `client_meta.bot_id` 字段。获取后在 `config.py` 的 `BOT_MAP` 中添加映射。

### Q: 支持多账号负载均衡吗？

**A:** 支持。添加多个 sessionid 后，AccountPool 会自动轮询分配，并有以下策略：
- 账号间轮询（优先选 inflight 最少的）
- 请求间隔抖动（防风控）
- 限流自动冷却（指数退避）
- 失效账号自动标记跳过

### Q: Cookie 中需要哪些字段？

**A:** 只需要 `sessionid`。doubao2API 会自动同时注入 `sessionid` 和 `sessionid_ss`（豆包双重验证）。

### Q: 支持 API Key 鉴权吗？

**A:** 支持。通过 `/api/admin/apikeys/add` 添加 API Key 后，请求必须携带有效 Key。支持三种方式：
1. `Authorization: Bearer <key>`
2. `x-api-key: <key>`
3. URL query `?key=<key>`

### Q: 和 qwen2API 的区别？

| 特性 | qwen2API | doubao2API |
|------|----------|------------|
| 端口 | 7860 | 7861 |
| 引擎 | Camoufox (Firefox) | Playwright (Chromium) |
| 认证 | JWT Bearer Token | Cookie sessionid |
| 模型标识 | model name | bot_id (数字) |
| 反爬方式 | 无 a_bogus | a_bogus (字节JS自动注入) |
| 消息格式 | 纯文本 content | content_block 嵌套结构 |
| 新会话 | 两步创建 | 一步创建+发消息 |
