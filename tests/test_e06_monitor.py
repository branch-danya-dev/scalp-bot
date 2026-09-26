"""Monitor correctness without starting a trading engine or exchange connection."""
import hashlib
import importlib.util
import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('e06_monitor', ROOT / 'tools/e06_monitor/server.py')
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def append(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('ab') as f:
        f.write(json.dumps(row).encode() + b'\n')


def event(name, payload, symbol=None, stamp=100, mono=None):
    return dict(event=name, symbol=symbol, payload=payload, wallSeconds=stamp,
                monoNs=int((mono if mono is not None else stamp) * 1e9))


def phase(path, start=100, latest=110, balance=999, equity=1002):
    for name in ui.PORTFOLIOS:
        append(path / f'{name}-events.jsonl', event('bot_started', dict(
            startedAt=start, durationSeconds=43200, config=dict(startBalance=1000)), stamp=start))
    append(path / 'equity.jsonl', dict(wallSeconds=latest, monoNs=int(latest*1e9), portfolios={
        name: dict(balance=balance, equity=equity, openPositions=1, marketHealth=dict(ready=True))
        for name in ui.PORTFOLIOS}))


def test_partial_utf8_and_bounded_incremental_reads(tmp_path):
    path = tmp_path / 'events.jsonl'
    first = json.dumps(dict(text='запись'), ensure_ascii=False).encode()
    path.write_bytes(first[:12])
    cursor = ui.Cursor(path)
    rows = []
    cursor.read(lambda line: rows.append(json.loads(line)), budget=5)
    assert not rows
    assert cursor.offset == 5
    with path.open('ab') as f:
        f.write(first[12:] + b'\n{"second":2}\n{"unfinished":')
    while cursor.offset < path.stat().st_size:
        cursor.read(lambda line: rows.append(json.loads(line)), budget=5)
    assert rows == [dict(text='запись'), dict(second=2)]
    assert cursor.progress()['complete']  # A partial last line is normal while recording.
    assert cursor.progress()['pendingBytes'] > 0
    cursor.read(lambda line: rows.append(json.loads(line)))
    assert len(rows) == 2
    with path.open('ab') as f:
        f.write(b'3}\n')
    cursor.read(lambda line: rows.append(json.loads(line)))
    assert rows[-1] == dict(unfinished=3)
    assert ui.tail_row(path) == dict(unfinished=3)
    path.write_bytes(b'{}\n')
    with pytest.raises(ValueError, match='truncated'):
        cursor.read(lambda _: None)


def test_tail_ignores_partial_record(tmp_path):
    p = tmp_path / 'equity.jsonl'
    p.write_bytes(b'{"a":1}\n{"b":2}\n{"bad":')
    assert ui.tail_row(p) == dict(b=2)
    assert ui.tail_row(p, limit=18) == dict(b=2)


def test_waiting_phase_transition_failure_and_stale(tmp_path):
    root = tmp_path / 'new-capture'
    monitor = ui.Monitor(root)
    monitor.update(now=110)
    assert monitor.snapshot()['status'] == 'waiting'
    assert not root.exists()  # Viewing a future capture must not reserve its output.
    phase(root / 'smoke')
    monitor.update(now=115)
    assert monitor.snapshot()['status'] == 'recording'
    monitor.update(now=200)
    assert monitor.snapshot()['status'] == 'stale'
    write(root / 'smoke/result.json', {})
    monitor.update(now=200)
    assert monitor.snapshot()['status'] == 'recorded'
    (root / 'smoke-replay').mkdir()
    monitor.update(now=200)
    assert monitor.snapshot()['status'] == 'replay'
    write(root / 'smoke-replay/result.json', {})
    monitor.update(now=200)
    assert monitor.snapshot()['status'] == 'replay_recorded'
    phase(root / 'independent', start=300, latest=310, equity=1000)
    monitor.update(now=310)
    s = monitor.snapshot()
    assert s['status'] == 'recording' and s['phase'] == 'independent'
    assert s['portfolios']['baseline']['net'] == 0
    assert len(s['chart']) == 1  # Smoke history is not mixed into the new portfolio.
    write(root / 'completed.json', dict(status='capture_completed'))
    monitor.update(now=320)
    assert monitor.snapshot()['status'] == 'completed'
    write(root / 'independent/failure.json', dict(error='transport failed'))
    monitor.update(now=320)
    assert monitor.snapshot()['status'] == 'failed'
    assert monitor.snapshot()['failure']['error'] == 'transport failed'


def test_financial_metrics_partial_exit_and_full_cycle_net(tmp_path):
    phase(tmp_path)
    p = tmp_path / 'baseline-events.jsonl'
    position = dict(symbol='BTCUSDT', side='long', quantity=2, notional=200,
                    entry=100, unrealized_pnl=2)
    append(p, event('trade_opened', dict(position=position), 'BTCUSDT', 101))
    append(p, event('partial_take', dict(remainingQuantity=1, remainingNotional=100,
                   newStop=100, newTarget=120, netPnl=5), 'BTCUSDT', 102))
    m = ui.Monitor(tmp_path)
    m.update(now=110)
    s = m.snapshot()
    b = s['portfolios']['baseline']
    assert b['net'] == 2 and b['unrealized'] == 3
    assert b['positions'][0]['quantity'] == 1
    assert b['positions'][0]['unrealized_pnl'] is None
    assert b['closedCount'] == 0  # A partial fill is not another closed trade.
    append(p, event('trade_closed', dict(symbol='BTCUSDT', netPnl=7, fees=1, closedAt=112), 'BTCUSDT', 112))
    m.update(now=115)
    m.update(now=116)
    b = m.snapshot()['portfolios']['baseline']
    assert not b['positions'] and b['closedCount'] == 1
    assert b['closedNet'] == 7 and b['closedFees'] == 1


def test_monotonic_progress_and_unfinished_marker(tmp_path):
    phase(tmp_path)
    append(tmp_path / 'equity.jsonl', dict(wallSeconds=90, monoNs=int(120e9), portfolios={
        n: dict(balance=1000, equity=1000, openPositions=0) for n in ui.PORTFOLIOS}))
    (tmp_path / 'failure.json').write_text('{"error":', encoding='utf-8')
    m = ui.Monitor(tmp_path)
    m.update(now=95)
    assert m.snapshot()['elapsedSeconds'] == 20
    assert m.snapshot()['status'] == 'recording'


def test_backlog_hides_incomplete_positions_and_preserves_latest_equity(tmp_path, monkeypatch):
    phase(tmp_path)
    monkeypatch.setattr(ui, 'CHUNK', 1)  # Tail must deliver the newest equity even before history catches up.
    read = ui.Cursor.read
    monkeypatch.setattr(ui.Cursor, 'read', lambda self, consume, budget=1: read(self, consume, budget=budget))
    m = ui.Monitor(tmp_path)
    m.update(now=110)
    assert m.snapshot()['portfolios']['baseline']['equity'] == 1002
    assert not m.snapshot()['equityHistory']['complete']
    ledger = ui.Ledger(tmp_path / 'baseline-events.jsonl')
    ledger.positions['X'] = dict(symbol='X')
    ledger.cursor.read(ledger.consume, budget=1)
    assert not ledger.snapshot()['history']['complete']
    assert not ledger.snapshot()['positions']


def test_chart_memory_bounded(tmp_path):
    p = ui.Phase(tmp_path)
    for i in range(5000):
        row = dict(wallSeconds=i*5, monoNs=i, portfolios={n: dict(equity=1000+i) for n in ui.PORTFOLIOS})
        p.consume_equity(json.dumps(row).encode())
    assert len(p.chart) <= 1500
    assert p.chart[0]['time'] == 0
    assert p.latest['wallSeconds'] == 4999*5


def test_read_only_http_assets_and_host_restriction(tmp_path):
    phase(tmp_path)
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    monitor = ui.Monitor(tmp_path)
    monitor.update(now=110)
    server = ui.make_server(monitor, 0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base + '/api/state') as response:
            assert json.load(response)['readOnly'] is True
        for asset in ('/', '/app.js', '/styles.css'):
            with urlopen(base + asset) as response:
                assert response.status == 200
                assert response.headers['Cache-Control'] == 'no-store'
        for request, code in ((Request(base + '/api/start', data=b'{}'), 501),
                              (Request(base + '/../../.env'), 404),
                              (Request(base + '/api/state', headers={'Host': 'evil.example'}), 403)):
            with pytest.raises(HTTPError) as exc:
                urlopen(request)
            assert exc.value.code == code
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    assert before == after


def test_monitor_does_not_import_or_modify_frozen_capture():
    import ast
    tree = ast.parse((ROOT / 'tools/e06_monitor/server.py').read_text())
    imports = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    imports += [item.name for node in ast.walk(tree) if isinstance(node, ast.Import) for item in node.names]
    assert not any(name and name.startswith('scalp_bot') for name in imports)
