"""
doubao2api admin — 管理后台 API

账号管理（sessionid 体系）、API Key 管理、系统状态查看
"""

import logging
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

from backend.core.config import settings, API_KEYS, save_api_keys

log = logging.getLogger("doubao2api.admin")
router = APIRouter()


# ── 请求模型 ────────────────────────────────────────────────


class AddAccountRequest(BaseModel):
    sessionid: str
    name: str = ""


class RemoveAccountRequest(BaseModel):
    sessionid: str


class AddApiKeyRequest(BaseModel):
    key: str


class RemoveApiKeyRequest(BaseModel):
    key: str


class UpdateMaxInflightRequest(BaseModel):
    value: int


# ── 鉴权 ────────────────────────────────────────────────────


def _check_admin(request: Request):
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    if token != settings.ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Admin key required")


# ── 账号管理 ────────────────────────────────────────────────


@router.get("/accounts")
async def list_accounts(request: Request):
    _check_admin(request)
    pool = request.app.state.account_pool
    result = []
    for acc in pool.accounts:
        result.append({
            "sessionid": acc.sessionid[:8] + "..." if len(acc.sessionid) > 8 else acc.sessionid,
            "name": acc.name,
            "status": acc.get_status_code(),
            "status_text": acc.get_status_text(),
            "inflight": acc.inflight,
            "consecutive_failures": acc.consecutive_failures,
            "last_error": acc.last_error[:100] if acc.last_error else "",
        })
    return {"accounts": result}


@router.post("/accounts/add")
async def add_account(req: AddAccountRequest, request: Request):
    _check_admin(request)
    from backend.core.account_pool import Account

    pool = request.app.state.account_pool
    acc = Account(sessionid=req.sessionid, name=req.name)
    await pool.add(acc)
    log.info(f"[Admin] Account added: {acc.name}")
    return {"status": "ok", "name": acc.name}


@router.post("/accounts/remove")
async def remove_account(req: RemoveAccountRequest, request: Request):
    _check_admin(request)
    pool = request.app.state.account_pool
    await pool.remove(req.sessionid)
    log.info(f"[Admin] Account removed: {req.sessionid[:8]}...")
    return {"status": "ok"}


# ── API Key 管理 ────────────────────────────────────────────


@router.get("/apikeys")
async def list_apikeys(request: Request):
    _check_admin(request)
    return {"keys": list(API_KEYS)}


@router.post("/apikeys/add")
async def add_apikey(req: AddApiKeyRequest, request: Request):
    _check_admin(request)
    API_KEYS.add(req.key)
    save_api_keys(API_KEYS)
    log.info(f"[Admin] API Key added")
    return {"status": "ok"}


@router.post("/apikeys/remove")
async def remove_apikey(req: RemoveApiKeyRequest, request: Request):
    _check_admin(request)
    API_KEYS.discard(req.key)
    save_api_keys(API_KEYS)
    log.info(f"[Admin] API Key removed")
    return {"status": "ok"}


# ── 系统状态 ────────────────────────────────────────────────


@router.get("/status")
async def system_status(request: Request):
    _check_admin(request)
    pool = request.app.state.account_pool
    engine = request.app.state.browser_engine
    session_store = request.app.state.doubao_client.session_store

    return {
        "account_pool": pool.status(),
        "browser_engine": {
            "started": engine._started,
            "pool_size": engine.pool_size,
        },
        "sessions": session_store.status(),
        "config": {
            "engine_mode": settings.ENGINE_MODE,
            "base_url": settings.BASE_URL,
            "default_bot_id": settings.DEFAULT_BOT_ID,
            "max_inflight": settings.MAX_INFLIGHT_PER_ACCOUNT,
            "browser_pool_size": settings.BROWSER_POOL_SIZE,
        },
    }


@router.post("/max_inflight")
async def update_max_inflight(req: UpdateMaxInflightRequest, request: Request):
    _check_admin(request)
    pool = request.app.state.account_pool
    pool.set_max_inflight(req.value)
    log.info(f"[Admin] Max inflight updated to {req.value}")
    return {"status": "ok", "max_inflight": pool.max_inflight}
