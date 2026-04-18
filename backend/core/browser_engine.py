"""
doubao2api BrowserEngine — Playwright + Chromium

核心原理：豆包的 a_bogus / msToken 由字节跳动前端 JS 拦截器在浏览器内
自动注入到 fetch 请求的 URL query 中，因此：
  1. JS 脚本无需手动生成 a_bogus / msToken
  2. 必须在浏览器环境中执行 fetch，字节 JS 会 hook window.fetch
  3. 使用 Playwright Chromium（兼容字节 JS 拦截器）

关键设计：**先注入 Cookie → 再导航页面**
  - 页面在加载时即以登录态初始化，字节 JS 拦截器以正确状态运行
  - 避免"先加载游客态 → 再注入 Cookie → 再刷新"的不可靠流程
"""

import asyncio
import logging
import random
import uuid
from backend.core.config import settings

log = logging.getLogger("doubao2api.browser")


def _request_jitter_seconds() -> float:
    low = max(0, settings.REQUEST_JITTER_MIN_MS)
    high = max(low, settings.REQUEST_JITTER_MAX_MS)
    return random.uniform(low, high) / 1000.0


def _generate_trace_id() -> str:
    """生成 x-flow-trace Header 所需的 trace ID。"""
    return uuid.uuid4().hex


# ── 浏览器内 fetch 脚本 ────────────────────────────────────
# 豆包 SSE 流式请求
# - 不带 a_bogus / msToken，字节 JS 拦截器自动注入
# - credentials: 'include' 携带 Cookie
# - 完整收取 SSE body 后一次性返回（与 qwen2API 的 JS_STREAM_FULL 策略一致）

JS_STREAM_DOUBAO = (
    "async (args) => {"
    # 检测 fetch 是否被字节 JS 劫持
    "const fetchStr=window.fetch.toString().substring(0,80);"
    # 构造请求 Headers
    "const headers={"
    "'Content-Type':'application/json',"
    "'Agw-Js-Conv':'str',"
    "'x-flow-trace':args.trace_id,"
    "'last-event-id':'undefined'"
    "};"
    # 构造 fetch 选项
    "const opts={"
    "method:'POST',"
    "headers:headers,"
    "body:args.body,"
    "credentials:'include',"
    "signal:AbortSignal.timeout(1800000)"
    "};"
    "try{"
    "const res=await fetch(args.url,opts);"
    "if(!res.ok){"
    "const t=await res.text();"
    "return{status:res.status,body:t.substring(0,2000),fetch_hook:fetchStr};}"
    # 读取完整 SSE body
    "const rdr=res.body.getReader();"
    "const dec=new TextDecoder();"
    "let body='';"
    "while(true){"
    "const{done,value}=await rdr.read();"
    "if(done)break;"
    "body+=dec.decode(value,{stream:true});}"
    "return{status:res.status,body:body,fetch_hook:fetchStr,body_len:body.length};"
    "}catch(e){"
    "return{status:0,body:'JS error: '+e.message,fetch_hook:fetchStr};"
    "}}"
)


