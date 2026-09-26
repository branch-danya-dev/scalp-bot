"""Local archived-snapshot checks. NOT cold replay or counterfactual portfolio PnL."""
import argparse
from copy import deepcopy
from dataclasses import fields
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scalp_bot.config import Settings
from scalp_bot.domain import Action, Side, StrategyDecision, Trend
from scalp_bot.execution_book import coherent_execution_book
from scalp_bot.research import _book_from_public, _candle_from_public
from scalp_bot.risk import RiskEngine
from scalp_bot.scenario import ROLES, Scenario, ScenarioRouter
from scalp_bot.strategy.base import Strategy
from scalp_bot.strategy.flow_context import build_multi_horizon_flow_context
from scalp_bot.strategy.liquidity import LiquidityTarget
from scalp_bot.strategy.liquidity_evidence import LiquidityEvidence, LiquidityEvidenceState
from scalp_bot.strategy.market_context import MarketContext, ExecutionContext, build_structure_context
from scalp_bot.strategy.pre_state import FormingCandleContext
from scalp_bot.strategy.regime import HTFBias, HTFBiasSnapshot, LocalRegime, LocalRegimeSnapshot
from scalp_bot.strategy.structure import market_structure_from_public
from scalp_bot.strategy.targets import structural_target, movement_budget


def snake(key):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()


def decode(cls, source, enums=None):
    data = {snake(k): v for k, v in source.items()}
    for k, enum in (enums or {}).items():
        data[k] = enum(data[k])
    return cls(**{f.name: data[f.name] for f in fields(cls) if f.name in data})


def snapshot(market):
    raw = market['marketContext']
    h = raw['htfBias']
    htf = HTFBiasSnapshot(HTFBias(h['bias']), h['strength'], Trend(h['trend15m']),
        Trend(h['trend1h']), h['alignment'], Trend(h['legacyTrend'])) if h else None
    local = decode(LocalRegimeSnapshot, raw['localRegime'], dict(regime=LocalRegime,
        direction=Trend, parent_direction=Trend, structure_1m=Trend, structure_5m=Trend)) if raw['localRegime'] else None
    structure = market_structure_from_public(market.get('structure'))
    flow = build_multi_horizon_flow_context(market['tradeFlow'], market['bookFlow'], observed_at_ms=raw['observedAtMs'])
    liquidity = decode(LiquidityEvidence, raw['liquidityEvidence'],
        dict(state=LiquidityEvidenceState, directional_bias=Trend)) if raw.get('liquidityEvidence') else None
    forming = decode(FormingCandleContext, raw['formingCandle'], dict(direction=Trend)) if raw.get('formingCandle') else None
    context = MarketContext(raw['symbol'], raw['observedAtMs'], raw['lastPrice'], Trend(raw['legacyTrend']),
        htf, local, flow, liquidity, build_structure_context(structure, raw['lastPrice']),
        decode(ExecutionContext, raw['executionContext']), forming)
    return context, [_candle_from_public(c) for c in market['candles']], structure, _book_from_public(market['fastOrderbook']), _book_from_public(market['deepOrderbook'])


def decision(raw):
    return StrategyDecision(raw['strategy'], Action(raw['action']), [], entry=raw['entry'],
        stop=raw['stop'], target=raw['target'], setup_id=raw.get('setup_id'),
        watched_level=raw.get('watched_level'), details=deepcopy(raw.get('details', {})))


