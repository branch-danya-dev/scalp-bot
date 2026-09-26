"""Full offline 3s portfolio against an already verified 8s captured baseline.

Sequential paired comparison, with unchanged captured source and E01 disabled.
Only breakout_hold_without_retest_seconds changes at engine construction.
"""
import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys
import time


def read_control(root, proof_path):
    import msgspec
    expected = json.loads((root/'result.json').read_text(encoding='utf-8'))
    proof = json.loads(Path(proof_path).read_text(encoding='utf-8'))
    if ((root/'failure.json').exists() or expected['status'] != 'comparison_completed'
            or proof['status'] != 'baseline_replay_matched'
            or proof['inputs'] != expected['eventsConsumed']
            or proof['inputHash'] != expected['sharedInputHash']
            or proof['baselineNet'] != expected['portfolios']['baseline']['net']):
        raise ValueError('missing or inconsistent verified baseline')
    activations, trades, digest, count = [], [], hashlib.sha256(), 0
    with (root/'baseline-events.jsonl').open('rb') as source:
        for line in source:
            digest.update(line)
            row = msgspec.json.decode(line)
            count += 1
            if row['event'] in ('symbol_activated', 'symbol_deactivated'):
                activations.append((row['event'], row['symbol'], row['monoNs']))
            if row['event'] == 'trade_closed': trades.append(row['payload'])
    if count != proof['comparedOutputEvents'] or trades != expected['portfolios']['baseline']['trades']:
        raise ValueError('baseline ledger differs from verified result')
    return expected, activations, digest.hexdigest()


