"""Observe causal book-mid response without changing any replay decision.

Compare every output with the previously verified 8s or 3s portfolio ledger.
The reference is the strategy's book mid at creation of break_started_at, not
the first emitted BREAK, not a historical trade arriving after the break.
"""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import runpy
import sys
import time


class ResponseObserver:
    def __init__(self, output):
        self.output = output
        self.anchors = {}
        self.counts = Counter()
        self.signals = []

    def observe(self, symbol, mono, observed_ms, state, price, decision):
        details = decision.details
        generation = tuple(state.zone_key or ()) if state else ()
        started = state.break_started_at if state else 0
        if not started or not generation:
            if self.anchors.pop(symbol, None) is not None:
                self.counts['resets'] += 1
            return
        key = (generation, started)
        anchor = self.anchors.get(symbol)
        if anchor is None or anchor['key'] != key:
            # A missed creation must stay missing rather than acquire a later price.
            created_now = observed_ms is not None and abs(started*1000-observed_ms) < .01
            anchor = {'key': key, 'price': price if created_now else None,
                      'monoNs': mono if created_now else None}
            self.anchors[symbol] = anchor
            self.counts['episodes'] += 1
        if generation[0] not in ('support', 'resistance'):
            return
        # Preserve the anchor across early returns, but don't mislabel another
        # level or inactive evaluation as a current BREAK observation.
        if tuple(details.get('zoneGeneration') or ()) != generation:
            return
        sign = 1 if generation[0] == 'resistance' else -1
        response = None
        if anchor['price'] and price and mono > anchor['monoNs']:
            response = sign*(price/anchor['price']-1)*10000
        rolling = details.get('directionalResponseBps')
        row = dict(symbol=symbol, monoNs=mono, observedAtMs=observed_ms,
            generation=generation, breakStartedAt=started,
            anchorMonoNs=anchor['monoNs'], anchorPrice=anchor['price'], currentPrice=price,
            priceSource='strategy_book_mid', postBreakResponseBps=response,
            postBreakObserved=response is not None,
            rollingResponseBps=rolling,
            rollingPass5bps=None if rolling is None else rolling >= 5,
            postBreakPass5bps=None if response is None else response >= 5,
            action=decision.action.value, state=details.get('state'),
            confirmationMode=details.get('breakoutConfirmationMode'),
            holdSeconds=details.get('breakHoldSeconds'),
            reasons=decision.reasons)
        self.output.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
        self.counts['observations'] += 1
        if rolling is not None and response is not None and rolling >= 5 and response < 5:
            self.counts['rollingPassPostBreakFail'] += 1
        if decision.action.value in ('long', 'short'):
            self.signals.append(row)

    def wrap(self, strategy, clock):
        original = strategy.evaluate
        def evaluate(candles, book, trend, **kwargs):
            decision = original(candles, book, trend, **kwargs)
            symbol = kwargs.get('symbol')
            self.observe(symbol, clock.perf_counter_ns(), kwargs.get('observed_at_ms'),
                         strategy._states.get(symbol), book.mid, decision)
            return decision
        return evaluate