def check_case(case, config):
    context, candles, structure, fast, deep = snapshot(case['openMarket'])
    enabled = {k: k in ('level_breakout', 'weak_level_rejection', 'orderbook_density') for k in ROLES}
    router = ScenarioRouter()
    assigned = router.observe(case['symbol'], context, candles, structure, enabled, 0)
    raw = case.get('lastDecision', case.get('decision'))
    old = decision(raw)
    new = decision(raw)
    new.target, _, target_source = structural_target(fast.executable_entry(new.side), new.action,
        [LiquidityTarget(**r) for r in raw['details']['liquidityLadder']],
        movement=movement_budget(candles))
    new.details['expectedImpulsePct'] = movement_budget(candles)/new.entry
    new.details['targetSource'] = target_source
    strategy = Strategy(); strategy.key = new.strategy
    invalid = strategy.entry_invalidation(new, fast)
    risk = RiskEngine(config)
    # Same balance/risk budget and corrected execution for BOTH geometries.
    # These are plan-level checks, never reconstructed historical allocations.
    results = [risk.build_plan(case['symbol'], d, config.start_balance, fast,
        config.start_balance*config.max_leverage, config.start_balance*config.max_total_risk_fraction,
        depth_book=deep) for d in (old, new)]
    return dict(id=case['id'], symbol=case['symbol'], originalOwner=case['strategy'],
        originalNet=case['oldClose']['netPnl'], originalExit=case['oldClose']['reason'],
        snapshotOwner=assigned.owner if assigned else None, snapshotSide=assigned.side if assigned else None,
        situation=router.public(case['symbol'])['situation']['status'],
        oldTarget=old.target, newTarget=new.target, newTargetSource=target_source,
        targetInputs='recorded causal liquidity ladder from original decision; not rebuilt from truncated chart history',
        originalPlanSameExecution=dict(allowed=results[0].allowed, reason=results[0].reason),
        newPlanSameExecution=dict(allowed=results[1].allowed, reason=results[1].reason),
        entryInvalidation=invalid, executionBook=coherent_execution_book(fast, deep).execution,
        newEntry=None, newExit=None, newNet=None,
        limitation='snapshot selection and geometry only; owner preparation/portfolio prefix absent')


def check_retry(case):
    """T06 actual first bid/ask and last bid/ask; no invented book quantities."""
    context, rows, _, fast, _ = snapshot(case['openMarket'])
    original = decision(case['firstDecision'])
    class RecordedQuote:
        def executable_entry(self, side):
            return case['firstExecutionContext']['bestAsk' if side==Side.LONG else 'bestBid']
    router = ScenarioRouter()
    span = movement_budget(rows)/2
    s = Scenario(case['symbol'], 'archived:first-ready-contract', original.strategy, original.side.value,
        original.setup_id, original.entry, span, 0, 0, 300, ['local contract from first ready decision'])
    router.scenarios[case['symbol']] = s
    first = router.accept_decision(case['symbol'], original, 0, RecordedQuote())
    router.reject(case['symbol'], 'risk', 'recorded economic refusal', 1)
    last = router.accept_decision(case['symbol'], decision(case['lastDecision']), case['signalToEntrySeconds'], fast)
    return dict(id=case['id'], firstTarget=first.target, frozenTarget=s.frozen['target'],
        oldLastTarget=case['lastDecision']['target'], newLastTradeable=last.tradeable,
        state=s.state, reason=last.reasons, firstSignalMono=s.first_signal_mono,
        limitation='original owner contract isolated; no full state replay')


def corrected_t03_final_leg(case, config):
    _, _, _, fast, deep = snapshot(case['closeMarket'])
    book = coherent_execution_book(fast, deep)
    close = case['oldClose']
    quantity = close['originalQuantity']-sum(p['closedQuantity'] for p in case['partialEvents'])
    raw, visible, _ = book.exit_vwap_quantity(Side.SHORT, quantity)
    assert visible >= quantity
    fill = raw*(1+config.slippage_bps/10000)
    delta_gross = (close['exit']-fill)*quantity
    delta_fee = (fill-close['exit'])*quantity*config.taker_fee_rate
    return dict(originalNet=close['netPnl'], correctedSameFinalLegNet=close['netPnl']+delta_gross-delta_fee,
        originalFill=close['exit'], correctedFill=fill, finalQuantity=quantity,
        executionQuality=book.execution,
        limitation='final leg only; actual entry and partial fixed; NOT a new strategy trade')


def run():
    root = Path(__file__).resolve().parents[1]
    fixture = json.loads((root/'tests/fixtures/entry_edge_controls.json').read_text(encoding='utf-8'))
    winners = json.loads((root/'tests/fixtures/scenario_older_winners.json').read_text(encoding='utf-8'))
    config = Settings(_env_file=None, **fixture['config'])
    return dict(type='local_snapshot_regression', fullPortfolioReplay=False, profitabilityProven=False,
        sourceCommit=fixture['sourceCommit'], sourceSha256=fixture['sourceSha256'],
        sixControls=[check_case(c, config) for c in fixture['cases']],
        olderWinners=[check_case(c, config) for c in winners['cases']],
        olderWinnerLimitation='old manifest unavailable; same current audited risk config for plan-only comparison',
        t06Retry=check_retry(fixture['cases'][5]),
        t03Execution=corrected_t03_final_leg(fixture['cases'][2], config))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'controls':len(result['sixControls']), 'olderWinners':len(result['olderWinners']),
                      'fullPortfolioReplay':False, 'output':str(args.output)}))