async def run(args):
    source_root = Path(args.source_root).resolve()
    if not any(Path(p).resolve() == source_root for p in sys.path):
        sys.path.insert(0, str(source_root))
    from scalp_bot.e01_feed import read_header, observations
    from scalp_bot.e01_comparison import E01Comparison
    from scalp_bot.e01_live import DiskRecorder, write_json
    from scalp_bot.offline_bootstrap import _RecordedSettings, _MemoryRecorder
    from scalp_bot.manifest_validation import check_manifest, fingerprint

    root, output = Path(args.directory), Path(args.output_directory)
    expected, source_activations, source_ledger_hash = read_control(root, args.baseline_proof)
    output.mkdir(parents=True, exist_ok=False)
    recorder = None
    started = time.monotonic()

    class CandidateRecorder(DiskRecorder):
        def __init__(self, path, clock):
            super().__init__(path, clock)
            self.activations, self.counts = [], Counter()

        def record(self, event, symbol, payload):
            super().record(event, symbol, payload)
            self.counts[event] += 1
            if event in ('symbol_activated', 'symbol_deactivated'):
                self.activations.append((event, symbol, self.clock.perf_counter_ns()))
            if event in ('trade_opened', 'trade_closed'):
                plan = payload.get('plan', payload)
                print(json.dumps(dict(event=event, symbol=symbol, strategy=plan.get('strategy'),
                    monoNs=self.clock.perf_counter_ns(), net=payload.get('netPnl'), reason=payload.get('reason'))), flush=True)

    def factory(name, clock):
        nonlocal recorder
        if name != 'baseline': return _MemoryRecorder(clock)
        recorder = CandidateRecorder(output/'candidate-events.jsonl', clock)
        return recorder

    try:
        with (root/args.feed_name).open(encoding='utf-8') as feed, (output/'equity.jsonl').open('x',encoding='utf-8') as equity_file:
            header = read_header(feed)
            baseline = expected['portfolios']['baseline']
            if (header['schema'] != 'e01-feed-v2' or header['config']['breakout_hold_without_retest_seconds'] != 8
                    or header['config']['e01_breakout_obstacle_veto'] is not False
                    or baseline['manifest']['config'] != header['config']
                    or check_manifest(baseline['manifest'])[1]
                    or baseline['manifest']['code']['sourceSha256'] != header['code']['sourceSha256']
                    or fingerprint(baseline['manifest']['runtime']) != fingerprint(header['runtime'])):
                raise ValueError('expected verified 8s baseline with E01 disabled and matching manifest')
            config = _RecordedSettings(**dict(header['config'], breakout_hold_without_retest_seconds=3.0),
                                       bybit_api_key='', bybit_api_secret='')
            scenario = dict(experiment='breakout_hold_8s_vs_3s_development', baselineSeconds=8, candidateSeconds=3,
                e01=False, inputHash=expected['sharedInputHash'], sourceCode=header['code']['sourceSha256'],
                baselineLedgerSha256=source_ledger_hash, baselineProof=str(args.baseline_proof),
                candidateConfigChanges={'breakout_hold_without_retest_seconds':3.0},
                purpose='Diagnostic full-portfolio replay on already explored data; not holdout',
                execution='Separate portfolio from cold start; original strategy, arbiter, risk and paper broker')
            (output/'scenario.json').write_text(json.dumps(scenario,indent=2),encoding='utf-8')
            pair = E01Comparison(config, max_events=5_000_000, recorder_factory=factory)
            del pair.engines['candidate']  # Keep the E01-off engine, named baseline internally.
            engine = pair.engines['baseline']
            if engine.strategies['level_breakout'].hold_without_retest_seconds != 3:
                raise ValueError('candidate hold not applied at construction')
            pair.input_hash = fingerprint(header)
            peak, drawdown, samples = config.start_balance, 0, 0
            for row in observations(feed, header):
                await pair.apply(row)
                if recorder.error: raise ValueError('candidate engine error: '+recorder.error)
                if row['kind'] in ('health','stop'):
                    equity = engine.broker.balance + sum(p.unrealized_pnl for p in engine.broker.positions.values())
                    peak = max(peak, equity)
                    drawdown = max(drawdown, peak-equity)
                    write_json(equity_file, dict(monoNs=row['monoNs'],wallSeconds=row['wallSeconds'],
                        balance=engine.broker.balance,equity=equity,openPositions=len(engine.broker.positions)))
                    samples += 1
                if pair.count % 50000 == 0:
                    recorder.stream.flush()
                    equity_file.flush()
                    print(f'{pair.count} inputs; {time.monotonic()-started:.1f}s; closed={engine.broker.total_closed_trades}',flush=True)
            if (engine.running or engine.broker.positions or engine.broker.pending_entries
                    or pair.count != expected['eventsConsumed'] or pair.input_hash != expected['sharedInputHash']):
                raise ValueError('incomplete feed or unsettled candidate')
            if recorder.activations != source_activations:
                raise ValueError('candidate symbol lifetimes differ: recorded subscription coverage not established')
            trades = pair._trade_ledger(engine)
            manifest = engine._run_manifest
            changed = [k for k,v in header['config'].items() if manifest['config'][k] != v]
            if (changed != ['breakout_hold_without_retest_seconds'] or check_manifest(manifest)[1]
                    or manifest['config']['e01_breakout_obstacle_veto'] is not False):
                raise ValueError('candidate differs beyond hold')
            net = engine.broker.balance-config.start_balance
            if not math.isclose(net, sum(t['netPnl'] for t in trades), abs_tol=1e-9):
                raise ValueError('candidate ledger/balance mismatch')
            if any(recorder.counts[e] != 1 for e in ('bot_started','run_summary','bot_stopped')):
                raise ValueError('invalid candidate lifecycle')
            candidate = dict(manifest=manifest,balance=engine.broker.balance,net=net,
                fees=sum(t['fees'] for t in trades),trades=trades,sampledMaxDrawdownUsd=drawdown,
                eventCounts=dict(recorder.counts),equitySamples=samples)
            result = dict(status='completed_development_comparison',scenario=scenario,
                eventsConsumed=pair.count,inputHash=pair.input_hash,configDifferences=changed,
                subscriptionLifetimesMatch=True,baselineReusedFromVerifiedReplay=True,
                baseline={k:baseline[k] for k in ('manifest','balance','net','fees','trades','sampledMaxDrawdownUsd')},
                candidate=candidate,netDifference=net-baseline['net'],elapsedSeconds=time.monotonic()-started,
                profitabilityProven=False,productionReady=False,holdout=False)
            (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
            print(json.dumps({k:v for k,v in result.items() if k not in ('baseline','candidate','scenario')}),flush=True)
            return result
    except BaseException as exc:
        (output/'failure.json').write_text(json.dumps(dict(errorType=type(exc).__name__,error=str(exc))),encoding='utf-8')
        raise
    finally:
        if recorder is not None: recorder.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory')
    p.add_argument('--feed-name',required=True)
    p.add_argument('--source-root',required=True)
    p.add_argument('--baseline-proof',required=True)
    p.add_argument('--output-directory',required=True)
    asyncio.run(run(p.parse_args()))


if __name__ == '__main__': main()