class BrowserEngine:
    """Playwright Chromium 浏览器引擎，管理页面池并执行豆包 SSE 请求。"""

    def __init__(
        self,
        pool_size: int = None,
        base_url: str = None,
    ):
        self.pool_size = pool_size or settings.BROWSER_POOL_SIZE
        self.base_url = base_url or settings.BASE_URL
        self._browser = None
        self._playwright = None
        self._pages: asyncio.Queue = asyncio.Queue()
        self._started = False
        self._ready = asyncio.Event()
        # 跟踪每个 context 的 sessionid，用于检测 Cookie 变化
        self._context_sessionid: dict[int, str] = {}  # context id → sessionid

    # ── 生命周期 ──────────────────────────────────────────

    async def start(self):
        """启动 Playwright Chromium 并初始化页面池。"""
        if self._started:
            return
        try:
            await self._start_playwright()
        except Exception as e:
            log.error(f"[Browser] Playwright Chromium 启动失败: {e}")
            log.error("[Browser] 请确认已运行: python -m playwright install chromium")
        finally:
            self._ready.set()

    async def _start_playwright(self):
        from playwright.async_api import async_playwright

        log.info("Starting browser engine (Playwright Chromium)...")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-gpu",
                "--lang=zh-CN",
            ],
        )
        # 只创建空的 context+page，不加载页面
        # 页面导航在 fetch_chat 中按需执行（先注入 Cookie 再导航）
        await self._init_pages()
        self._started = True
        log.info(f"Browser engine started, pool_size={self.pool_size}")

    async def _init_pages(self):
        """创建页面池：每个 page 绑定独立的 BrowserContext（Cookie 隔离）。

        只创建空的 context+page，不导航。Cookie 注入和页面导航在
        fetch_chat 中按需执行，确保页面以登录态加载。
        """
        log.info(f"[Browser] 正在初始化 {self.pool_size} 个并发页面...")
        for i in range(self.pool_size):
            context = await self._browser.new_context(
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            # 队列元素：(page, context) 元组，context 用于 Cookie 注入
            await self._pages.put((page, context))
            log.info(f"  [Browser] Page {i+1}/{self.pool_size} ready (idle, no navigation yet)")

    async def stop(self):
        """关闭浏览器引擎。"""
        self._started = False
        # 关闭所有 context 和 page
        while not self._pages.empty():
            try:
                page, context = self._pages.get_nowait()
                await context.close()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass

    # ── Cookie 注入 ──────────────────────────────────────

    async def inject_session(self, context, sessionid: str):
        """向 BrowserContext 注入豆包 sessionid Cookie。

        豆包使用 Cookie sessionid 认证，无需 Bearer Token。
        注入后所有该 context 下的 fetch 请求会自动携带 Cookie。
        
        注入的 Cookie 列表：
        - sessionid: 主认证 Cookie
        - sessionid_ss: sessionid 的 ss 变体
        - sid_tt: sessionid 的 tt 变体（字节跳动统一认证）
        - sid_guard: sessionid 的守护 Cookie
        """
        cookies = [
            {
                "name": "sessionid",
                "value": sessionid,
                "domain": ".doubao.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
            {
                "name": "sessionid_ss",
                "value": sessionid,
                "domain": ".doubao.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
            {
                "name": "sid_tt",
                "value": sessionid,
                "domain": ".doubao.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
            {
                "name": "sid_guard",
                "value": sessionid,
                "domain": ".doubao.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            },
        ]
        await context.add_cookies(cookies)
        log.debug(f"[Browser] Cookie sessionid 注入完成 ({len(cookies)} 个)")

    # ── SSE 流式请求 ─────────────────────────────────────

    async def fetch_chat(
        self,
        sessionid: str,
        request_body: str,
        conversation_id: str = "",
    ):
        """执行豆包 SSE 流式请求，异步生成器返回完整 SSE body。

        流程：
        1. 从页面池获取 (page, context)
        2. 检查是否需要重新导航（Cookie 变化或首次请求）
        3. 先注入 Cookie → 再导航页面（确保以登录态加载）
        4. 执行 JS_STREAM_DOUBAO 在浏览器内 fetch
        5. 返回 {status, body} 字典

        Args:
            sessionid: 豆包账号的 sessionid
            request_body: JSON 字符串形式的请求体
            conversation_id: 会话 ID（用于日志）
        """
        await asyncio.wait_for(self._ready.wait(), timeout=300)
        if not self._started:
            yield {"status": 0, "body": "Browser engine not started. Run: python -m playwright install chromium"}
            return

        try:
            page, context = await asyncio.wait_for(self._pages.get(), timeout=60)
        except asyncio.TimeoutError:
            yield {"status": 429, "body": "Too Many Requests (Queue full)"}
            return

        needs_refresh = False
        trace_id = _generate_trace_id()

        try:
            ctx_id = id(context)
            prev_sessionid = self._context_sessionid.get(ctx_id, "")
            
            # 判断是否需要（重新）导航页面
            # 1. 首次请求（prev_sessionid 为空）
            # 2. Cookie 变化（不同账号）
            need_navigate = (prev_sessionid != sessionid)

            if need_navigate:
                # 先注入 Cookie，再导航页面
                # 这样页面加载时字节 JS 拦截器在登录态下正确初始化
                await self.inject_session(context, sessionid)
                self._context_sessionid[ctx_id] = sessionid

                log.info(f"[Browser] {'首次' if not prev_sessionid else 'Cookie 变化，'}导航页面 (sessionid={sessionid[:8]}...)")
                try:
                    await page.goto(self.base_url, wait_until="networkidle", timeout=30000)
                    # 额外等待，确保字节 JS 拦截器完全加载
                    await asyncio.sleep(2)
                except Exception:
                    log.warning("[Browser] networkidle 超时，尝试 domcontentloaded")
                    try:
                        await page.goto(self.base_url, wait_until="domcontentloaded", timeout=15000)
                        await asyncio.sleep(1.5)
                    except Exception:
                        log.warning("[Browser] 页面导航超时")

                # 验证 fetch 是否被劫持
                try:
                    fetch_str = await page.evaluate("() => window.fetch.toString().substring(0, 80)")
                    is_hooked = "native code" not in fetch_str
                    log.info(f"[Browser] fetch hooked={is_hooked}, fetch={fetch_str[:60]}")
                except Exception:
                    pass
            else:
                # 同一账号，Cookie 已存在，无需重新导航
                log.info(f"[Browser] 复用已有页面 (sessionid={sessionid[:8]}...)")

            # 抖动延迟
            await asyncio.sleep(_request_jitter_seconds())

            # 在浏览器内执行 fetch（字节 JS 拦截器自动注入 a_bogus / msToken）
            url = f"{self.base_url}/chat/completion"
            # 诊断：打印请求体的关键字段
            try:
                import json as _json
                body_obj = _json.loads(request_body)
                cm = body_obj.get("client_meta", {})
                log.info(
                    f"[Browser] 请求体诊断: conversation_id={cm.get('conversation_id', 'N/A')!r}, "
                    f"local_conversation_id={cm.get('local_conversation_id', 'N/A')}, "
                    f"bot_id={cm.get('bot_id', 'N/A')}, "
                    f"need_create_conversation={body_obj.get('option', {}).get('need_create_conversation', 'N/A')}"
                )
            except Exception:
                pass
            res = await asyncio.wait_for(
                page.evaluate(
                    JS_STREAM_DOUBAO,
                    {
                        "url": url,
                        "body": request_body,
                        "trace_id": trace_id,
                    },
                ),
                timeout=1800,
            )

            if isinstance(res, dict) and res.get("status") == 0:
                needs_refresh = True

            # 诊断日志
            if isinstance(res, dict):
                hook = res.get("fetch_hook", "unknown")
                body_len = res.get("body_len", len(res.get("body", "")))
                log.info(
                    f"[Browser] fetch result: status={res.get('status')}, "
                    f"body_len={body_len}, fetch_hook={hook[:60]}"
                )

            yield res if isinstance(res, dict) else {"status": 0, "body": str(res)}

        except asyncio.TimeoutError:
            needs_refresh = True
            yield {"status": 0, "body": "Timeout"}
        except Exception as e:
            needs_refresh = True
            log.error(f"[Browser] fetch_chat 异常: {e}")
            yield {"status": 0, "body": str(e)}
        finally:
            if needs_refresh:
                asyncio.create_task(self._refresh_page_and_return(page, context))
            else:
                await self._pages.put((page, context))

    # ── 页面刷新与回收 ───────────────────────────────────

    async def _refresh_page(self, page, context):
        """刷新页面，恢复字节 JS 拦截器上下文。"""
        try:
            await asyncio.wait_for(
                page.goto(self.base_url, wait_until="domcontentloaded"),
                timeout=20000,
            )
        except Exception:
            log.warning("[Browser] 页面刷新超时")

    async def _refresh_page_and_return(self, page, context):
        """刷新页面并放回页面池。"""
        await self._refresh_page(page, context)
        await self._pages.put((page, context))
