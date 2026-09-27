import pytest
from scalp_bot.config import Settings
from scalp_bot.paper import PaperBroker
from scalp_bot.runtime_clock import ReplayRuntimeClock
from scalp_bot.experiment_controller import ExperimentController, ArmSnapshot, HASH_FIELDS, TECHNICAL_GATES


class Arm:
    def __init__(self):
        self.broker = PaperBroker(Settings(_env_file=None, start_balance=1000))
        self.view = ArmSnapshot(1000, 1000, 0, 0, 0, True)
        self.disabled = False
        self.events = []
    def begin(self, start): self.start = start
    def disable_entries(self): self.disabled = True
    def snapshot(self): return self.view
    async def consume(self, event):
        self.events.append(event["value"])
        event["value"] = -1
    async def finalize(self): pass


def fixture():
    a, b = Arm(), Arm()
    clock = ReplayRuntimeClock(wall_seconds=1, mono_ns=1)
    controller = ExperimentController(a, b, clock=clock, emit=lambda *args: None)
    hashes = dict.fromkeys(HASH_FIELDS, "a"*64)
    passport = dict(execution="paper", durationSeconds=1800, lossLimitUsdt=30, drawdownLimitUsdt=30,
        additionalBDrawdownUsdt=0, finalizationSeconds=90, autoRestart=False,
        gates=dict.fromkeys(TECHNICAL_GATES, "MET"), hashes=hashes)
    return controller, a, b, passport, hashes, clock


@pytest.mark.parametrize("key", HASH_FIELDS)
def test_missing_hash_blocks_start(key):
    c, a, b, p, hashes, clock = fixture()
    actual = dict(hashes)
    actual[key] = "b"*64
    with pytest.raises(ValueError): c.start(p, actual)
    assert c.state == "CREATED"


@pytest.mark.parametrize("equity_a,equity_b,reason", [(969, 1000, "fixed_loss_or_drawdown_A"),
    (990, 989.999, "additional_B_drawdown"), (1000, float("nan"), "arm_health_or_unknown_equity")])
def test_fixed_and_relative_stops_stop_both(equity_a, equity_b, reason):
    c, a, b, p, hashes, clock = fixture()
    c.start(p, hashes)
    a.view = ArmSnapshot(1000, equity_a, 0, 0, 0, True)
    b.view = ArmSnapshot(1000, equity_b, 0, 0, 0, True)
    assert not c.check() and c.reason == reason and a.disabled and b.disabled


@pytest.mark.asyncio
async def test_identical_feed_independent_inputs_no_fills_not_pass_restart_forbidden():
    c, a, b, p, hashes, clock = fixture()
    c.start(p, hashes)
    await c.consume(dict(sequence=1, value=10))
    assert a.events == b.events == [10] and a.start == b.start
    result = await c.finalize()
    assert result["tradingGate"] == "INCONCLUSIVE"
    with pytest.raises(RuntimeError): c.start(p, hashes)


def test_eight_hours_require_paired_gate():
    c, a, b, p, hashes, clock = fixture()
    p["durationSeconds"] = 28800
    with pytest.raises(ValueError, match="gates"): c.start(p, hashes)


def test_fault_in_one_disable_callback_cannot_leave_other_arm_accepting():
    c, a, b, p, hashes, clock = fixture()
    c.start(p, hashes)
    def fails(): raise RuntimeError("fault")
    a.disable_entries = fails
    try:
        c.stop("data_gap")
    except RuntimeError:
        pass
    assert b.disabled
