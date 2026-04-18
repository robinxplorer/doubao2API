"""
doubao2api probes — 健康检查与就绪探针
"""

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request):
    app = request.app
    engine = getattr(app.state, "browser_engine", None)
    if engine and engine._started:
        return {"status": "ready"}
    return {"status": "not_ready"}
