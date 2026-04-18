"""
doubao2API — 豆包网页版逆向 API 网关

基于 BrowserOnly 架构：Playwright Chromium + 字节 JS 拦截器自动注入 a_bogus/msToken
"""

import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Windows UTF-8 输出修复
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 将项目根目录加入 sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.core.config import settings
from backend.core.database import AsyncJsonDB
from backend.core.browser_engine import BrowserEngine
from backend.core.account_pool import AccountPool
from backend.services.doubao_client import DoubaoClient
from backend.api import admin, v1_chat, probes

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("doubao2api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting doubao2API — BrowserOnly Gateway...")

    # 初始化数据库
    app.state.accounts_db = AsyncJsonDB(settings.ACCOUNTS_FILE, default_data=[])
    app.state.users_db = AsyncJsonDB(settings.USERS_FILE, default_data=[])
    app.state.sessions_db = AsyncJsonDB(settings.SESSIONS_FILE, default_data=[])

    # 初始化浏览器引擎（BrowserOnly — 仅此一种模式）
    browser_engine = BrowserEngine(pool_size=settings.BROWSER_POOL_SIZE)
    app.state.browser_engine = browser_engine

    # 初始化账号池
    account_pool = AccountPool(app.state.accounts_db, max_inflight=settings.MAX_INFLIGHT_PER_ACCOUNT)
    app.state.account_pool = account_pool

    # 初始化客户端
    doubao_client = DoubaoClient(browser_engine, account_pool)
    app.state.doubao_client = doubao_client

    # 加载账号 + 启动引擎
    await account_pool.load()
    await browser_engine.start()

    if not browser_engine._started:
        log.error(
            "⚠️ 浏览器引擎启动失败！请确认 Playwright Chromium 已安装：\n"
            "  python -m playwright install chromium\n"
            "服务仍将启动，但所有请求将返回错误。"
        )

    log.info(
        f"doubao2API ready: accounts={len(account_pool.accounts)}, "
        f"browser_pool={settings.BROWSER_POOL_SIZE}, "
        f"browser_started={browser_engine._started}, "
        f"port={settings.PORT}"
    )

    yield

    log.info("Shutting down doubao2API...")
    await browser_engine.stop()


app = FastAPI(
    title="doubao2API BrowserOnly Gateway",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载路由
app.include_router(v1_chat.router, tags=["OpenAI Compatible"])
app.include_router(probes.router, tags=["Probes"])
app.include_router(admin.router, prefix="/api/admin", tags=["Dashboard Admin"])


@app.get("/api", tags=["System"])
async def root():
    return {
        "status": "doubao2API BrowserOnly Gateway is running",
        "docs": "/docs",
        "version": "1.0.0",
        "engine": "browser (Playwright + Chromium)",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=settings.PORT, workers=1)
