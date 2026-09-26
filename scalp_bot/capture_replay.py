"""Disk-indexed baseline replay of ordinary UI captures, without network access."""
from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
import sqlite3
import zlib
import msgspec

from .input_journal import validate_input_journal
from .offline_bootstrap import restore_cold_engine
from .offline_scheduler import OfflineScheduledReplay, comparable_events
from .offline_segment import SegmentMismatch


class IndexedInputs:
    disk_backed = True

    def __init__(self, connection, start, stop):
        self.connection, self.start, self.stop = connection, start, stop

    @classmethod
    def build(cls, source, database):
        if database.exists():
            raise FileExistsError(database)
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA cache_size=-8192")
            connection.execute("CREATE TABLE inputs (idx INTEGER PRIMARY KEY, payload BLOB NOT NULL)")
            connection.execute("CREATE TABLE scopes (id INTEGER PRIMARY KEY, end_idx INTEGER NOT NULL)")
            count = 0
            with gzip.open(source, "rb") as stream:
                for line in stream:
                    event = json.loads(line)
                    row = event["payload"]
                    body = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode()
                    connection.execute("INSERT INTO inputs VALUES (?, ?)", (count, zlib.compress(body, 1)))
                    if row["kind"] == "scope" and row["body"]["phase"] == "end":
                        connection.execute("INSERT INTO scopes VALUES (?, ?)", (row["body"]["id"], count))
                    count += 1
                    if count % 10000 == 0:
                        connection.commit()
                connection.commit()
            return cls(connection, 0, count)
        except BaseException:
            connection.close()
            database.unlink()
            raise

    def __len__(self):
        return self.stop - self.start

    def __bool__(self):
        return len(self) > 0

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(len(self))
            if step != 1:
                raise ValueError("only contiguous input windows are supported")
            return IndexedInputs(self.connection, self.start + start, self.start + stop)
        if key < 0:
            key += len(self)
        if not 0 <= key < len(self):
            raise IndexError(key)
        value = self.connection.execute("SELECT payload FROM inputs WHERE idx=?", (self.start + key,)).fetchone()
        return json.loads(zlib.decompress(value[0]))

    def __iter__(self):
        cursor = self.connection.execute("SELECT payload FROM inputs WHERE idx>=? AND idx<? ORDER BY idx", (self.start, self.stop))
        for (payload,) in cursor:
            yield json.loads(zlib.decompress(payload))

    def scope_end(self, start, identity):
        row = self.connection.execute("SELECT end_idx FROM scopes WHERE id=?", (identity,)).fetchone()
        end = row[0] - self.start if row else None
        return end if end is not None and start <= end < len(self) else None


class OutputCheck:
    def __init__(self, path):
        self.stream = path.open(encoding="utf-8")
        self.count = 0
        self.trade_count = 0
        self.summary = None
        self.error = None

    def append(self, actual):
        line = self.stream.readline()
        if not line:
            raise SegmentMismatch("replay produced an extra output event")
        row = json.loads(line)
        expected = {k: row[k] for k in ("event", "symbol", "payload")}
        # SessionRecorder persists tuples and numeric map keys as JSON values.
        # Compare the exact persisted representation, including financial fields.
        persisted = msgspec.json.decode(msgspec.json.encode(actual))
        if comparable_events([persisted]) != comparable_events([expected]):
            self.error = SegmentMismatch(f"output mismatch #{self.count + 1}: {actual['event']}/{actual['symbol']}")
            raise self.error
        self.count += 1
        if actual["event"] == "trade_closed":
            self.trade_count += 1
        if actual["event"] == "run_summary":
            self.summary = actual["payload"]

    def exhausted(self):
        if self.stream.read(1):
            raise SegmentMismatch("replay omitted output events")


def file_sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def capture_file(directory, name):
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError("invalid capture filename")
    return directory / name


async def verify_capture(directory):
    directory = Path(directory).resolve()
    metadata = json.loads((directory / "capture.json").read_text(encoding="utf-8"))
    if metadata["status"] != "sealed":
        raise ValueError("capture is not sealed; stop its server cleanly first")
    source = capture_file(directory, metadata["inputs"])
    session = capture_file(directory, metadata["session"])
    database = directory / "replay-index.sqlite"
    rows = outputs = None
    try:
        integrity = validate_input_journal(source)
        if (integrity["structuralStatus"] != "checks_passed" or integrity["boundRuns"] != 1
                or not integrity["captureManifestChecked"] or not integrity["policySnapshotChecked"]):
            raise ValueError("input journal integrity check failed")
        print("Input chain checked; indexing the capture on disk...", flush=True)
        rows = IndexedInputs.build(source, database)
        prefix = list(rows[:3])
        manifest = prefix[1]['body']['manifest']
        if manifest['code']['sourceSha256'] != metadata['sourceSha256']:
            raise ValueError('capture metadata source differs from input manifest')
        engine = restore_cold_engine(prefix)
        outputs = OutputCheck(session)
        print("Replaying the recorded engine and checking every output...", flush=True)
        result = await OfflineScheduledReplay(engine).apply(rows[3:], event_sink=outputs)
        outputs.exhausted()
        if (not result["serviceLifecycleMatched"] or engine.running or engine.broker.positions
                or engine.broker.pending_entries or outputs.summary is None
                or outputs.trade_count != engine.broker.total_closed_trades
                or engine.broker.balance != outputs.summary["balance"]):
            raise ValueError("replay ended with an incomplete portfolio")
        report = dict(status="baseline_replay_matched", profile=metadata["profile"],
            sourceSha256=metadata["sourceSha256"],
            files={path.name: file_sha256(path) for path in (source, session, directory / "source-at-capture.zip")},
            inputs=len(rows), outputEvents=outputs.count, closedTrades=outputs.trade_count,
            balance=engine.broker.balance, summary=outputs.summary, integrity=integrity,
            isolatedStrategySimulationPerformed=False, profitabilityProven=False)
        (directory / "capture-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    except BaseException as exc:
        if outputs is not None and outputs.error is not None:
            exc = outputs.error
        (directory / "capture-check.json").write_text(json.dumps(dict(status="rejected", errorType=type(exc).__name__, error=str(exc)), ensure_ascii=False, indent=2), encoding="utf-8")
        raise exc
    finally:
        if outputs is not None:
            outputs.stream.close()
        if rows is not None:
            rows.connection.close()
            database.unlink()
