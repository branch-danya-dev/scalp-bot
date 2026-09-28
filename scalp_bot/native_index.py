"""Versioned disk-backed full-population v5 validation and indexed access.

The index is disposable and built exclusively from a verified raw stream. Task
state and rows live on disk; no fixture row cap, sampling, or manual production
coverage flag exists. Structural completeness alone is not production coverage.
"""
from collections.abc import Mapping, MutableMapping, Sequence
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

import msgspec

from .manifest_validation import fingerprint
from .native_v5 import MAX_BYTES, SCHEMA, NativeTapeError, _check_provenance, validate_events

INDEX_SCHEMA = "native-causal-v5-index-1"


class _Tasks(MutableMapping):
    def __init__(self, db): self.db = db
    def __getitem__(self, key):
        row = self.db.execute("SELECT value FROM tasks WHERE id=?", (key,)).fetchone()
        if row is None: raise KeyError(key)
        return json.loads(row[0])
    def __setitem__(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?)", (key, json.dumps(value)))
    def __delitem__(self, key): self.db.execute("DELETE FROM tasks WHERE id=?", (key,))
    def __iter__(self):
        for row in self.db.execute("SELECT id FROM tasks ORDER BY rowid"): yield row[0]
    def __len__(self): return self.db.execute("SELECT count(*) FROM tasks").fetchone()[0]
    def values(self):
        for row in self.db.execute("SELECT value FROM tasks ORDER BY rowid"): yield json.loads(row[0])


class _Events(Sequence):
    def __init__(self, db, count): self.db, self.count = db, count
    def __len__(self): return self.count
    def __iter__(self):
        for row in self.db.execute("SELECT value FROM events ORDER BY sequence"):
            yield json.loads(row[0])
    def __getitem__(self, index):
        if isinstance(index, slice):
            raise NativeTapeError("full tape slicing would materialize the population; use indexed iteration")
        if index < 0: index += self.count
        if not 0 <= index < self.count: raise IndexError(index)
        return json.loads(self.db.execute("SELECT value FROM events WHERE sequence=?", (index+1,)).fetchone()[0])


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024*1024): digest.update(chunk)
    return digest.hexdigest()


class IndexedNativeTape:
    """Call close() after all consumers; no open-existing/unverified index path."""
    def __init__(self, raw_path, index_path, *, expected_provenance, external_inventory=None,
                 expected_inventory_sha256=None, artifact_hashes=None):
        self.db = None
        index_path = Path(index_path)
        # Exclusive creation: failed indexes are retained, never overwritten.
        with index_path.open("xb"): pass
        self.db = sqlite3.connect(index_path)
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE events (sequence INTEGER PRIMARY KEY, task TEXT, module TEXT, kind TEXT, value TEXT)")
        self.db.execute("CREATE INDEX event_task ON events(task, sequence)")
        try:
            self._build(raw_path, expected_provenance)
            inventory = external_inventory or []
            if expected_inventory_sha256 is not None and fingerprint(inventory) != expected_inventory_sha256:
                raise NativeTapeError("external raw inventory digest mismatch")
            for item in inventory:
                if set(item) != {"path", "bytes", "sha256"} or Path(item["path"]).stat().st_size != item["bytes"] or _file_hash(item["path"]) != item["sha256"]:
                    raise NativeTapeError("external raw inventory content mismatch")
            for item in (artifact_hashes or {}).values():
                if set(item) != {"path", "sha256"} or _file_hash(item["path"]) != item["sha256"]:
                    raise NativeTapeError("source/model/probe artifact content mismatch")
            self.counts = validate_events(self.events, self.header["capture_id"], task_store=_Tasks(self.db))
            self.db.commit()
            self.receipt = dict(schema=INDEX_SCHEMA, rawSha256=_file_hash(raw_path),
                populationSha256=self.population_hash, rows=len(self.events),
                provenance=self.provenance, externalInventorySha256=fingerprint(inventory),
                artifacts=artifact_hashes or {}, completeChain=True, completeLifecycle=True,
                productionCoverage=False,
                missingProof=["complete production scheduler/clock/actor execution and semantic acceptance"])
        except BaseException:
            self.close()
            raise

    @property
    def provenance(self): return self.header["provenance"]

    def _build(self, raw_path, expected):
        previous, header, footer, count = None, None, None, 0
        population = hashlib.sha256(b"[")
        try:
            with gzip.open(raw_path, "rb") as stream:
                while raw := stream.readline(MAX_BYTES+1):
                    if len(raw) > MAX_BYTES: raise NativeTapeError("oversized v5 row")
                    row = msgspec.json.decode(raw)
                    if not isinstance(row, dict) or row.get("schema") != SCHEMA:
                        raise NativeTapeError("unsupported native stream version")
                    if footer is not None or row.get("previousHash") != previous or row.get("hash") != fingerprint({k:v for k,v in row.items() if k != "hash"}):
                        raise NativeTapeError("v5 full hash chain mismatch or suffix after footer")
                    previous = row["hash"]
                    if header is None:
                        if row.get("kind") != "header": raise NativeTapeError("missing native header")
                        header = row
                    elif row.get("kind") == "footer":
                        footer = row
                    else:
                        count += 1
                        if row.get("sequence") != count: raise NativeTapeError("native dispatch sequence gap")
                        self.db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                            (count, row.get("task_id"), row.get("module_id"), row.get("kind"), raw))
                        population.update((b"," if count > 1 else b"")+json.dumps(row["hash"]).encode())
        except (OSError, EOFError, msgspec.DecodeError, sqlite3.Error) as exc:
            raise NativeTapeError("corrupt or incomplete indexed native stream") from exc
        if header is None or footer is None: raise NativeTapeError("missing v5 header/footer suffix")
        _check_provenance(header.get("provenance"))
        if header["provenance"] != expected:
            raise NativeTapeError("exact source/config/runtime/schema provenance mismatch")
        if (set(header) != {"schema", "kind", "capture_id", "provenance", "coverage", "productionCoverage", "ordering", "capacityBytes", "previousHash", "hash"}
                or not isinstance(header["capture_id"], str) or not header["capture_id"]
                or header["coverage"] != "explicit_owned_tasks_only" or header["productionCoverage"] is not False
                or header["ordering"] != "shared-lock-at-observation"
                or type(header["capacityBytes"]) is not int or not 1024 <= header["capacityBytes"] <= MAX_BYTES):
            raise NativeTapeError("unsupported v5 coverage contract/header")
        if (set(footer) != {"schema", "kind", "count", "lastSequence", "valid", "health", "previousHash", "hash"}
                or footer.get("valid") is not True or type(footer.get("count")) is not int
                or type(footer.get("lastSequence")) is not int
                or footer["count"] != count or footer["lastSequence"] != count
                or not isinstance(footer.get("health"), dict)
                or any(footer["health"].get(key) != 0 for key in ("rejected", "errorCode", "pendingBytes"))):
            raise NativeTapeError("invalid or incomplete v5 footer")
        population.update(b"]")
        self.header, self.footer, self.population_hash = header, footer, population.hexdigest()
        self.events = _Events(self.db, count)

    def task_events(self, task_id):
        for row in self.db.execute("SELECT value FROM events WHERE task=? ORDER BY sequence", (task_id,)):
            yield json.loads(row[0])

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
