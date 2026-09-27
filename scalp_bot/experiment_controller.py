"""External paper A/B guard. No network clients or automatic launch authority."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
import math

from .paper import PaperBroker

HASH_FIELDS = ("source", "config_a", "config_b", "runtime", "dataset", "split", "model", "calibration", "adapter")
TECHNICAL_GATES = ("baseline_natural_fill", "capture_integrity", "latency", "model_data",
    "model_economic_ranking", "adapter", "ledger_isolation")


def validate_passport(passport, actual_hashes):
    if passport.get("execution") != "paper" or passport.get("lossLimitUsdt") != 30 or passport.get("drawdownLimitUsdt") != 30 or passport.get("additionalBDrawdownUsdt") != 0:
        raise ValueError("paper fixed experiment limits required")
    if passport.get("durationSeconds") not in {1800, 3600, 28800}:
        raise ValueError("only preregistered technical or 8h durations")
    if passport.get("finalizationSeconds") != 90 or passport.get("autoRestart") is not False:
        raise ValueError("bounded finalization and no restart required")
    gates = TECHNICAL_GATES + (("paired_technical_run",) if passport["durationSeconds"] == 28800 else ())
    if not all(passport.get("gates", {}).get(g) == "MET" for g in gates):
        raise ValueError("experiment gates unresolved")
    for key in HASH_FIELDS:
        value = passport.get("hashes", {}).get(key)
        if (not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
                or actual_hashes.get(key) != value):
            raise ValueError("missing/mismatched experiment hash: "+key)


@dataclass(frozen=True)
class ArmSnapshot:
    balance: float
    equity: float
    natural_fills: int
    pending: int
    positions: int
    reconciled: bool
    healthy: bool = True


class ExperimentController:
    """Arms implement begin, consume, snapshot, disable_entries, finalize.

    One caller feeds every ordered event to both arms; scanner decisions are part
    of that same shared event stream. Arm implementations must not open feeds.
    Finalization cancels pending and closes/reconciles using each arm's own costs.
    """
    def __init__(self, arm_a, arm_b, *, clock, emit):
        if type(arm_a.broker) is not PaperBroker or type(arm_b.broker) is not PaperBroker:
            raise ValueError("PaperBroker-only experiment")
        if arm_a is arm_b or arm_a.broker is arm_b.broker or arm_a.broker.positions is arm_b.broker.positions:
            raise ValueError("independent A/B ledgers required")
        self.arms = (arm_a, arm_b)
        self.clock, self.emit = clock, emit
        self.state = "CREATED"
        self.reason = None
        self.peaks = [1000., 1000.]
        self.sequence = -1
        self.last_mono = None
        self.passport = None
        self.start_ns = None

    def start(self, passport, actual_hashes):
        if self.state != "CREATED":
            raise RuntimeError("experiment cannot restart")
        validate_passport(passport, actual_hashes)
        for arm in self.arms:
            if arm.broker.balance != 1000 or arm.broker.positions or arm.broker.pending_entries:
                raise ValueError("fresh independent 1000 USDT ledgers required")
        self.passport = deepcopy(passport)
        self.start_ns = self.clock.perf_counter_ns()
        self.state = "RUNNING"
        try:
            for arm in self.arms:
                arm.begin(self.start_ns)
        except Exception:
            self.stop("arm_start_failure")
            raise
        self.emit("experiment_start", dict(startMonoNs=self.start_ns, passport=self.passport))

    def stop(self, reason):
        if self.state not in {"RUNNING", "STOPPING"}:
            return
        self.state = "STOPPING"
        self.reason = self.reason or reason
        for arm in self.arms:
            arm.disable_entries()
        self.emit("experiment_stop", dict(reason=self.reason))

    def check(self):
        if self.state != "RUNNING":
            return False
        now = self.clock.perf_counter_ns()
        if self.last_mono is not None and now < self.last_mono:
            self.stop("monotonic_regression")
            return False
        self.last_mono = now
        if now-self.start_ns >= self.passport["durationSeconds"]*1_000_000_000:
            self.stop("duration_elapsed")
            return False
        snapshots = [arm.snapshot() for arm in self.arms]
        drawdowns = []
        for i, snapshot in enumerate(snapshots):
            if not snapshot.healthy or not all(math.isfinite(v) for v in (snapshot.balance, snapshot.equity)):
                self.stop("arm_health_or_unknown_equity")
                return False
            self.peaks[i] = max(self.peaks[i], snapshot.equity)
            drawdown = self.peaks[i]-snapshot.equity
            drawdowns.append(drawdown)
            if 1000-snapshot.equity >= 30 or 1000-snapshot.balance >= 30 or drawdown >= 30:
                self.stop("fixed_loss_or_drawdown_"+"AB"[i])
                return False
        if drawdowns[1] > drawdowns[0]:
            self.stop("additional_B_drawdown")
            return False
        return True

    async def consume(self, event):
        if not self.check():
            return
        sequence = event["sequence"]
        if sequence <= self.sequence:
            self.stop("shared_feed_order")
            return
        self.sequence = sequence
        try:
            # Mutating MarketMessage/decision annotations cannot leak A -> B.
            for arm in self.arms:
                await arm.consume(deepcopy(event))
        except Exception:
            self.stop("arm_consumer_failure")
            raise
        self.check()

    async def run(self, shared_source):
        """Clock/risk checks continue during a silent feed; never extend Start."""
        if self.state != "RUNNING":
            raise RuntimeError("explicit validated Start required")
        iterator = shared_source.__aiter__()
        pending = None
        try:
            while self.check():
                if pending is None:
                    pending = asyncio.create_task(anext(iterator))
                ready, _ = await asyncio.wait({pending}, timeout=.1)
                if not ready:
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    self.stop("feed_ended")
                    break
                pending = None
                remaining = max(0., self.passport["durationSeconds"]-(self.clock.perf_counter_ns()-self.start_ns)/1e9)
                await asyncio.wait_for(self.consume(event), timeout=min(1., remaining))
        except asyncio.CancelledError:
            self.stop("controller_cancelled")
            raise
        except Exception:
            self.stop("feed_or_arm_failure")
        finally:
            if pending is not None:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if self.state == "STOPPING":
                await asyncio.shield(self.finalize())

    async def finalize(self):
        if self.state == "RUNNING":
            self.stop("requested_stop")
        if self.state != "STOPPING":
            raise RuntimeError("finalization requires one stopped experiment")
        try:
            await asyncio.wait_for(asyncio.gather(*(arm.finalize() for arm in self.arms)), timeout=90)
            snapshots = [arm.snapshot() for arm in self.arms]
            complete = all(s.reconciled and s.healthy and s.pending == 0 and s.positions == 0
                and math.isfinite(s.balance) and math.isfinite(s.equity) and abs(s.balance-s.equity) < 1e-6 for s in snapshots)
        except Exception:
            complete, snapshots = False, []
        self.state = "COMPLETED" if complete else "INCOMPLETE"
        result = dict(state=self.state, reason=self.reason,
            naturalFills=[s.natural_fills for s in snapshots],
            net=[s.balance-1000 for s in snapshots] if complete else None,
            tradingGate="MET" if complete and all(s.natural_fills > 0 for s in snapshots) else "INCONCLUSIVE",
            autoRestart=False)
        self.emit("experiment_final", result)
        return result
