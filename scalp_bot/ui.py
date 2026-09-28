"""Shared original trading terminal and read APIs; no engine is created here."""
import asyncio
from pathlib import Path
from fastapi import HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"


def mount_trading_ui(app, engine_provider, state_provider):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")


    @app.get("/replay")
    async def replay() -> FileResponse:
        return FileResponse(STATIC_DIR / "replay.html")


    @app.get("/api/state")
    async def state(symbol: str | None = Query(default=None)) -> dict:
        return await state_provider(symbol)


    @app.get("/api/replay/sessions")
    async def replay_sessions() -> dict:
        sessions = await asyncio.to_thread(
            engine_provider().recorder.list_sessions
        )
        return {"sessions": sessions}


    @app.get("/api/reviews/opportunities")
    async def opportunity_analysis(
        session: str | None = Query(default=None),
        horizon: float = Query(default=120.0, ge=10.0, le=900.0),
    ) -> dict:
        try:
            return await asyncio.to_thread(
                engine_provider().recorder.opportunity_analysis,
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
                engine_provider().recorder.trade_review_summaries,
                session,
            )
            return {
                "session": session or engine_provider().recorder.path.name,
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
                engine_provider().recorder.trade_review,
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
                engine_provider().recorder.replay_bundle,
                name,
                symbol,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid session name") from exc