async def run(args):
    source = Path(args.source_root).resolve()
    if not any(Path(p).resolve() == source for p in sys.path):
        sys.path.insert(0, str(source))
    from scalp_bot.e01_feed import read_header, observations
    from scalp_bot.e01_comparison import E01Comparison
    from scalp_bot.offline_bootstrap import _RecordedSettings, _MemoryRecorder
    from scalp_bot.manifest_validation import fingerprint
    helpers = runpy.run_path(str(Path(__file__).with_name('replay-e01-breakout-window.py')))
    root, out = Path(args.directory), Path(args.output_directory)
    out.mkdir(parents=True, exist_ok=False)
    ledger = root/'baseline-events.jsonl' if args.hold == 8 else root/'hold-3s-portfolio-01'/'candidate-events.jsonl'
    expected = json.loads((root/'result.json').read_text(encoding='utf-8'))
    if args.hold == 8:
        proof = json.loads((root/'baseline-replay-05'/'result.json').read_text(encoding='utf-8'))
        evidence = json.loads((root/'baseline-replay-05'/'entry-evidence.json').read_text(encoding='utf-8'))
        portfolio = expected['portfolios']['baseline']
        if proof['status'] != 'baseline_replay_matched' or proof['inputHash'] != expected['sharedInputHash']:
            raise ValueError('unverified baseline')
    else:
        proof = json.loads((root/'hold-3s-portfolio-01'/'result.json').read_text(encoding='utf-8'))
        evidence = json.loads((root/'hold-3s-portfolio-01'/'validation.json').read_text(encoding='utf-8'))
        portfolio = proof['candidate']
        if proof['status'] != 'completed_development_comparison' or proof['inputHash'] != expected['sharedInputHash']:
            raise ValueError('unverified 3s portfolio')
    with ledger.open('rb') as stream:
        ledger_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
    if ledger_hash != evidence.get('ledgerSha256', evidence.get('candidateLedgerSha256')):
        raise ValueError('ledger hash mismatch')
    recorder = None
    def factory(name, clock):
        nonlocal recorder
        if name != 'baseline':
            return _MemoryRecorder(clock)
        recorder = helpers['CheckedRecorder'](ledger, clock)
        return recorder
    started = time.monotonic()
    try:
        with (root/args.feed_name).open(encoding='utf-8') as feed, (out/'response.jsonl').open('x',encoding='utf-8') as trace:
            header = read_header(feed)
            config = dict(header['config'], breakout_hold_without_retest_seconds=float(args.hold))
            if config != portfolio['manifest']['config'] or config['e01_breakout_obstacle_veto'] is not False:
                raise ValueError('config differs from checked portfolio')
            scenario = dict(holdSeconds=args.hold, mode='observation_only', ledgerSha256=ledger_hash,
                inputHash=expected['sharedInputHash'], sourceSha256=header['code']['sourceSha256'],
                definition='direction * (current book mid / book mid at break creation - 1) * 10000',
                thresholdBps=5, tapeUsed=False, holdout=False)
            (out/'scenario.json').write_text(json.dumps(scenario,indent=2),encoding='utf-8')
            pair = E01Comparison(_RecordedSettings(**config, bybit_api_key='', bybit_api_secret=''),
                                 max_events=5_000_000, recorder_factory=factory)
            del pair.engines['candidate']
            engine = pair.engines['baseline']
            observer = ResponseObserver(trace)
            strategy = engine.strategies['level_breakout']
            strategy.evaluate = observer.wrap(strategy, engine.clock)
            pair.input_hash = fingerprint(header)
            for row in observations(feed, header):
                await pair.apply(row)
                if pair.count % 50000 == 0:
                    trace.flush()
                    print(f'{pair.count} inputs; {time.monotonic()-started:.1f}s; outputs={recorder.count}',flush=True)
            recorder.exhausted()
            if (engine.running or engine.broker.positions or engine.broker.pending_entries or
                pair.count != expected['eventsConsumed'] or pair.input_hash != expected['sharedInputHash'] or
                engine.broker.balance != portfolio['balance'] or
                helpers['canonical'](pair._trade_ledger(engine)) != helpers['canonical'](portfolio['trades'])):
                raise ValueError('replay not equal or unsettled')
            report = dict(status='response_observed_portfolio_matched', scenario=scenario,
                inputs=pair.count, inputHash=pair.input_hash, comparedOutputEvents=recorder.count,
                net=engine.broker.balance-engine.config.start_balance,
                counts=dict(observer.counts), signals=observer.signals,
                ignoredFields=['manifestId','run_summary.payload.latencyMetrics'],
                elapsedSeconds=time.monotonic()-started, profitabilityProven=False)
            (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({k:v for k,v in report.items() if k not in ('signals','scenario')}),flush=True)
            return report
    except BaseException as exc:
        (out/'failure.json').write_text(json.dumps(dict(errorType=type(exc).__name__,error=str(exc))),encoding='utf-8')
        raise
    finally:
        if recorder: recorder.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory')
    parser.add_argument('--feed-name', required=True)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--hold', type=int, choices=(3,8), required=True)
    parser.add_argument('--output-directory', required=True)
    asyncio.run(run(parser.parse_args()))
