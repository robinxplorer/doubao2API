"""
doubao2api v1_chat — OpenAI 兼容的 /v1/chat/completions 接口

豆包特有逻辑：
  - 模型名通过 resolve_bot_id() 映射为 bot_id
  - 多轮对话由豆包服务端管理（conversation_id），客户端不传历史
  - 支持流式（SSE chunk delta）和非流式响应
  - 文生图通过 SSE 中的 creation_block 提取
"""

import asyncio as aio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse

from backend.core.account_pool import Account
from backend.core.config import resolve_bot_id, settings, API_KEYS
from backend.services.doubao_client import DoubaoClient

log = logging.getLogger("doubao2api.chat")
router = APIRouter()


# ── 文生图意图检测 ───────────────────────────────────────────

import re

_T2I_PATTERN = re.compile(
    r"(生成图片|画(一|个|张)?图|draw|generate\s+image|create\s+image|make\s+image|图片生成|文生图|生成一张|画一张)",
    re.IGNORECASE,
)


def _detect_media_intent(messages: list) -> str:
    """检测用户意图：'t2i'（文生图）或 't2t'（纯文本）。"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            else:
                text = str(content)
            if _T2I_PATTERN.search(text):
                return "t2i"
            break
    return "t2t"


def _extract_last_user_text(messages: list) -> str:
    """提取最后一条用户消息的文本。"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                return " ".join(
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            return str(content)
    return ""


# ── 鉴权 ────────────────────────────────────────────────────


def _check_auth(request: Request) -> str:
    """检查 API Key 鉴权，返回 token。"""
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""

    if not token:
        token = request.headers.get("x-api-key", "").strip()
    if not token:
        token = (
            request.query_params.get("key", "").strip()
            or request.query_params.get("api_key", "").strip()
        )

    admin_k = settings.ADMIN_KEY

    if API_KEYS:
        if token != admin_k and token not in API_KEYS and not token:
            raise HTTPException(status_code=401, detail="Invalid API Key")

    return token


# ── OpenAI 兼容格式构建 ──────────────────────────────────────


def _make_chunk(completion_id: str, created: int, model: str, delta: dict, finish_reason=None) -> str:
    """构建 SSE 格式的 chat.completion.chunk。"""
    return json.dumps(
        {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        },
        ensure_ascii=False,
    )


# ── 路由 ────────────────────────────────────────────────────


