"""应用入口：FastAPI + 静态前端托管。启动：python -m uvicorn backend.main:app --port 8765"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api import router
from backend.api_perception import router as perception_router

app = FastAPI(title="交期哨兵 Delivery Sentinel", version="2.0.0")
app.include_router(router)
app.include_router(perception_router)

_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(_FRONTEND / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=_FRONTEND), name="static")


@app.on_event("startup")
def _startup():
    from backend.store import get_store  # 预热：种子数据在启动时即就绪
    get_store()
