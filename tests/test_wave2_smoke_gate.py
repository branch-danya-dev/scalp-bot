import importlib.util
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_writer_loss_has_priority_over_no_fills(tmp_path):
    spec = importlib.util.spec_from_file_location("smoke", Path(__file__).parents[1]/"scripts/smoke-trading-model.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    result = dict(naturalFills=0, error=None, writerHealth=dict(inputWriterError="queue exceeded"))
    source = Path(spec.origin).read_text()
    assignment = next(node for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
            and t.slice.value == "acceptance" for t in node.targets))
    environment = dict(vars(smoke), result=result)
    exec(compile(ast.Module(body=[assignment], type_ignores=[]), spec.origin, "exec"), environment)
    assert result["acceptance"] == "INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['input', 'session', 'drops', 'research', 'transport'])
async def test_capture_failure_stops_new_activity_before_deadline(failure):
    spec = importlib.util.spec_from_file_location("smoke_monitor", Path(__file__).parents[1]/"scripts/smoke-trading-model.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    stops = []
    bot = SimpleNamespace(running=True, set_running=lambda value: stops.append(value))
    recorder = SimpleNamespace(inputs=SimpleNamespace(error='byte limit' if failure == 'input' else None),
        health=lambda: dict(writerError='disk error' if failure == 'session' else None,
            droppedRows=int(failure == 'drops')))
    research = SimpleNamespace(health=lambda: dict(failure='research loss' if failure == 'research' else None))
    with pytest.raises(RuntimeError, match='capture recording failed'):
        await smoke.wait_for_capture(bot, recorder, research, .01,
            dict(backpressureEvents=int(failure == 'transport'), discardedMessages=0))
    assert stops == [False]


@pytest.mark.asyncio
async def test_healthy_capture_finishes_without_stopping_early():
    spec = importlib.util.spec_from_file_location("smoke_monitor", Path(__file__).parents[1]/"scripts/smoke-trading-model.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    stops = []
    bot = SimpleNamespace(running=True, set_running=lambda value: stops.append(value))
    recorder = SimpleNamespace(inputs=SimpleNamespace(error=None), health=lambda: {})
    await smoke.wait_for_capture(bot, recorder, None, .001)
    assert stops == []


def test_backpressure_report_invalidates_smoke_even_without_writer_loss():
    spec = importlib.util.spec_from_file_location("smoke_gate", Path(__file__).parents[1]/"scripts/smoke-trading-model.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    result = dict(naturalFills=1, transport=dict(backpressureEvents=1, discardedMessages=0),
        writerHealth=dict(inputAccepted=10, inputWritten=10),
        latencyMs={k: dict(p99=1) for k in ('event_loop_lateness', 'data_to_adapter')})
    assert smoke.assess_smoke(result)['status'] == 'INVALID'


def test_transport_monitor_observes_direct_journal_events_and_retains_rows():
    spec = importlib.util.spec_from_file_location("smoke_transport", Path(__file__).parents[1]/"scripts/smoke-trading-model.py")
    smoke = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(smoke)
    recorded = []
    journal = SimpleNamespace(append=lambda *args: recorded.append(args))
    transport = smoke.monitor_transport(journal)
    journal.append('transport', 'BTCUSDT', dict(phase='disconnect', errorType='MarketDataBackpressureError', discarded=3))
    journal.append('transport', 'BTCUSDT', dict(phase='connect', errorType=None, discarded=0))
    journal.append('clock_error', None, dict(errorType='TimeoutError'))
    assert transport == dict(backpressureEvents=1, discardedMessages=3)
    assert len(recorded) == 3
