import pytest
from test_experiment_controller import fixture


@pytest.mark.asyncio
async def test_failed_finalization_preserves_each_arm_balance_and_error():
    controller, a, b, passport, hashes, clock = fixture()
    controller.start(passport, hashes)
    async def broken():
        raise RuntimeError("reconciliation failed")
    a.finalize = broken
    result = await controller.finalize()
    assert result["state"] == "INCOMPLETE"
    assert len(result["armSnapshots"]) == 2
    assert result["armSnapshots"][0]["balance"] == 1000
    assert result["finalizationErrors"]
    assert result["net"] is None


@pytest.mark.asyncio
async def test_finalization_timeout_is_bounded_and_preserves_results(monkeypatch):
    import asyncio
    from scalp_bot import experiment_controller
    controller, a, b, passport, hashes, clock = fixture()
    controller.start(passport, hashes)
    async def never_finishes():
        await asyncio.Event().wait()
    a.finalize = never_finishes
    original = asyncio.wait_for
    async def bounded(awaitable, timeout):
        assert timeout == 90
        return await original(awaitable, .01)
    monkeypatch.setattr(experiment_controller.asyncio, "wait_for", bounded)
    result = await controller.finalize()
    assert result["state"] == "INCOMPLETE"
    assert result["finalizationErrors"][0]["errorType"] == "TimeoutError"
    assert result["armSnapshots"][1]["balance"] == 1000
    assert a.disabled and b.disabled


@pytest.mark.asyncio
async def test_unreconciled_exposure_cannot_be_success():
    from dataclasses import replace
    controller, a, b, passport, hashes, clock = fixture()
    controller.start(passport, hashes)
    b.view = replace(b.view, pending=1, reconciled=False)
    result = await controller.finalize()
    assert result["state"] == "INCOMPLETE" and result["net"] is None
    assert result["armSnapshots"][1]["pending"] == 1
