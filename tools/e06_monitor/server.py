"""Read-only paired-capture monitor. Deliberately independent of scalp_bot imports.

Run with --capture pointing at the launcher's output directory, even before it
exists. No exchange connections, trading engine, dotenv or journal writes.
"""
from __future__ import annotations

import argparse
from collections import deque
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
from threading import Event, Lock, Thread
import time
from urllib.parse import urlsplit


PORTFOLIOS = ('baseline', 'candidate')
CHUNK = 2 * 1024 * 1024
MAX_LINE = 8 * 1024 * 1024
EVENT = re.compile(rb'^\s*\{\s*"event"\s*:\s*"([^"\\]+)"')
EVENTS = {'bot_started', 'trade_opened', 'trade_closed', 'partial_take',
          'research_frame', 'trade_scaled_in'}
POSITION_FIELDS = ('symbol', 'strategy', 'side', 'entry', 'quantity', 'notional',
                   'stop', 'target', 'opened_at', 'last_price', 'unrealized_pnl')
TRADE_FIELDS = ('symbol', 'strategy', 'side', 'entry', 'exit', 'netPnl', 'fees',
                'reason', 'openedAt', 'closedAt')


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def stat_size(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def tail_row(path, limit=CHUNK):
    """Only complete records: an in-flight final line is never decoded."""
    try:
        with path.open('rb') as stream:
            size = stream.seek(0, 2)
            start = max(0, size - limit)
            stream.seek(start)
            data = stream.read(limit)
    except FileNotFoundError:
        return None
    lines = data.split(b'\n')[:-1]
    if start:
        lines = lines[1:]
    for line in reversed(lines):
        if line.strip():
            return json.loads(line)
    return None


class Cursor:
    """Bounded incremental reads; retains partial UTF-8 records until newline."""
    def __init__(self, path):
        self.path, self.offset, self.pending = path, 0, b''
        self.identity = None

    def read(self, consume, budget=CHUNK):
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            if self.offset:
                raise ValueError(f'{self.path.name}: journal disappeared; restart the monitor')
            return
        identity = (stat.st_dev, stat.st_ino)
        if (self.identity is not None and identity != self.identity) or stat.st_size < self.offset:
            raise ValueError(f'{self.path.name}: journal replaced/truncated; restart the monitor')
        self.identity = identity
        with self.path.open('rb') as stream:
            stream.seek(self.offset)
            data = stream.read(budget)
        self.offset += len(data)
        lines = (self.pending + data).split(b'\n')
        self.pending = lines.pop()
        if len(self.pending) > MAX_LINE:
            raise ValueError(f'{self.path.name}: oversized unfinished record')
        for line in lines:
            if line.strip():
                consume(line)

    def progress(self):
        size = stat_size(self.path)
        consumed = self.offset - len(self.pending)
        return dict(bytesRead=consumed, bytesTotal=size,
                    pendingBytes=len(self.pending),
                    complete=self.path.exists() and self.offset == size,
                    percent=round(100 * consumed / size, 1) if size else 0)


class Ledger:
    def __init__(self, path):
        self.cursor = Cursor(path)
        self.started = None
        self.positions = {}
        self.closed = deque(maxlen=100)
        self.closed_count = 0
        self.closed_net = 0.0
        self.closed_fees = 0.0
        self.last_at = None

    def consume(self, line):
        match = EVENT.match(line)
        if match and match.group(1).decode() not in EVENTS:
            return
        row = json.loads(line)
        event, payload = row.get('event'), row.get('payload', {})
        if event not in EVENTS:
            return
        stamp = row.get('wallSeconds')
        self.last_at = stamp
        symbol = row.get('symbol') or payload.get('symbol')
        if event == 'bot_started':
            self.started = dict(startedAt=payload.get('startedAt'),
                                monoNs=row.get('monoNs'),
                                durationSeconds=payload.get('durationSeconds'),
                                startBalance=payload.get('config', {}).get('startBalance'))
        elif event in ('research_frame', 'trade_opened', 'trade_scaled_in'):
            if 'position' in payload and symbol:
                position = payload['position']
                if position:
                    self.positions[symbol] = dict(
                        **{k: position.get(k) for k in POSITION_FIELDS}, observedAt=stamp)
                else:
                    self.positions.pop(symbol, None)
        elif event == 'partial_take' and symbol in self.positions:
            position = self.positions[symbol]
            for source, target in (('remainingQuantity', 'quantity'), ('remainingNotional', 'notional'),
                                   ('newStop', 'stop'), ('newTarget', 'target')):
                position[target] = payload.get(source)
            # The previous mark was for a different quantity. Wait for a frame.
            position['unrealized_pnl'] = None
            position['observedAt'] = stamp
        elif event == 'trade_closed':
            self.positions.pop(symbol, None)
            self.closed.append({k: payload.get(k) for k in TRADE_FIELDS})
            self.closed_count += 1
            self.closed_net += payload['netPnl']
            self.closed_fees += payload['fees']

    def snapshot(self):
        progress = self.cursor.progress()
        return dict(history=progress, positions=list(self.positions.values()) if progress['complete'] else [],
                    closedTrades=list(reversed(self.closed)), closedCount=self.closed_count,
                    closedNet=self.closed_net, closedFees=self.closed_fees, ledgerAt=self.last_at)


class Phase:
    def __init__(self, path):
        self.path = path
        self.equity = Cursor(path / 'equity.jsonl')
        self.ledgers = {name: Ledger(path / f'{name}-events.jsonl') for name in PORTFOLIOS}
        self.latest = None
        self.chart = []
        self.chart_interval = 5
        self.tail_size = None

    def consume_equity(self, line):
        row = json.loads(line)
        if not self.latest or row['monoNs'] >= self.latest['monoNs']:
            self.latest = row
        point = dict(time=row['wallSeconds'], **{
            name: row['portfolios'][name]['equity'] for name in PORTFOLIOS})
        if not self.chart or point['time'] >= self.chart[-1]['time'] + self.chart_interval:
            self.chart.append(point)
        if len(self.chart) > 1500:
            self.chart = self.chart[::2]
            self.chart_interval *= 2

    def update(self):
        size = stat_size(self.equity.path)
        if size != self.tail_size and size > self.equity.offset + CHUNK:
            row = tail_row(self.equity.path)
            if row:
                self.latest = row
        self.tail_size = size
        self.equity.read(self.consume_equity)
        for ledger in self.ledgers.values():
            ledger.cursor.read(ledger.consume)

    def snapshot(self):
        latest = self.latest or {}
        started = next((l.started for l in self.ledgers.values() if l.started), {})
        stamp = latest.get('wallSeconds')
        start_at = number(started.get('startedAt'))
        duration = number(started.get('durationSeconds'))
        start_mono, last_mono = number(started.get('monoNs')), number(latest.get('monoNs'))
        elapsed = max(0, (last_mono - start_mono) / 1e9) if start_mono is not None and last_mono is not None else None
        start_balance = number(started.get('startBalance'))
        portfolios = {}
        for name, ledger in self.ledgers.items():
            recorded = latest.get('portfolios', {}).get(name, {})
            equity, balance = number(recorded.get('equity')), number(recorded.get('balance'))
            portfolios[name] = dict(**ledger.snapshot(), **recorded,
                net=equity - start_balance if equity is not None and start_balance is not None else None,
                unrealized=equity - balance if equity is not None and balance is not None else None)
        chart = list(self.chart)
        # Do not bridge over a history backlog with a misleading straight line.
        if self.equity.progress()['complete'] and stamp is not None and (not chart or stamp > chart[-1]['time']):
            chart.append(dict(time=stamp, **{n: latest['portfolios'][n]['equity'] for n in PORTFOLIOS}))
        return dict(latestAt=stamp, startedAt=start_at, durationSeconds=duration,
                    elapsedSeconds=elapsed, startBalance=start_balance, portfolios=portfolios,
                    chart=chart, equityHistory=self.equity.progress())


class Monitor:
    def __init__(self, capture):
        self.capture = Path(capture).resolve()
        self.phase = None
        self.cache = {}
        self.error = None
        self.lock = Lock()
        self.stop = Event()
        self.state = dict(capture=str(self.capture), status='waiting', phases=[], portfolios={}, chart=[])

    def marker(self, path):
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        identity = (stat.st_mtime_ns, stat.st_size)
        cached = self.cache.get(path)
        if cached and cached[0] == identity:
            return cached[1]
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            return None  # Writer may not have finished/closed its marker yet.
        self.cache[path] = (identity, value)
        return value

    def update(self, now=None):
        now = time.time() if now is None else now
        root = self.capture
        direct = (root / 'equity.jsonl').exists()
        phase_names = ['capture'] if direct else ['smoke', 'smoke-replay', 'independent']
        phases = []
        for name in phase_names:
            folder = root if direct else root / name
            failure = self.marker(folder / 'failure.json')
            result = self.marker(folder / 'result.json')
            phases.append(dict(name=name, exists=folder.is_dir(),
                               status='failed' if failure is not None else 'recorded' if result is not None
                               else 'present' if folder.is_dir() else 'pending',
                               audit=(folder / 'audit.json').exists()))
        available = [p for p in phases if p['exists']]
        active = available[-1]['name'] if available else None
        data_name = 'capture' if direct else 'independent' if (root / 'independent').is_dir() else 'smoke'
        folder = root if direct else root / data_name
        if folder.is_dir() and (self.phase is None or self.phase.path != folder):
            self.phase = Phase(folder)
        if self.phase is not None and not self.error:
            try:
                self.phase.update()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.error = f'{type(exc).__name__}: {exc}'
        snapshot = self.phase.snapshot() if self.phase else dict(portfolios={}, chart=[])
        latest = snapshot.get('latestAt')
        age = max(0, now - latest) if latest is not None else None
        failed = next((p for p in phases if p['status'] == 'failed'), None)
        failure = self.marker(root / 'failure.json')
        if failure is None and failed:
            failure = self.marker((root if direct else root / failed['name']) / 'failure.json')
        completed = self.marker(root / 'completed.json') is not None
        result_present = self.marker(folder / 'result.json') is not None
        if failure is not None:
            status = 'failed'
        elif completed or direct and result_present:
            status = 'completed'
        elif self.error:
            status = 'monitor_error'
        elif active == 'smoke-replay':
            status = 'replay_recorded' if phases[1]['status'] == 'recorded' else 'replay'
        elif result_present:
            status = 'recorded'
        elif latest is None:
            status = 'warming' if active else 'waiting'
        elif age > 30:
            status = 'stale'
        elif snapshot.get('startedAt') is None:
            status = 'warming'
        else:
            status = 'recording'
        protocol = self.marker(root / 'protocol.json') or {}
        experiment = protocol.get('protocol', {}).get('experiment', 'E06' if not direct else 'paired')
        state = dict(**snapshot, capture=str(root), phase=active, dataPhase=data_name, phases=phases,
                     status=status, experiment=experiment, ageSeconds=age, serverTime=now,
                     failure=failure, monitorError=self.error, readOnly=True)
        with self.lock:
            self.state = state

    def snapshot(self):
        with self.lock:
            return deepcopy(self.state)

    def run(self):
        while not self.stop.is_set():
            try:
                self.update()
            except Exception as exc:
                with self.lock:
                    self.state.update(status='monitor_error', monitorError=f'{type(exc).__name__}: {exc}')
            self.stop.wait(.5)


def make_server(monitor, port=8001):
    static = Path(__file__).parent / 'static'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Loopback binding plus Host validation prevents DNS rebinding reads.
            if self.headers.get('Host') not in (f'127.0.0.1:{self.server.server_port}',
                                                f'localhost:{self.server.server_port}'):
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            if path == '/api/state':
                body = json.dumps(monitor.snapshot(), ensure_ascii=False, allow_nan=False).encode('utf-8')
                content_type = 'application/json; charset=utf-8'
            else:
                assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                          '/styles.css': ('styles.css', 'text/css')}
                if path not in assets:
                    self.send_error(404)
                    return
                filename, content_type = assets[path]
                body = (static / filename).read_bytes()
                content_type += '; charset=utf-8'
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8001)
    args = parser.parse_args()
    monitor = Monitor(args.capture)
    try:
        server = make_server(monitor, args.port)
    except OSError as exc:
        parser.exit(1, f'Cannot open UI port {args.port}: {exc}. Choose another --port; 8000 is reserved for the main UI.\n')
    thread = Thread(target=monitor.run, daemon=True)
    thread.start()
    print(f'Paper monitor: http://127.0.0.1:{server.server_port}/ | {monitor.capture}', flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        monitor.stop.set()
        server.server_close()
        thread.join(timeout=3)


if __name__ == '__main__':
    main()
