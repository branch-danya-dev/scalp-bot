import threading

import pytest

from scalp_bot import app as app_module


@pytest.mark.asyncio
async def test_replay_session_listing_runs_recorder_io_off_event_loop(
    monkeypatch,
) -> None:
    caller_thread = threading.get_ident()

    def fake_list_sessions():
        return [{
            "name": "session-test.jsonl",
            "thread": threading.get_ident(),
        }]

    monkeypatch.setattr(
        app_module.engine.recorder,
        "list_sessions",
        fake_list_sessions,
    )

    result = await app_module.replay_sessions()

    assert result["sessions"][0]["name"] == "session-test.jsonl"
    assert result["sessions"][0]["thread"] != caller_thread
