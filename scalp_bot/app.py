from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from prometheus_client import make_asgi_app

from .config import settings
from .engine import TradingEngine
from .capture import from_environment


capture = from_environment(settings, os.environ)
engine = capture.engine if capture is not None else TradingEngine(settings)
STATIC_DIR = Path(__file__).parent / "static"


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
        try:
            await engine.close()
        finally:
            if capture is not None:
                capture.finish()


app = FastAPI(title="Scalp Bot", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
if settings.prometheus_enabled:
    app.mount("/metrics", make_asgi_app())


class ToggleBody(BaseModel):
    enabled: bool


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/replay")
async def replay() -> FileResponse:
    return FileResponse(STATIC_DIR / "replay.html")


@app.get("/api/state")
async def state(symbol: str | None = Query(default=None)) -> dict:
    result = engine.public_state(symbol)
    if capture is not None:
        result["capture"] = capture.public()
    return result


@app.get("/api/replay/sessions")
async def replay_sessions() -> dict:
    sessions = await asyncio.to_thread(
        engine.recorder.list_sessions
    )
    return {"sessions": sessions}


@app.get("/api/reviews/opportunities")
async def opportunity_analysis(
    session: str | None = Query(default=None),
    horizon: float = Query(default=120.0, ge=10.0, le=900.0),
) -> dict:
    try:
        return await asyncio.to_thread(
            engine.recorder.opportunity_analysis,
            session,
            horizon_seconds=horizon,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid session name") from exc


@app.get("/api/reviews/trades")
async def trade_review_summaries(
    session: str | None = Query(default=None),
) -> dict:
    try:
        reviews = await asyncio.to_thread(
            engine.recorder.trade_review_summaries,
            session,
        )
        return {
            "session": session or engine.recorder.path.name,
            "reviews": reviews,
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid session name") from exc


@app.get("/api/reviews/trades/{review_id}")
async def trade_review(
    review_id: str,
    session: str | None = Query(default=None),
) -> dict:
    try:
        return await asyncio.to_thread(
            engine.recorder.trade_review,
            review_id,
            session,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Trade review not found") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid session name") from exc


@app.get("/api/replay/session/{name}")
async def replay_session(name: str, symbol: str | None = Query(default=None)) -> dict:
    try:
        return await asyncio.to_thread(
            engine.recorder.replay_bundle,
            name,
            symbol,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid session name") from exc


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
