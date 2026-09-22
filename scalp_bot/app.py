from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import settings
from .engine import TradingEngine


engine = TradingEngine(settings)
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await engine.start()
    try:
        yield
    finally:
        await engine.close()


app = FastAPI(title="Scalp Bot", version="0.2.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
    return engine.public_state(symbol)


@app.get("/api/replay/sessions")
async def replay_sessions() -> dict:
    return {"sessions": engine.recorder.list_sessions()}


@app.get("/api/replay/session/{name}")
async def replay_session(name: str, symbol: str | None = Query(default=None)) -> dict:
    try:
        return engine.recorder.replay_bundle(name, symbol)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid session name") from exc


@app.post("/api/bot/start")
async def start_bot() -> dict:
    try:
        engine.set_running(True)
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
    try:
        engine.toggle_strategy(key, body.enabled)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown strategy") from exc
    return {"ok": True, "strategy": key, "enabled": body.enabled}
