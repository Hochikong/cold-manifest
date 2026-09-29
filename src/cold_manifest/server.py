"""FastAPI 应用：/api 只读查询路由 + 前端静态托管（生产同源）。

注册顺序（oracle 要求）：先注册全部 /api 路由，最后 mount 静态/SPA，
避免 catch-all mount 吞掉 API 请求。
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.routes_collect import batches_router, router as collect_router
from .api.routes_diffs import router as diffs_router
from .api.routes_imports import router as imports_router
from .api.routes_misc import router as misc_router
from .api.routes_snapshots import router as snapshots_router
from .api.routes_tasks import router as tasks_router
from .api.routes_trends import router as trends_router
from .api.state import AppState
from .tasks import TaskRunner

# 前端构建产物目录（仓库根/frontend/dist，随 zip 分发）
_FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _resolve_data_root(explicit: "str | None" = None) -> str:
    return explicit or os.environ.get("CLDM_DATA_ROOT") or "./data"


def create_app(data_root: "str | None" = None) -> FastAPI:
    """构建应用。data_root 优先级：参数 > CLDM_DATA_ROOT > ./data。"""
    app = FastAPI(title="cold-manifest", version=__version__,
                  lifespan=_lifespan_factory(_resolve_data_root(data_root)))

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    # 参数非法 → 400（默认 422 统一转为 400，符合 §6.2 约定）
    @app.exception_handler(RequestValidationError)
    async def validation_to_400(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": exc.errors()}, status_code=400)

    # /api 路由（必须先于静态 mount 注册）
    app.include_router(snapshots_router)
    app.include_router(diffs_router)
    app.include_router(misc_router)
    app.include_router(imports_router)
    app.include_router(collect_router)
    app.include_router(batches_router)
    app.include_router(tasks_router)
    app.include_router(trends_router)

    if _FRONTEND_DIST.is_dir():
        app.mount("/", StaticFiles(directory=str(_FRONTEND_DIST), html=True), name="spa")

        @app.exception_handler(404)
        async def spa_fallback(request: Request, exc: Any) -> JSONResponse | FileResponse:
            # SPA fallback：非 API 路径未命中静态文件时回 index.html
            if request.url.path.startswith("/api"):
                detail = getattr(exc, "detail", None) or "Not Found"
                return JSONResponse({"detail": detail}, status_code=404)
            return FileResponse(_FRONTEND_DIST / "index.html")

    return app


def _lifespan_factory(data_root: str) -> Any:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = AppState(data_root)
        app.state.cldm = state
        runner = TaskRunner(data_root)
        runner.start()
        app.state.task_runner = runner
        try:
            yield
        finally:
            runner.stop()
            state.close()

    return lifespan


app = create_app()
