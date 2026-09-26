"""E06 research overlay: native full portfolios, conditional 3s/8s hold.

Captured source remains immutable. Engine manifests describe that base only;
scenario + overlay hashes are mandatory parts of the effective experiment.
Candidate execution is provisional until finalize validates full control parity.
"""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import runpy
import sys
import time


def code_hashes():
    directory = Path(__file__).resolve().parent
    return {name: hashlib.sha256((directory/name).read_bytes()).hexdigest() for name in
            ('compare-conditional-breakout-hold.py', 'compare-breakout-hold.py', 'replay-e01-breakout-window.py')}


def install_overlay(strategy, clock, stream, enabled):
    """Change only sustained hold after the native pressure calculation.

    Each evaluation calls native evaluate exactly once on its own retained state.
    The original pressure/score, retest, and response are untouched. The temporary
    hold is restored even on error, so another symbol cannot inherit the branch.
    """
    if strategy.hold_without_retest_seconds != 8:
        raise ValueError('E06 requires base hold 8s')
    original_evaluate, original_pressure = strategy.evaluate, strategy._pressure_score
    counts = Counter()
    selected = {}
    def pressure(*args, **kwargs):
        score, details = original_pressure(*args, **kwargs)
        near, compressed = details.get('nearCloses'), details.get('compressedPullbacks')
        prepared = near is not None and compressed is not None and near >= 2 and compressed >= 2
        hold = 3.0 if enabled and prepared else 8.0
        strategy.hold_without_retest_seconds = hold
        selected.update(nearCloses=near, compressedPullbacks=compressed, prepared=prepared, holdSeconds=hold)
        return score, details
    def evaluate(candles, book, trend, **kwargs):
        strategy.hold_without_retest_seconds = 8.0
        selected.clear()
        try:
            decision = original_evaluate(candles, book, trend, **kwargs)
            if selected:
                counts[str(int(selected['holdSeconds']))] += 1
                stream.write(json.dumps(dict(symbol=kwargs.get('symbol'), monoNs=clock.perf_counter_ns(),
                    generation=decision.details.get('zoneGeneration'), action=decision.action.value,
                    state=decision.details.get('state'), confirmationMode=decision.details.get('breakoutConfirmationMode'),
                    **selected), ensure_ascii=False)+'\n')
            return decision
        finally:
            strategy.hold_without_retest_seconds = 8.0
    strategy._pressure_score = pressure
    strategy.evaluate = evaluate
    return counts


