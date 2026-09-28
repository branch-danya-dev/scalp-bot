import importlib.util
from pathlib import Path
from types import SimpleNamespace

from scalp_bot.runtime_clock import ReplayRuntimeClock


def test_capture_profile_wraps_existing_session_clocks_and_keeps_envelopes_raw():
    spec = importlib.util.spec_from_file_location('capture_profile', Path(__file__).parents[1]/'scripts/profile-capture-model.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _, _, Engine, _, _ = module.capture_components(clock_reads=True)
    engine = Engine.__new__(Engine)
    raw = ReplayRuntimeClock(wall_seconds=100, mono_ns=1000)
    rows = []
    engine.clock = raw
    engine.recorder = SimpleNamespace(record=lambda event, symbol, row: rows.append(row))
    engine.broker = SimpleNamespace(clock=raw)
    engine.sessions = {'AAA': SimpleNamespace(clock=raw)}
    engine.benchmark_warmup_complete()
    assert engine.input_journal.clock is raw
    assert engine.sessions['AAA'].clock is engine.broker.clock is engine.clock
    engine.clock.set_observation(wall_seconds=101, mono_ns=2000)
    assert engine.sessions['AAA'].clock.time() == 101
    assert rows[-1]['kind'] == 'clock_read'
    assert rows[-1]['body'] == dict(scopeId=None, method='time', value=101)
    assert rows[-1]['processingMonoNs'] == 2000
