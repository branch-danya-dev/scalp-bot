"""Offline baseline replay with streamed ledger comparison and breakout probes.

Use a separately restored, hash-matching captured source tree. Diagnostic hold
variants evaluate independent clones at a single step; they never place orders
or feed state back into the baseline. They are not portfolio counterfactuals.
"""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import time


def canonical(value):
    if isinstance(value, dict):
        return {k:canonical(v) for k,v in value.items() if k != 'manifestId'}
    if isinstance(value, (list, tuple)): return [canonical(v) for v in value]
    return value


def differences(a, b, path=''):
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(set(a)|set(b)) for d in differences(a.get(k), b.get(k), path+'/'+k)]
    if isinstance(a, list) and isinstance(b, list) and len(a)==len(b):
        return [d for i,(x,y) in enumerate(zip(a,b)) for d in differences(x,y,path+'/'+str(i))]
    return [] if a==b else [dict(path=path, replay=a, recorded=b)]


def event_value(row):
    value = canonical(row)
    if value['event'] == 'run_summary':
        # Process-wide transport/CPU histograms cannot recur in a network-free,
        # single-portfolio replay. Preserve every trading and clock field.
        value['payload'].pop('latencyMetrics', None)
    return value


class CheckedRecorder:
    def __init__(self, path, clock):
        self.path, self.clock = str(path), clock
        self.source = Path(path).open(encoding='utf-8')
        self.rows, self.count = [], 0

    def record(self, event, symbol, payload):
        expected_line = self.source.readline()
        if not expected_line: raise ValueError('replay produced extra event')
        expected = json.loads(expected_line)
        actual = dict(event=event, symbol=symbol, payload=payload,
                      wallSeconds=self.clock.time(), monoNs=self.clock.perf_counter_ns())
        if event_value(actual) != event_value(expected):
            differing = [k for k in actual if canonical(actual[k]) != canonical(expected.get(k))]
            detail = differences(event_value(actual), event_value(expected))[:8]
            raise ValueError(f'ledger mismatch at event {self.count+1}: {event}/{symbol}, fields={differing}; {json.dumps(detail)}')
        self.count += 1
        if event == 'trade_closed': self.rows.append(deepcopy(dict(event=event,symbol=symbol,payload=payload)))

    def health(self): return dict(mode='e01_disk', pending=0, dropped=0, writerError=None)
    def close(self): self.source.close()
    def exhausted(self):
        if self.source.read(1): raise ValueError('replay omitted recorded events')


def probe_wrapper(strategy, clock, windows, output, typical_range_abs):
    original = strategy.evaluate
    def evaluate(candles, book, trend, **kwargs):
        symbol, mono = kwargs.get('symbol'), clock.perf_counter_ns()
        selected = any(w['symbol'] == symbol and w['startMonoNs'] <= mono <= w['endMonoNs'] for w in windows)
        # Copy only before evaluation; baseline state and the two probes never alias.
        before = deepcopy(strategy) if selected else None
        decision = original(candles, book, trend, **kwargs)
        if not selected: return decision
        d = decision.details
        row = dict(symbol=symbol, monoNs=mono, observedAtMs=kwargs.get('observed_at_ms'),
            action=decision.action.value, reasons=decision.reasons, generation=d.get('zoneGeneration'),
            state=d.get('state'), price=(book.best_bid+book.best_ask)/2,
            details={k:d.get(k) for k in ('breakHoldSeconds','retestSeen','retestHoldSeconds',
                'retestResponseReady','sustainedResponseReady','stopDistancePct','pressureScore',
                'localBreakFlowConfirmed','breakoutAbsorbed','directionalResponseBps')}, probes=[])
        zone = d.get('zone')
        generation = d.get('zoneGeneration')
        if zone and generation and generation[0] in ('support', 'resistance'):
            price = row['price']
            buffer = max((zone['high']-zone['low'])*.45, typical_range_abs(candles)*.20,
                         price*max(book.spread_pct*2, .00025))
            stop = zone['high']+buffer if generation[0]=='support' else zone['low']-buffer
            row.update(structuralStop=stop, structuralStopDistancePct=abs(price-stop)/price,
                       maxStopPct=strategy.max_stop_pct)
        for hold in (0.0, 3.0):
            clone = deepcopy(before)
            clone.hold_without_retest_seconds = hold
            # Instance wrapper isn't installed on the pre-evaluation copy: use the class method.
            test = type(clone).evaluate(clone, candles, book, trend, **kwargs)
            row['probes'].append(dict(holdSeconds=hold, action=test.action.value,
                reasons=test.reasons, entry=test.entry, stop=test.stop, target=test.target,
                stopDistancePct=test.details.get('stopDistancePct')))
        output.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+'\n')
        return decision
    return evaluate