async def run(args):
    source = Path(args.source_root).resolve()
    if not any(Path(p).resolve() == source for p in sys.path): sys.path.insert(0, str(source))
    from scalp_bot.e01_feed import read_header, observations
    from scalp_bot.e01_comparison import E01Comparison
    from scalp_bot.offline_bootstrap import _RecordedSettings, _MemoryRecorder
    from scalp_bot.e01_live import DiskRecorder, write_json
    from scalp_bot.manifest_validation import check_manifest, fingerprint
    directory = Path(__file__).resolve().parent
    reference = runpy.run_path(str(directory/'compare-breakout-hold.py'))
    replay = runpy.run_path(str(directory/'replay-e01-breakout-window.py'))
    root, out = Path(args.directory), Path(args.output_directory)
    out.mkdir(parents=True, exist_ok=False)
    recorder = None
    started = time.monotonic()
    hashes = code_hashes()
    class CandidateRecorder(DiskRecorder):
        def __init__(self, path, clock):
            super().__init__(path, clock)
            self.activations, self.counts = [], Counter()
        def record(self, event, symbol, payload):
            super().record(event, symbol, payload)
            self.counts[event] += 1
            if event in ('symbol_activated', 'symbol_deactivated'):
                self.activations.append((event,symbol,self.clock.perf_counter_ns()))
            if event == 'trade_closed':
                print(json.dumps(dict(symbol=symbol, net=payload['netPnl'],reason=payload['reason'])),flush=True)
    def factory(name, clock):
        nonlocal recorder
        if name != 'baseline': return _MemoryRecorder(clock)
        recorder = (replay['CheckedRecorder'](root/'baseline-events.jsonl',clock) if args.mode == 'control'
                    else CandidateRecorder(out/'candidate-events.jsonl',clock))
        return recorder
    try:
        expected, activations, ledger_hash = reference['read_control'](root,root/'baseline-replay-05'/'result.json')
        with (root/args.feed_name).open(encoding='utf-8') as feed, (out/'hold-decisions.jsonl').open('x',encoding='utf-8') as trace, (out/'equity.jsonl').open('x',encoding='utf-8') as equity_file:
            header = read_header(feed)
            baseline = expected['portfolios']['baseline']
            if (header['config']['breakout_hold_without_retest_seconds'] != 8 or
                header['config']['e01_breakout_obstacle_veto'] is not False or
                baseline['manifest']['config'] != header['config'] or check_manifest(baseline['manifest'])[1] or
                baseline['manifest']['code']['sourceSha256'] != header['code']['sourceSha256'] or
                fingerprint(baseline['manifest']['runtime']) != fingerprint(header['runtime'])):
                raise ValueError('invalid captured baseline')
            scenario = dict(experiment='E06', mode=args.mode, overlayEnabled=args.mode=='candidate',
                rule='each evaluate: nearCloses>=2 AND compressedPullbacks>=2 ->3s; otherwise8s; no timer reset; retest unchanged',
                baseSourceSha256=header['code']['sourceSha256'], baseConfig=header['config'],
                researchCodeHashes=hashes, inputHash=expected['sharedInputHash'], controlLedgerSha256=ledger_hash,
                provenance='base engine manifest alone is insufficient; include this hashed research overlay', holdout=False)
            scenario['scenarioSha256'] = fingerprint(scenario)
            (out/'scenario.json').write_text(json.dumps(scenario,ensure_ascii=False,indent=2),encoding='utf-8')
            pair = E01Comparison(_RecordedSettings(**header['config'],bybit_api_key='',bybit_api_secret=''),
                                 max_events=5_000_000,recorder_factory=factory)
            del pair.engines['candidate']
            engine = pair.engines['baseline']
            counts = install_overlay(engine.strategies['level_breakout'],engine.clock,trace,args.mode=='candidate')
            pair.input_hash = fingerprint(header)
            peak, dd = engine.config.start_balance, 0
            for row in observations(feed,header):
                await pair.apply(row)
                if getattr(recorder,'error',None): raise ValueError('engine error: '+recorder.error)
                if row['kind'] in ('health','stop'):
                    equity = engine.broker.balance + sum(p.unrealized_pnl for p in engine.broker.positions.values())
                    peak, dd = max(peak,equity), max(dd,max(peak,equity)-equity)
                    write_json(equity_file,dict(monoNs=row['monoNs'],balance=engine.broker.balance,equity=equity,openPositions=len(engine.broker.positions)))
                if pair.count % 50000 == 0:
                    trace.flush(); equity_file.flush()
                    print(f'{args.mode}: {pair.count} inputs; {time.monotonic()-started:.1f}s; closed={engine.broker.total_closed_trades}',flush=True)
            if (engine.running or engine.broker.positions or engine.broker.pending_entries or
                pair.count != expected['eventsConsumed'] or pair.input_hash != expected['sharedInputHash']):
                raise ValueError('unsettled or incomplete run')
            trades = pair._trade_ledger(engine)
            net = engine.broker.balance-engine.config.start_balance
            if not math.isclose(net,sum(t['netPnl'] for t in trades),abs_tol=1e-9): raise ValueError('net mismatch')
            if engine._run_manifest['config'] != header['config'] or check_manifest(engine._run_manifest)[1]:
                raise ValueError('base manifest/config changed')
            if code_hashes() != hashes: raise ValueError('research code changed during execution')
            if args.mode == 'control':
                recorder.exhausted()
                if (replay['canonical'](trades) != replay['canonical'](baseline['trades']) or
                    engine.broker.balance != baseline['balance'] or dd != baseline['sampledMaxDrawdownUsd']):
                    raise ValueError('control differs from captured portfolio')
            else:
                if recorder.activations != activations: raise ValueError('recorded subscription coverage differs')
                if any(recorder.counts[e]!=1 for e in ('bot_started','run_summary','bot_stopped')):
                    raise ValueError('candidate lifecycle mismatch')
            result = dict(status='control_parity_matched' if args.mode=='control' else 'candidate_executed_pending_control',
                scenario=scenario, inputs=pair.count, inputHash=pair.input_hash, baseManifest=engine._run_manifest,
                outputEvents=recorder.count if args.mode=='control' else sum(recorder.counts.values()),
                balance=engine.broker.balance,net=net,fees=sum(t['fees'] for t in trades),trades=trades,
                sampledMaxDrawdownUsd=dd,holdEvaluationCounts=dict(counts),subscriptionCoverageMatched=True,
                elapsedSeconds=time.monotonic()-started,holdout=False,productionReady=False)
            (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({k:v for k,v in result.items() if k not in ('baseManifest','scenario','trades')}),flush=True)
            return result
    except BaseException as exc:
        (out/'failure.json').write_text(json.dumps(dict(errorType=type(exc).__name__,error=str(exc))),encoding='utf-8')
        raise
    finally:
        if recorder: recorder.close()


