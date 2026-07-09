from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from threading import Event
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from api import accounts, ai, image_tasks, register, system
from api.errors import install_exception_handlers
from api.support import resolve_web_asset, start_limited_account_watcher
from services.backup_service import backup_service
from services.config import config
from services.image_service import start_image_cleanup_scheduler
from utils.log import logger


def _should_log_api_timing(path: str) -> bool:
    return (
        path.startswith("/v1/")
        or path.startswith("/api/image-tasks")
    )


def create_app() -> FastAPI:
    app_version = config.app_version

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        stop_event = Event()
        thread = start_limited_account_watcher(stop_event)
        cleanup_thread = start_image_cleanup_scheduler(stop_event)
        backup_service.start()
        config.cleanup_old_images()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=1)
            cleanup_thread.join(timeout=1)
            backup_service.stop()

    app = FastAPI(title="chatgpt2api", version=app_version, lifespan=lifespan)
    install_exception_handlers(app)

    @app.middleware("http")
    async def log_api_timing(request, call_next):
        path = request.url.path
        if not _should_log_api_timing(path):
            return await call_next(request)

        request_id = request.headers.get("x-request-id") or uuid4().hex
        received_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        started = perf_counter()
        logger.info({
            "event": "api_request_received",
            "request_id": request_id,
            "method": request.method,
            "path": path,
            "received_at": received_at,
            "client": request.client.host if request.client else "",
            "content_length": request.headers.get("content-length", ""),
        })
        try:
            response = await call_next(request)
        except Exception:
            logger.exception({
                "event": "api_request_failed",
                "request_id": request_id,
                "method": request.method,
                "path": path,
                "duration_ms": int((perf_counter() - started) * 1000),
            })
            raise
        duration_ms = int((perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Received-At"] = received_at
        response.headers["X-Process-Time-Ms"] = str(duration_ms)
        logger.info({
            "event": "api_request_completed",
            "request_id": request_id,
            "method": request.method,
            "path": path,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
        })
        return response

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(ai.create_router())
    app.include_router(accounts.create_router())
    app.include_router(image_tasks.create_router())
    app.include_router(register.create_router())
    app.include_router(system.create_router(app_version))

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_web(full_path: str):
        asset = resolve_web_asset(full_path)
        if asset is not None:
            return FileResponse(asset)
        if full_path.strip("/").startswith("_next/"):
            raise HTTPException(status_code=404, detail="Not Found")
        fallback = resolve_web_asset("")
        if fallback is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(fallback)

    return app
