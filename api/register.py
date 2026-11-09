from __future__ import annotations

import asyncio

from fastapi import APIRouter, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.support import require_admin
from services.register_service import register_service


class RegisterConfigRequest(BaseModel):
    config_revision: int | None = None
    mail: dict | None = None
    proxy: str | None = None
    total: int | None = None
    threads: int | None = None
    mode: str | None = None
    target_quota: int | None = None
    target_available: int | None = None
    check_interval: int | None = None


class OutlookPoolResetRequest(BaseModel):
    scope: str | None = None


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/register")
    async def get_register_config(authorization: str | None = Header(default=None)):
        await run_in_threadpool(require_admin, authorization)
        return {"register": await run_in_threadpool(register_service.get)}

    @router.post("/api/register")
    async def update_register_config(body: RegisterConfigRequest, authorization: str | None = Header(default=None)):
        await run_in_threadpool(require_admin, authorization)
        return {"register": await _service_call(register_service.update, body.model_dump(exclude_none=True))}

    @router.post("/api/register/start")
    async def start_register(authorization: str | None = Header(default=None)):
        await run_in_threadpool(require_admin, authorization)
        return {"register": await _service_call(register_service.start)}

    @router.post("/api/register/stop")
    async def stop_register(authorization: str | None = Header(default=None)):
        await run_in_threadpool(require_admin, authorization)
        return {"register": await _service_call(register_service.stop)}

    @router.post("/api/register/reset")
    async def reset_register(authorization: str | None = Header(default=None)):
        await run_in_threadpool(require_admin, authorization)
        return {"register": await _service_call(register_service.reset)}

    @router.post("/api/register/outlook-pool/reset")
    async def reset_outlook_pool(body: OutlookPoolResetRequest, authorization: str | None = Header(default=None)):
        await run_in_threadpool(require_admin, authorization)
        return {"register": await _service_call(register_service.reset_outlook_pool, body.scope or "all")}

    @router.get("/api/register/events")
    async def register_events(token: str = ""):
        await run_in_threadpool(require_admin, f"Bearer {token}")

        async def stream():
            last = ""
            while True:
                payload = await run_in_threadpool(register_service.snapshot_json)
                if payload != last:
                    last = payload
                    yield f"data: {payload}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return router


async def _service_call(handler, *args):
    try:
        return await run_in_threadpool(handler, *args)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail={"error": f"配置保存失败，原文件或备份已保留: {exc}"}) from exc