def finalize(control_dir, candidate_dir, output):
    # Pure file verification; no runtime or dotenv imports.
    def digest(value):
        return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    portfolios = []
    for directory, status in ((control_dir,'control_parity_matched'),(candidate_dir,'candidate_executed_pending_control')):
        if (directory/'failure.json').exists(): raise ValueError('failed run')
        result = json.loads((directory/'result.json').read_text(encoding='utf-8'))
        scenario = result['scenario']
        if result['status'] != status or scenario['scenarioSha256'] != digest({k:v for k,v in scenario.items() if k!='scenarioSha256'}):
            raise ValueError('invalid run/scenario')
        if scenario != json.loads((directory/'scenario.json').read_text(encoding='utf-8')):
            raise ValueError('scenario mismatch')
        portfolios.append(result)
    control,candidate = portfolios
    for key in ('inputs','inputHash'):
        if control[key] != candidate[key]: raise ValueError('different inputs')
    for key in ('baseSourceSha256','baseConfig','researchCodeHashes','inputHash','controlLedgerSha256','rule'):
        if control['scenario'][key] != candidate['scenario'][key]: raise ValueError('different research contract')
    if control['scenario']['overlayEnabled'] or not candidate['scenario']['overlayEnabled']:
        raise ValueError('incorrect treatment')
    if code_hashes() != candidate['scenario']['researchCodeHashes']: raise ValueError('research code changed')
    ledger_digest = hashlib.sha256()
    closed, events = [], 0
    with (candidate_dir/'candidate-events.jsonl').open('rb') as stream:
        for raw in stream:
            ledger_digest.update(raw)
            row = json.loads(raw)
            events += 1
            if row['event']=='trade_closed': closed.append(row['payload'])
    if events != candidate['outputEvents'] or closed != candidate['trades']:
        raise ValueError('persisted candidate ledger differs')
    if not math.isclose(sum(t['netPnl'] for t in closed),candidate['net'],abs_tol=1e-9):
        raise ValueError('persisted candidate net differs')
    result = dict(status='E06_development_comparison_validated',control=control,candidate=candidate,
                  candidateLedgerSha256=ledger_digest.hexdigest(),
                  netDifference=candidate['net']-control['net'],holdout=False,productionReady=False)
    with output.open('x',encoding='utf-8') as stream: json.dump(result,stream,ensure_ascii=False,indent=2)
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('--mode',choices=('control','candidate','finalize'),required=True)
    parser.add_argument('--feed-name')
    parser.add_argument('--source-root')
    parser.add_argument('--output-directory',required=True)
    parser.add_argument('--control-directory')
    args=parser.parse_args()
    if args.mode=='finalize':
        result=finalize(Path(args.control_directory),Path(args.directory),Path(args.output_directory)/'comparison.json')
        print(json.dumps(dict(status=result['status'],netDifference=result['netDifference'])))
    else:
        if not args.feed_name or not args.source_root: parser.error('feed-name and source-root required')
        asyncio.run(run(args))
