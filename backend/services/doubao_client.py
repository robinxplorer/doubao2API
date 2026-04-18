"""
doubao2api DoubaoClient — 核心聊天客户端

负责：
  1. 从 AccountPool 获取可用账号
  2. 通过 SessionStore 构建请求 payload
  3. 通过 BrowserEngine 执行浏览器内 fetch
  4. 通过 DoubaoSSEParser 解析 SSE 响应
  5. 支持 OpenAI 兼容的流式/非流式输出
"""

import asyncio
import json
import logging
import time
from typing import AsyncGenerator, Optional

from backend.core.browser_engine import BrowserEngine
from backend.core.account_pool import Account, AccountPool
from backend.core.config import settings
from backend.services.sse_parser import DoubaoSSEParser, StreamResult
from backend.services.session_store import SessionStore

log = logging.getLogger("doubao2api.client")


class DoubaoClient:
    """豆包聊天客户端，串联引擎+账号池+会话管理+SSE 解析。"""

    def __init__(self, engine: BrowserEngine, account_pool: AccountPool):
        self.engine = engine
        self.account_pool = account_pool
        self.session_store = SessionStore()

    async def chat(
        self,
        text: str,
        bot_id: str = "",
        conversation_id: str = "",
        exclude_accounts: set = None,
    ) -> tuple[StreamResult, Optional[Account], str]:
        """非流式聊天，返回 (StreamResult, Account, session_id)。

        内部会自动管理会话状态：新会话创建、SSE 解析后更新。
        """
        bot_id = bot_id or settings.DEFAULT_BOT_ID
        acc = await self.account_pool.acquire_wait(timeout=60, exclude=exclude_accounts)
        if not acc:
            return StreamResult(error="No available accounts"), None, ""

        # 创建或恢复会话
        session = self.session_store.create_session(
            bot_id=bot_id,
            conversation_id=conversation_id,
        )

        try:
            # 构建请求 payload
            payload = self.session_store.build_full_payload(session.session_id, text)
            body_str = json.dumps(payload, ensure_ascii=False)

            log.info(
                f"[Client] chat request: session={session.session_id[:8]}... "
                f"bot_id={bot_id} is_new={not conversation_id} "
                f"account={acc.name}"
            )

            # 通过浏览器引擎执行请求
            result = None
            async for res in self.engine.fetch_chat(
                sessionid=acc.sessionid,
                request_body=body_str,
                conversation_id=conversation_id,
            ):
                result = res
                break  # fetch_chat yield 一次

            if not result:
                self.account_pool.release(acc)
                return StreamResult(error="Empty response from engine"), acc, session.session_id

            if result.get("status", 0) != 200:
                error_body = result.get("body", "Unknown error")
                # 检测账号失效
                if "session" in str(error_body).lower() and "expired" in str(error_body).lower():
                    self.account_pool.mark_invalid(acc, reason="session_expired", error_message=error_body[:200])
                elif "rate" in str(error_body).lower() or "limit" in str(error_body).lower():
                    self.account_pool.mark_rate_limited(acc, error_message=error_body[:200])
                else:
                    # 非账号问题的上游错误（如浏览器引擎未启动），不标记 invalid
                    log.warning(f"[Client] upstream error (not marking invalid): {error_body[:200]}")
                self.account_pool.release(acc)
                return StreamResult(error=f"HTTP {result.get('status')}: {error_body[:500]}"), acc, session.session_id

            # 解析 SSE 响应
            raw_body = result.get("body", "")
            log.info(f"[Client] raw_body length={len(raw_body)}, preview={raw_body[:300]!r}")
            parser = DoubaoSSEParser()
            stream_result = parser.parse_raw_sse(raw_body)
            log.info(
                f"[Client] SSE parsed: text_len={len(stream_result.text)}, "
                f"error={stream_result.error}, "
                f"conv_id={stream_result.session_meta.conversation_id[:12] if stream_result.session_meta.conversation_id else 'N/A'}"
            )

            if not stream_result.text and not stream_result.error:
                log.warning(
                    f"[Client] ⚠️ SSE 解析成功但文本为空！raw_body 前 500 字符:\n{raw_body[:500]}"
                )

            if stream_result.error:
                # 检测常见错误码
                err = stream_result.error
                if "conversation id is 0" in err:
                    log.error(f"[Client] conversation id is 0 — 可能缺少 local_conversation_id")
                elif "invalid param" in err:
                    log.error(f"[Client] invalid param — 请求体格式可能有误")
                # SSE 解析错误可能是请求格式问题，不一定是账号问题
                log.warning(f"[Client] SSE parse error (not marking invalid): {err[:200]}")
                self.account_pool.release(acc)
                return stream_result, acc, session.session_id

            # 更新会话状态
            meta = stream_result.session_meta
            self.session_store.update_from_sse(
                session.session_id,
                conversation_id=meta.conversation_id or None,
                section_id=meta.section_id or None,
                message_index=meta.last_message_index if meta.last_message_index else None,
            )
            self.session_store.increment_turn(session.session_id)

            # 标记账号成功 + 释放
            self.account_pool.mark_success(acc)
            self.account_pool.release(acc)

            log.info(
                f"[Client] chat complete: conv_id={meta.conversation_id[:12] if meta.conversation_id else 'N/A'}... "
                f"text_len={len(stream_result.text)} session={session.session_id[:8]}..."
            )

            return stream_result, acc, session.session_id

        except Exception as e:
            log.error(f"[Client] chat exception: {e}")
            self.account_pool.release(acc)
            return StreamResult(error=str(e)), acc, session.session_id

    async def chat_stream(
        self,
        text: str,
        bot_id: str = "",
        conversation_id: str = "",
        exclude_accounts: set = None,
    ) -> AsyncGenerator[dict, None]:
        """流式聊天，逐 chunk 返回事件字典。

        事件格式：
          {"type": "meta", "session_id": str, "acc": Account}
          {"type": "delta", "content": str}
          {"type": "image", "url": str}
          {"type": "suggestion", "items": list}
          {"type": "error", "message": str}
          {"type": "done"}
        """
        bot_id = bot_id or settings.DEFAULT_BOT_ID
        acc = await self.account_pool.acquire_wait(timeout=60, exclude=exclude_accounts)
        if not acc:
            yield {"type": "error", "message": "No available accounts"}
            return

        session = self.session_store.create_session(
            bot_id=bot_id,
            conversation_id=conversation_id,
        )

        yield {"type": "meta", "session_id": session.session_id, "acc": acc}

        try:
            payload = self.session_store.build_full_payload(session.session_id, text)
            body_str = json.dumps(payload, ensure_ascii=False)

            log.info(
                f"[Client] stream request: session={session.session_id[:8]}... "
                f"bot_id={bot_id} is_new={not conversation_id} "
                f"account={acc.name}"
            )

            # 通过浏览器引擎执行请求
            result = None
            async for res in self.engine.fetch_chat(
                sessionid=acc.sessionid,
                request_body=body_str,
                conversation_id=conversation_id,
            ):
                result = res
                break

            if not result:
                self.account_pool.release(acc)
                yield {"type": "error", "message": "Empty response from engine"}
                return

            if result.get("status", 0) != 200:
                error_body = result.get("body", "Unknown error")
                if "session" in str(error_body).lower() and "expired" in str(error_body).lower():
                    self.account_pool.mark_invalid(acc, reason="session_expired", error_message=error_body[:200])
                elif "rate" in str(error_body).lower() or "limit" in str(error_body).lower():
                    self.account_pool.mark_rate_limited(acc, error_message=error_body[:200])
                self.account_pool.release(acc)
                yield {"type": "error", "message": f"HTTP {result.get('status')}: {error_body[:500]}"}
                return

            # 逐行解析 SSE 事件，实时 yield delta
            raw_body = result.get("body", "")
            log.info(f"[Client-Stream] raw_body length={len(raw_body)}, preview={raw_body[:300]!r}")
            async for event in self._stream_sse(raw_body, session.session_id, acc):
                yield event

        except Exception as e:
            log.error(f"[Client] stream exception: {e}")
            self.account_pool.release(acc)
            yield {"type": "error", "message": str(e)}

    async def _stream_sse(self, raw_body: str, session_id: str, acc: Account):
        """逐事件解析 SSE 并 yield 增量文本。"""
        parser = DoubaoSSEParser()
        events = parser._split_sse_events(raw_body)

        full_text = ""
        session_meta_updated = False

        for evt in events:
            if evt.event_type == "SSE_HEARTBEAT":
                continue

            elif evt.event_type == "SSE_ACK":
                # 提取会话元数据
                meta = evt.data.get("ack_client_meta", {})
                conv_id = meta.get("conversation_id", "")
                section_id = meta.get("section_id", "")
                if conv_id or section_id:
                    self.session_store.update_from_sse(
                        session_id,
                        conversation_id=conv_id or None,
                        section_id=section_id or None,
                    )
                    session_meta_updated = True

            elif evt.event_type == "STREAM_MSG_NOTIFY":
                # 提取首个文本块
                meta = evt.data.get("meta", {})
                idx = meta.get("index_in_conv")
                if idx is not None:
                    self.session_store.update_from_sse(session_id, message_index=idx)

                content = evt.data.get("content", {})
                blocks = content.get("content_block", [])
                for block in blocks:
                    if block.get("block_type") == 10000:
                        text = block.get("content", {}).get("text_block", {}).get("text", "")
                        if text:
                            full_text += text
                            yield {"type": "delta", "content": text}
                        break

            elif evt.event_type == "CHUNK_DELTA":
                # 增量文本（最简洁路径）
                text = evt.data.get("text", "")
                if text:
                    full_text += text
                    yield {"type": "delta", "content": text}

            elif evt.event_type == "STREAM_CHUNK":
                # 文生图 + 建议 + 文本
                for op in evt.data.get("patch_op", []):
                    patch_object = op.get("patch_object", 0)
                    if patch_object == 1:
                        # content_block 更新
                        for block in op.get("patch_value", {}).get("content_block", []):
                            block_type = block.get("block_type", 0)
                            if block_type == 2074:
                                # 文生图
                                img_urls = self._extract_image_urls(block)
                                for url in img_urls:
                                    yield {"type": "image", "url": url}
                            elif block_type == 10000:
                                # 文本块增量
                                text = block.get("content", {}).get("text_block", {}).get("text", "")
                                if text:
                                    full_text += text
                                    yield {"type": "delta", "content": text}
                    elif patch_object == 102:
                        # 增量文本（游客模式 / 部分场景）
                        pv = op.get("patch_value", {})
                        content_str = pv.get("content", "")
                        if content_str:
                            try:
                                content_obj = json.loads(content_str) if isinstance(content_str, str) else content_str
                                text = content_obj.get("text", "")
                                if text:
                                    full_text += text
                                    yield {"type": "delta", "content": text}
                            except (json.JSONDecodeError, AttributeError):
                                pass
                    elif patch_object == 50:
                        # 建议系统
                        ext = op.get("patch_value", {}).get("ext", {})
                        sp_v2 = ext.get("sp_v2", "")
                        if sp_v2:
                            try:
                                suggestions = json.loads(sp_v2)
                                if isinstance(suggestions, list):
                                    yield {"type": "suggestion", "items": suggestions}
                            except json.JSONDecodeError:
                                pass

            elif evt.event_type == "SSE_REPLY_END":
                end_type = evt.data.get("end_type", 0)
                if end_type >= 1:
                    # 流结束
                    self.session_store.increment_turn(session_id)
                    self.account_pool.mark_success(acc)
                    self.account_pool.release(acc)
                    yield {"type": "done"}
                    return

            elif evt.event_type == "STREAM_ERROR":
                error_msg = evt.data.get("error_msg", "Unknown SSE error")
                log.error(f"[Client] SSE error: {error_msg}")
                self.account_pool.mark_invalid(acc, reason="upstream_error", error_message=error_msg[:200])
                self.account_pool.release(acc)
                yield {"type": "error", "message": error_msg}
                return

        # 如果没收到 SSE_REPLY_END，也标记完成
        if session_meta_updated:
            self.account_pool.mark_success(acc)
        self.account_pool.release(acc)
        yield {"type": "done"}

    @staticmethod
    def _extract_image_urls(block: dict) -> list[str]:
        """从 creation_block 提取图片 URL"""
        urls = []
        content = block.get("content", {})
        creation_block = content.get("creation_block", {})
        creations = creation_block.get("creations", [])
        for creation in creations:
            image = creation.get("image", {})
            url = image.get("image_ori_raw", {}).get("url", "")
            if not url:
                url = image.get("image_ori", {}).get("url", "")
            if not url:
                url = image.get("image_url", "")
            if url and url not in urls:
                urls.append(url)
        return urls

    async def chat_with_retry(
        self,
        text: str,
        bot_id: str = "",
        conversation_id: str = "",
        max_retries: int = None,
    ) -> tuple[StreamResult, Optional[Account], str]:
        """带重试的聊天（非流式）。"""
        max_retries = max_retries or settings.MAX_RETRIES
        exclude = set()

        for attempt in range(max_retries):
            result, acc, session_id = await self.chat(
                text=text,
                bot_id=bot_id,
                conversation_id=conversation_id,
                exclude_accounts=exclude,
            )

            if not result.error:
                return result, acc, session_id

            if acc:
                exclude.add(acc.sessionid)

            log.warning(
                f"[Client] retry {attempt+1}/{max_retries}: "
                f"error={result.error[:100]}"
            )
            await asyncio.sleep(0.5)

        return result, acc, session_id

    async def stream_with_retry(
        self,
        text: str,
        bot_id: str = "",
        conversation_id: str = "",
        max_retries: int = None,
    ) -> AsyncGenerator[dict, None]:
        """带重试的流式聊天。"""
        max_retries = max_retries or settings.MAX_RETRIES
        exclude = set()

        for attempt in range(max_retries):
            got_data = False
            session_id = ""

            async for event in self.chat_stream(
                text=text,
                bot_id=bot_id,
                conversation_id=conversation_id,
                exclude_accounts=exclude,
            ):
                if event["type"] == "meta":
                    session_id = event.get("session_id", "")
                    acc = event.get("acc")
                    if acc:
                        exclude.add(acc.sessionid)
                elif event["type"] == "delta":
                    got_data = True
                    yield event
                elif event["type"] in ("image", "suggestion"):
                    got_data = True
                    yield event
                elif event["type"] == "error":
                    if not got_data and attempt < max_retries - 1:
                        log.warning(
                            f"[Client] stream retry {attempt+1}/{max_retries}: "
                            f"error={event.get('message', '')[:100]}"
                        )
                        break  # 重试
                    else:
                        yield event
                        return
                elif event["type"] == "done":
                    yield event
                    return

            if got_data:
                return  # 已成功完成

            await asyncio.sleep(0.5)

        yield {"type": "error", "message": "Max retries exceeded"}