async def run(args):
    # Import only after selecting the capture source, retaining strict read_header guards.
    source_root = Path(args.source_root).resolve()
    if not any(Path(p).resolve() == source_root for p in sys.path):
        sys.path.insert(0, str(source_root))
    from scalp_bot.e01_feed import read_header, observations
    from scalp_bot.e01_comparison import E01Comparison
    from scalp_bot.offline_bootstrap import _RecordedSettings
    from scalp_bot.manifest_validation import fingerprint
    from scalp_bot.strategy.common import typical_range_abs
    root, out = Path(args.directory), Path(args.output_directory)
    out.mkdir(parents=True, exist_ok=False)
    windows = json.loads(Path(args.windows).read_text(encoding='utf-8'))
    recorders = {}
    def factory(name, clock):
        recorder = CheckedRecorder(root/f'{name}-events.jsonl', clock)
        recorders[name] = recorder
        return recorder
    started = time.monotonic()
    try:
        with (root/args.feed_name).open(encoding='utf-8') as feed, (out/'probes.jsonl').open('x',encoding='utf-8') as probes:
            header = read_header(feed)
            pair = E01Comparison(_RecordedSettings(**header['config'], bybit_api_key='', bybit_api_secret=''),
                                 max_events=5_000_000, recorder_factory=factory)
            # Portfolios are independent. Replay baseline only, with every original shared input.
            del pair.engines['candidate']
            engine = pair.engines['baseline']
            strategy = engine.strategies['level_breakout']
            strategy.evaluate = probe_wrapper(strategy, engine.clock, windows, probes, typical_range_abs)
            pair.input_hash = fingerprint(header)
            for row in observations(feed, header):
                await pair.apply(row)
                if pair.count % 50000 == 0:
                    probes.flush()
                    print(f'{pair.count} inputs, {time.monotonic()-started:.1f}s, outputs={recorders["baseline"].count}', flush=True)
            if engine.running or engine.broker.positions or engine.broker.pending_entries:
                raise ValueError('baseline not fully settled by recorded stop')
            trades = pair._trade_ledger(engine)
            recorders['baseline'].exhausted()
            expected = json.loads((root/'result.json').read_text(encoding='utf-8'))
            if (pair.input_hash != expected['sharedInputHash'] or
                    pair.count != expected['eventsConsumed'] or
                    canonical(trades) != canonical(expected['portfolios']['baseline']['trades']) or
                    engine.broker.balance != expected['portfolios']['baseline']['balance']):
                raise ValueError('final replay differs')
            report = dict(status='baseline_replay_matched', inputs=pair.count, inputHash=pair.input_hash,
                comparedOutputEvents=recorders['baseline'].count,
                ignoredFields=['manifestId', 'run_summary.payload.latencyMetrics'],
                baselineNet=engine.broker.balance-engine.config.start_balance,
                elapsedSeconds=time.monotonic()-started, windows=windows,
                scope='baseline on full recorded shared feed; candidate portfolio not replayed',
                probeScope='Independent one-step hold 0s/3s clones of actual pre-evaluation state; no orders or retained hypothetical state',
                profitabilityProven=False)
            (out/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report), flush=True)
            return report
    except BaseException as exc:
        (out/'failure.json').write_text(json.dumps(dict(errorType=type(exc).__name__,error=str(exc))),encoding='utf-8')
        raise
    finally:
        for recorder in recorders.values(): recorder.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory')
    p.add_argument('--feed-name', required=True)
    p.add_argument('--source-root', required=True)
    p.add_argument('--windows', required=True)
    p.add_argument('--output-directory', required=True)
    asyncio.run(run(p.parse_args()))


if __name__ == '__main__': main()
