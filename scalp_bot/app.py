from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from prometheus_client import make_asgi_app

from .config import settings
from .engine import TradingEngine
from .capture import from_environment
from .ui import mount_trading_ui


capture = from_environment(settings, os.environ)
engine = capture.engine if capture is not None else TradingEngine(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    monitor = None
    try:
        await engine.start()
        if capture is not None:
            monitor = asyncio.create_task(capture.monitor(), name="capture-health")
        yield
    finally:
        if monitor is not None:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        if capture is not None:
            await capture.close()
        else:
            await engine.close()


app = FastAPI(title="Scalp Bot", version="0.2.0", lifespan=lifespan)
if settings.prometheus_enabled:
    app.mount("/metrics", make_asgi_app())


class ToggleBody(BaseModel):
    enabled: bool


async def state(symbol: str | None = Query(default=None)) -> dict:
    result = capture.state(symbol) if capture is not None else engine.public_state(symbol)
    if capture is not None:
        result["capture"] = capture.public()
    return result


mount_trading_ui(app, lambda: engine, state)


@app.post("/api/bot/start")
async def start_bot() -> dict:
    try:
        if capture is not None:
            capture.before_start()
        engine.set_running(True)
        if capture is not None:
            capture.started = True
    except RuntimeError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    return {"ok": True, "running": True}


@app.post("/api/bot/stop")
async def stop_bot() -> dict:
    if capture is None or (not capture.finished and capture._close_task is None):
        engine.set_running(False)
    return {"ok": True, "running": False}


@app.post("/api/strategies/{key}")
async def toggle_strategy(key: str, body: ToggleBody) -> dict:
    if capture is not None:
        raise HTTPException(status_code=409, detail="Состав стратегий зафиксирован профилем записи")
    try:
        engine.toggle_strategy(key, body.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown strategy") from exc
    return {"ok": True, "strategy": key, "enabled": body.enabled}