@router.post("/completions")
@router.post("/chat/completions")
@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    app = request.app
    client: DoubaoClient = app.state.doubao_client
    users_db = app.state.users_db

    # 鉴权
    token = _check_auth(request)

    # 配额检查
    users = await users_db.get()
    user = next((u for u in users if u["id"] == token), None)
    if user and user.get("quota", 0) <= user.get("used_tokens", 0):
        raise HTTPException(status_code=402, detail="Quota Exceeded")

    # 解析请求
    try:
        req_data = await request.json()
    except Exception:
        raise HTTPException(400, {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}})

    model_name = req_data.get("model", "doubao")
    bot_id = resolve_bot_id(model_name)
    stream = req_data.get("stream", False)
    messages = req_data.get("messages", [])

    # 提取用户消息文本
    user_text = _extract_last_user_text(messages)
    if not user_text:
        raise HTTPException(400, {"error": {"message": "No user message found", "type": "invalid_request_error"}})

    # 媒体意图检测
    media_intent = _detect_media_intent(messages)

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    log.info(
        f"[OAI] model={model_name}→bot_id={bot_id}, stream={stream}, "
        f"intent={media_intent}, text_len={len(user_text)}"
    )

    # ── 文生图路由 ──────────────────────────────────────

    if media_intent == "t2i":
        if stream:
            async def generate_image_stream():
                yield f"data: {_make_chunk(completion_id, created, model_name, {'role': 'assistant'})}\n\n"
                try:
                    result, acc, session_id = await client.chat_with_retry(
                        text=user_text, bot_id=bot_id,
                    )
                    # 注意：acc 已在 DoubaoClient 内部 release，此处不再重复释放
                    if result.error:
                        yield f"data: {json.dumps({'error': {'message': result.error, 'type': 'upstream_error'}})}\n\n"
                        return
                    # 提取图片 URL
                    image_urls = result.image_urls
                    if image_urls:
                        content = "\n".join(f"![generated]({u})" for u in image_urls)
                    else:
                        content = result.text
                    yield f"data: {_make_chunk(completion_id, created, model_name, {'content': content})}\n\n"
                    yield f"data: {_make_chunk(completion_id, created, model_name, {}, 'stop')}\n\n"
                    yield "data: [DONE]\n\n"
                except Exception as e:
                    log.error(f"[OAI-T2I] 生成失败: {e}")
                    yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'internal_error'}})}\n\n"

            return StreamingResponse(
                generate_image_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        else:
            try:
                result, acc, session_id = await client.chat_with_retry(
                    text=user_text, bot_id=bot_id,
                )
                # 注意：acc 已在 DoubaoClient 内部 release，此处不再重复释放
                if result.error:
                    raise HTTPException(status_code=500, detail=result.error)
                image_urls = result.image_urls
                content = "\n".join(f"![generated]({u})" for u in image_urls) if image_urls else result.text
                return JSONResponse({
                    "id": completion_id,
                    "object": "chat.completion",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                    "images": image_urls,
                    "usage": {
                        "prompt_tokens": len(user_text),
                        "completion_tokens": len(content),
                        "total_tokens": len(user_text) + len(content),
                    },
                })
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

    # ── 纯文本路由 ──────────────────────────────────────

    if stream:
        async def generate():
            acc: Optional[Account] = None
            sent_role = False
            streamed_len = 0
            session_id = ""

            try:
                async for event in client.stream_with_retry(
                    text=user_text, bot_id=bot_id,
                ):
                    if event["type"] == "meta":
                        acc = event.get("acc")
                        session_id = event.get("session_id", "")
                        yield ": upstream-connected\n\n"
                        continue

                    if event["type"] == "delta":
                        content = event.get("content", "")
                        if not sent_role:
                            yield f"data: {_make_chunk(completion_id, created, model_name, {'role': 'assistant'})}\n\n"
                            sent_role = True
                        streamed_len += len(content)
                        yield f"data: {_make_chunk(completion_id, created, model_name, {'content': content})}\n\n"

                    elif event["type"] == "image":
                        url = event.get("url", "")
                        if url:
                            img_md = f"![generated]({url})"
                            if not sent_role:
                                yield f"data: {_make_chunk(completion_id, created, model_name, {'role': 'assistant'})}\n\n"
                                sent_role = True
                            streamed_len += len(img_md)
                            yield f"data: {_make_chunk(completion_id, created, model_name, {'content': img_md})}\n\n"

                    elif event["type"] == "error":
                        yield f"data: {json.dumps({'error': {'message': event.get('message', 'Unknown'), 'type': 'upstream_error'}})}\n\n"
                        # 注意：acc 已在 DoubaoClient 内部 release
                        return

                    elif event["type"] == "done":
                        break

                # 流结束
                if not sent_role:
                    yield f"data: {_make_chunk(completion_id, created, model_name, {'role': 'assistant'})}\n\n"
                yield f"data: {_make_chunk(completion_id, created, model_name, {}, 'stop')}\n\n"
                yield "data: [DONE]\n\n"

                # 更新用户用量
                users = await users_db.get()
                for u in users:
                    if u["id"] == token:
                        u["used_tokens"] = u.get("used_tokens", 0) + streamed_len + len(user_text)
                        break
                await users_db.save(users)

                # 注意：acc 已在 DoubaoClient._stream_sse 内部 release

            except Exception as e:
                log.error(f"[Stream] exception: {e}")
                yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'internal_error'}})}\n\n"
                # 注意：acc 已在 DoubaoClient 内部 release

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    else:
        # 非流式
        try:
            result, acc, session_id = await client.chat_with_retry(
                text=user_text, bot_id=bot_id,
            )
            # 注意：acc 已在 DoubaoClient 内部 release，此处不再重复释放
            if result.error:
                raise HTTPException(status_code=500, detail=result.error)

            answer_text = result.text
            if result.image_urls:
                img_parts = [f"![generated]({u})" for u in result.image_urls]
                answer_text = "\n".join(img_parts) + "\n" + answer_text if answer_text else "\n".join(img_parts)

            # 更新用户用量
            users = await users_db.get()
            for u in users:
                if u["id"] == token:
                    u["used_tokens"] = u.get("used_tokens", 0) + len(answer_text) + len(user_text)
                    break
            await users_db.save(users)

            return JSONResponse({
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model_name,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": answer_text}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": len(user_text),
                    "completion_tokens": len(answer_text),
                    "total_tokens": len(user_text) + len(answer_text),
                },
            })
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
