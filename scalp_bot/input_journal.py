"""Experimental ordered input capture. Structural integrity is NOT replay parity.

Rows share SessionRecorder's bounded queue. A missing row breaks the sequence
and hash chain; a missing suffix leaves no footer. Neither is silently accepted.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import gzip
import json
import math
import os
from pathlib import Path
from typing import Callable

from .manifest_validation import fingerprint, check_manifest, valid_digest
from .runtime_clock import RuntimeClock


SCHEMA = "replay-input-v4"
V3_SCHEMA = "replay-input-v3"
V2_SCHEMA = "replay-input-v2"
LEGACY_SCHEMA = "replay-input-v1"
EVENT = "replay_input"
LEGACY_KINDS = frozenset({"header", "market_message", "clock_sample", "clock_error",
                   "callback", "control", "footer"})
V2_KINDS = LEGACY_KINDS | {"bootstrap", "rest_context", "scanner_result", "source_error",
                        "symbol_lifecycle", "manifest", "run_end"}
V3_KINDS = V2_KINDS | {"clock_read", "scope", "scheduler"}
KINDS = V3_KINDS | {"dispatch", "external", "transport", "policy_snapshot", "service", "context_await", "source_await"}
LEGACY_MISSING_COVERAGE = (
    "bootstrap_and_rest_context", "scanner_and_symbol_lifecycle",
    "every_runtime_clock_read", "scheduler_dispatch_and_completion", "manifest_binding",
)
V2_MISSING_COVERAGE = ("every_runtime_clock_read", "scheduler_dispatch_and_completion",
                    "transport_reconnect_state", "research_policy_artifact")
V3_MISSING_COVERAGE = ("periodic_and_external_dispatch", "transport_reconnect_state", "research_policy_artifact")
MISSING_COVERAGE = ("offline_replay_parity", "capture_load_validation")
COVERAGE = {LEGACY_SCHEMA: LEGACY_MISSING_COVERAGE, V2_SCHEMA: V2_MISSING_COVERAGE,
            V3_SCHEMA: V3_MISSING_COVERAGE, SCHEMA: MISSING_COVERAGE}
SCOPE_NAMES = {"evaluate", "arbiter", "event_evaluation", "market_message", "bootstrap_apply",
               "rest_context_apply", "scan", "clock_sync", "start_request", "stop", "toggle_strategy"}
V4_SCOPE_NAMES = SCOPE_NAMES | {"clock_loop", "scanner_loop", "context_loop", "arbiter_loop",
                               "paper_timer", "public_state", "market_health"}


def _book_state(body):
    return (isinstance(body, dict) and set(body) == {"depth", "synced", "updateId", "seq", "bids", "asks"}
        and type(body["synced"]) is bool and _uint(body["depth"]) and body["depth"] > 0
        and all(body[k] is None or _uint(body[k]) for k in ("updateId", "seq"))
        and all(isinstance(body[k], list) and all(isinstance(row, list) and len(row) == 2
            and all(_finite(v) for v in row) for row in body[k]) for k in ("bids", "asks")))


def _policy_snapshot(body):
    if set(body) != {"mode", "manifest", "contentHash", "sourceFileSha256"}:
        return False
    if body["mode"] not in ("off", "shadow", "enforce"):
        return False
    if body["sourceFileSha256"] is not None and not valid_digest(body["sourceFileSha256"]):
        return False
    try:
        if fingerprint(body["manifest"]) != body["contentHash"]:
            return False
        if body["mode"] == "off":
            return body["manifest"] is None
        from .research_policy import ResearchPolicyRuntime
        ResearchPolicyRuntime(mode=body["mode"], manifest=body["manifest"])
        return True
    except (ValueError, TypeError, AttributeError, KeyError):
        return False
CANDLE_FIELDS = frozenset("start_ms open high low close volume turnover confirmed".split())
CANDIDATE_FIELDS = frozenset("symbol turnover_24h change_24h last_price volume_24h spread_bps top_book_notional_usd mark_price funding_rate next_funding_time_ms trade_count_24h trade_count_source correlation_1h_btc activity_change activity_turnover activity_burst_ratio activity_compression_ratio activity_expansion_ratio activity_move_spent_ratio activity_level_proximity_score opportunity_readiness activity_score activity_rank".split())
INSTRUMENT_FIELDS = frozenset("symbol status tick_size qty_step min_order_qty min_notional_value max_order_qty max_market_order_qty funding_interval_minutes max_leverage".split())
FEE_FIELDS = frozenset("symbol maker_fee_rate taker_fee_rate source".split())


def _public_record(value, fields, strings, nullable=frozenset()):
    return (isinstance(value, dict) and set(value) == fields
            and all((v is None and k in nullable) or
                    (isinstance(v, str) and bool(v) if k in strings else _finite(v))
                    for k, v in value.items()))


def _candles(value):
    return isinstance(value, list) and all(isinstance(c, dict) and set(c) == CANDLE_FIELDS
        and _uint(c["start_ms"]) and type(c["confirmed"]) is bool
        and all(_finite(c[k]) for k in CANDLE_FIELDS - {"start_ms", "confirmed"}) for c in value)
MARKET_FIELDS = ("topic", "type", "ts", "cts", "data", "receipt_wall_ns",
                 "receipt_mono_ns", "parsed_mono_ns", "processor_started_mono_ns",
                 "event_id", "queue_depth", "queue_lag_ms")


def _finite(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _uint(value: object) -> bool:
    return type(value) is int and value >= 0


def valid_body(kind: str, symbol: object, body: object, schema: str = SCHEMA) -> bool:
    if not isinstance(body, dict) or (symbol is not None and not isinstance(symbol, str)):
        return False
    if kind == "header":
        return (symbol is None and body == {"coverage": "experimental_partial",
                "missingCoverage": list(COVERAGE[schema]), "parityReady": False})
    if schema == SCHEMA:
        if kind == "source_await":
            fields = {"id", "source", "phase"}
            if body.get("phase") == "failed":
                fields |= {"errorType", "errorMessage"}
                if not (isinstance(body.get("errorType"), str) and body["errorType"]
                        and isinstance(body.get("errorMessage"), str)):
                    return False
            return (set(body) == fields
                and _uint(body["id"]) and body["id"] > 0
                and body["phase"] in ("wait", "ready", "cancelled", "raised", "failed")
                and (symbol is None if body["source"] in ("scanner", "clock") else
                     body["source"] == "bootstrap" and isinstance(symbol, str) and bool(symbol)))
        if kind == "context_await":
            if not _uint(body.get("id")) or body["id"] == 0:
                return False
            if body.get("phase") == "wait":
                return (symbol is None and set(body) == {"id", "phase", "symbols"}
                    and isinstance(body["symbols"], list) and bool(body["symbols"])
                    and all(isinstance(s, str) and bool(s) for s in body["symbols"])
                    and len(set(body["symbols"])) == len(body["symbols"]))
            return (set(body) == {"id", "phase"} and
                (isinstance(symbol, str) and bool(symbol) if body.get("phase") == "request"
                 else symbol is None and body.get("phase") in ("ready", "cancelled")))
        if kind == "dispatch":
            return (symbol is None and set(body) == {"source", "phase", "id", "delay"}
                and body["source"] in ("clock", "scanner", "context", "arbiter", "paper_timer")
                and body["phase"] in ("wait", "wake", "cancelled") and _uint(body["id"]) and body["id"] > 0
                and _finite(body["delay"]) and body["delay"] >= 0)
        if kind == "external":
            return (symbol is None and set(body) == {"method", "selectedSymbol"}
                and body["method"] == "public_state"
                and (body["selectedSymbol"] is None or isinstance(body["selectedSymbol"], str)))
        if kind == "service":
            return symbol is None and set(body) == {"phase"} and body["phase"] in ("start", "close")
        if kind == "policy_snapshot":
            return symbol is None and _policy_snapshot(body)
        if kind == "transport":
            return (isinstance(symbol, str) and bool(symbol)
                and set(body) == {"phase", "workerId", "attempt", "topics", "errorType", "discarded", "fastState", "deepState"}
                and body["phase"] in ("connecting", "subscription_sent", "fault", "cancelled", "drained")
                and _uint(body["attempt"]) and body["attempt"] > 0 and _uint(body["discarded"])
                and _uint(body["workerId"]) and body["workerId"] > 0
                and isinstance(body["topics"], list) and bool(body["topics"])
                and all(isinstance(t, str) and t.startswith(("orderbook.", "publicTrade.", "kline.")) for t in body["topics"])
                and (body["errorType"] is None or isinstance(body["errorType"], str))
                and _book_state(body["fastState"]) and _book_state(body["deepState"]))
    if schema in (SCHEMA, V3_SCHEMA):
        if kind == "clock_read":
            return (symbol is None and set(body) == {"scopeId", "method", "value"}
                and (body["scopeId"] is None or (_uint(body["scopeId"]) and body["scopeId"] > 0))
                and body["method"] in ("time", "monotonic", "perf_counter_ns")
                and (_uint(body["value"]) if body["method"] == "perf_counter_ns" else _finite(body["value"])))
        if kind == "scope":
            if not _uint(body.get("id")) or body["id"] == 0:
                return False
            if body.get("phase") == "begin":
                return (set(body) == {"phase", "id", "parentId", "name"} and isinstance(body["name"], str)
                    and body["name"] in (V4_SCOPE_NAMES if schema == SCHEMA else SCOPE_NAMES) and (body["parentId"] is None or
                    (_uint(body["parentId"]) and body["parentId"] > 0)))
            return (set(body) == {"phase", "id", "outcome"} and body["phase"] == "end"
                    and body["outcome"] in ("returned", "raised", "cancelled"))
        if kind == "scheduler":
            return (isinstance(symbol, str) and bool(symbol)
                and set(body) == {"phase", "taskId", "reason", "delay", "outcome"}
                and body["phase"] in ("scheduled", "coalesced", "started", "sleep", "resumed", "finished")
                and _uint(body["taskId"]) and body["taskId"] > 0
                and isinstance(body["reason"], str) and _finite(body["delay"]) and body["delay"] >= 0
                and (body["outcome"] in ("returned", "raised", "cancelled") if body["phase"] == "finished" else body["outcome"] is None))
    if schema in (SCHEMA, V3_SCHEMA, V2_SCHEMA):
        if kind == "bootstrap":
            return (isinstance(symbol, str) and bool(symbol)
                and set(body) == {"instrument", "fees", "candles", "context5m", "context15m", "context1h"}
                and (body["instrument"] is None or (_public_record(body["instrument"], INSTRUMENT_FIELDS, {"symbol", "status"}) and body["instrument"]["symbol"] == symbol))
                and (body["fees"] is None or (_public_record(body["fees"], FEE_FIELDS, {"symbol", "source"}) and body["fees"]["symbol"] == symbol))
                and all(_candles(body[k]) for k in ("candles", "context5m", "context15m", "context1h")))
        if kind == "rest_context":
            return (isinstance(symbol, str) and bool(symbol)
                and set(body) == {"candles", "context5m", "context15m", "context1h"}
                and (body["candles"] is None or _candles(body["candles"]))
                and all(_candles(body[k]) for k in ("context5m", "context15m", "context1h")))
        if kind == "scanner_result":
            return symbol is None and set(body) == {"candidates"} and isinstance(body["candidates"], list) and all(
                _public_record(c, CANDIDATE_FIELDS, {"symbol", "trade_count_source"},
                    {"funding_rate", "next_funding_time_ms", "trade_count_24h", "trade_count_source",
                     "correlation_1h_btc", "activity_rank"}) for c in body["candidates"])
        if kind == "source_error":
            return (set(body) == {"source", "errorType"} and body["source"] in ("bootstrap", "context", "scanner")
                    and isinstance(body["errorType"], str))
        if kind == "symbol_lifecycle":
            return (isinstance(symbol, str) and bool(symbol) and set(body) == {"action", "reason"}
                    and body["action"] in ("activate", "deactivate") and isinstance(body["reason"], str))
        if kind == "manifest":
            return (symbol is None and set(body) == {"phase", "manifest"}
                    and body["phase"] in ("capture", "run") and isinstance(body["manifest"], dict)
                    and not check_manifest(body["manifest"])[1])
        if kind == "run_end":
            return (symbol is None and set(body) == {"manifestId", "manifestSha256", "reason"}
                    and valid_digest(body["manifestId"], 32) and valid_digest(body["manifestSha256"])
                    and isinstance(body["reason"], str))
    if kind == "footer":
        return symbol is None and set(body) == {"inputCount"} and _uint(body["inputCount"])
    if kind == "market_message":
        return (isinstance(symbol, str) and bool(symbol) and set(body) == set(MARKET_FIELDS)
                and isinstance(body["topic"], str)
                and body["topic"].startswith(("orderbook.", "publicTrade.", "kline."))
                and isinstance(body["data"], (list, dict))
                and all(_uint(body[k]) for k in ("receipt_wall_ns", "receipt_mono_ns",
                            "parsed_mono_ns", "processor_started_mono_ns", "queue_depth"))
                and _finite(body["queue_lag_ms"]) and body["queue_lag_ms"] >= 0
                and all(body[k] is None or _uint(body[k]) for k in ("ts", "cts"))
                and all(body[k] is None or isinstance(body[k], str) for k in ("type", "event_id")))
    if kind == "clock_sample":
        return (symbol is None and set(body) == {"server_ms", "sent_mono", "received_mono", "received_wall_ms"}
                and all(_finite(v) for v in body.values()))
    if kind == "clock_error":
        return symbol is None and set(body) == {"errorType"} and isinstance(body["errorType"], str)
    if kind == "callback":
        return (set(body) == {"name"} and
                ((body["name"] == "evaluate" and isinstance(symbol, str) and bool(symbol))
                 or (body["name"] == "arbiter" and symbol is None)))
    if kind == "control":
        if symbol is not None:
            return False
        if body.get("name") == "set_running":
            return set(body) == {"name", "value"} and type(body["value"]) is bool
        if body.get("name") == "stop":
            return set(body) == {"name", "reason"} and isinstance(body["reason"], str)
        if body.get("name") == "toggle_strategy":
            return (set(body) == {"name", "key", "enabled"} and isinstance(body["key"], str)
                    and type(body["enabled"]) is bool)
    return False


class InputJournal:
    def __init__(self, record: Callable, clock: RuntimeClock) -> None:
        self.record = record
        self.clock = clock
        self.sequence = 0
        self.previous_hash: str | None = None
        self.closed = False
        self.append("header", None, {"coverage": "experimental_partial",
            "missingCoverage": list(MISSING_COVERAGE), "parityReady": False})

    def append(self, kind: str, symbol: str | None, body: dict) -> None:
        if self.closed:
            raise RuntimeError("input journal is closed")
        if kind not in KINDS or not valid_body(kind, symbol, body):
            raise ValueError("unsupported input journal body")
        # Detach BEFORE enqueueing: the engine may mutate the message later.
        detached = json.loads(json.dumps(body, allow_nan=False))
        wall, mono = self.clock.time(), self.clock.perf_counter_ns()
        if not _finite(wall) or not _uint(mono):
            raise ValueError("invalid processing clock observation")
        row = {"schema": SCHEMA, "sequence": self.sequence + 1,
               "previousHash": self.previous_hash, "kind": kind, "symbol": symbol,
               "processingWallSeconds": wall, "processingMonoNs": mono, "body": detached}
        row["hash"] = fingerprint(row)
        # Advance even if the bounded recorder drops a row. The next row/footer
        # then exposes that loss instead of numbering the surviving rows anew.
        self.sequence += 1
        self.previous_hash = row["hash"]
        self.record(EVENT, symbol, row)

    def market_message(self, symbol: str, message) -> None:
        # No generic __dict__/transport/headers/auth/OTel serialization.
        body = {name: getattr(message, name) for name in MARKET_FIELDS}
        self.append("market_message", symbol, body)

    def close(self) -> None:
        if not self.closed:
            self.append("footer", None, {"inputCount": self.sequence - 1})
            self.closed = True


def validate_input_journal(path: str | Path, *, max_row_bytes: int = 16 * 1024 * 1024) -> dict:
    """Streaming, fixed-byte snapshot. Never returns raw payloads/configuration."""
    path = Path(path)
    if type(max_row_bytes) is not int or max_row_bytes < 1:
        raise ValueError("max_row_bytes must be a positive integer")
    issues: Counter = Counter()
    counts: Counter = Counter()
    digest = hashlib.sha256()
    last_hash = None
    sequence = 0
    last_mono = None
    header = footer = False
    missing_receipts = 0
    observed_schema = None
    capture_manifest = None
    run_manifests = {}
    active_run = None
    last_scope_id = 0
    open_scopes = {}
    tasks = {}
    last_task_id = 0
    waits = {}
    context_batches = {}
    source_waits = {}
    last_source_wait = 0
    last_context_batch = 0
    last_wait_id = 0
    connections = {}
    policy_snapshot = None
    compressed = path.suffix == ".gz"
    bytes_read = 0
    with (gzip.open(path, "rb") if compressed else path.open("rb")) as stream:
        size = os.fstat(stream.fileno()).st_size
        remaining = None if compressed else size
        while remaining is None or remaining:
            line = stream.readline(max_row_bytes + 1 if compressed else min(remaining, max_row_bytes + 1))
            if not line:
                if not compressed:
                    issues["snapshot_truncated"] += 1
                break
            if remaining is not None:
                remaining -= len(line)
            bytes_read += len(line)
            digest.update(line)
            if len(line) > max_row_bytes:
                issues["row_too_large"] += 1
                # Drain this line in bounded chunks, preserving snapshot hash.
                while not line.endswith(b"\n") and (remaining is None or remaining):
                    line = stream.readline(max_row_bytes + 1 if compressed else min(remaining, max_row_bytes + 1))
                    if not line:
                        break
                    if remaining is not None:
                        remaining -= len(line)
                    bytes_read += len(line)
                    digest.update(line)
                continue
            if not line.endswith(b"\n"):
                issues["partial_row"] += 1
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                issues["invalid_json"] += 1
                continue
            if not isinstance(event, dict):
                issues["invalid_row"] += 1
                continue
            if event.get("event") != EVENT:
                continue
            p = event.get("payload")
            fields = {"schema", "sequence", "previousHash", "kind", "symbol",
                      "processingWallSeconds", "processingMonoNs", "body", "hash"}
            if not isinstance(p, dict) or set(p) != fields or p.get("schema") not in (SCHEMA, V3_SCHEMA, V2_SCHEMA, LEGACY_SCHEMA):
                issues["invalid_envelope"] += 1
                continue
            schema = p["schema"]
            if observed_schema is not None and observed_schema != schema:
                issues["mixed_schemas"] += 1
            observed_schema = observed_schema or schema
            if footer:
                issues["input_after_footer"] += 1
            kind = p["kind"]
            allowed = (LEGACY_KINDS if schema == LEGACY_SCHEMA else V2_KINDS if schema == V2_SCHEMA
                       else V3_KINDS if schema == V3_SCHEMA else KINDS)
            if not isinstance(kind, str) or kind not in allowed or not valid_body(kind, p["symbol"], p["body"], schema):
                issues["invalid_body"] += 1
                continue
            counts[kind] += 1
            body = p["body"]
            if kind == "source_await":
                identity = body["id"]
                key = (body["source"], p["symbol"])
                if body["phase"] == "wait":
                    if identity <= last_source_wait:
                        issues["duplicate_source_wait"] += 1
                    last_source_wait = max(last_source_wait, identity)
                    source_waits[identity] = key
                elif source_waits.pop(identity, None) != key:
                    issues["unmatched_source_resume"] += 1
            if kind == "context_await":
                identity, phase = body["id"], body["phase"]
                if phase == "wait":
                    if identity <= last_context_batch or context_batches:
                        issues["invalid_context_wait"] += 1
                    last_context_batch = max(last_context_batch, identity)
                    context_batches[identity] = list(body["symbols"])
                elif identity not in context_batches:
                    issues["unmatched_context_await"] += 1
                elif phase == "request":
                    pending = context_batches[identity]
                    if not pending or pending.pop(0) != p["symbol"]:
                        issues["invalid_context_request"] += 1
                else:
                    pending = context_batches.pop(identity)
                    if phase == "ready" and pending:
                        issues["premature_context_ready"] += 1
            if kind == "policy_snapshot":
                if policy_snapshot is not None or capture_manifest is None:
                    issues["misplaced_policy_snapshot"] += 1
                policy_snapshot = body
                if capture_manifest is not None:
                    policy = capture_manifest.get("researchPolicy", {})
                    if (body["mode"] != policy.get("mode") or body["sourceFileSha256"] != policy.get("sourceSha256")
                        or (body["manifest"] or {}).get("policyFingerprint") != policy.get("policyFingerprint")):
                        issues["policy_manifest_binding_mismatch"] += 1
            if kind == "dispatch":
                identity = body["id"]
                if body["phase"] == "wait":
                    if identity <= last_wait_id:
                        issues["duplicate_wait_id"] += 1
                    last_wait_id = max(last_wait_id, identity)
                    waits[identity] = (body["source"], body["delay"])
                elif waits.pop(identity, None) != (body["source"], body["delay"]):
                    issues["unmatched_dispatch_resume"] += 1
            if kind == "transport":
                key = (body["workerId"], p["symbol"], tuple(body["topics"]))
                previous = connections.get(key)
                phase, attempt = body["phase"], body["attempt"]
                valid = False
                if phase == "connecting":
                    valid = previous is None and attempt == 1 or previous is not None and previous == (attempt - 1, "drained")
                elif previous is not None and previous[0] == attempt:
                    before = previous[1]
                    valid = ((phase == "subscription_sent" and before == "connecting")
                        or (phase in ("fault", "cancelled") and before in ("connecting", "subscription_sent"))
                        or (phase == "drained" and before in ("subscription_sent", "fault", "cancelled")))
                if not valid:
                    issues["invalid_transport_transition"] += 1
                connections[key] = (attempt, phase)
            if kind == "scope":
                identity = body["id"]
                if body["phase"] == "begin":
                    if identity != last_scope_id + 1 or (body["parentId"] is not None and body["parentId"] > last_scope_id):
                        issues["invalid_scope_parent_or_id"] += 1
                    last_scope_id = max(last_scope_id, identity)
                    open_scopes[identity] = p["symbol"]
                else:
                    if identity not in open_scopes or open_scopes.get(identity) != p["symbol"]:
                        issues["unmatched_scope_end"] += 1
                    open_scopes.pop(identity, None)
            if kind == "clock_read" and body["scopeId"] is not None and body["scopeId"] not in open_scopes:
                issues["clock_read_outside_scope"] += 1
            if kind == "scheduler":
                identity, phase = body["taskId"], body["phase"]
                previous = tasks.get(identity)
                if phase == "scheduled":
                    if identity <= last_task_id:
                        issues["duplicate_scheduled_task"] += 1
                    last_task_id = max(last_task_id, identity)
                    tasks[identity] = (p["symbol"], "scheduled")
                elif previous is None or previous[0] != p["symbol"]:
                    issues["invalid_scheduler_transition"] += 1
                elif phase == "coalesced":
                    pass
                elif ((phase == "started" and previous[1] == "scheduled")
                      or (phase == "sleep" and previous[1] in ("started", "resumed"))
                      or (phase == "resumed" and previous[1] == "sleep")
                      or (phase == "finished" and (previous[1] != "scheduled" or body["outcome"] == "cancelled"))):
                    if phase == "finished":
                        tasks.pop(identity)
                    else:
                        tasks[identity] = (p["symbol"], phase)
                else:
                    issues["invalid_scheduler_transition"] += 1
            if kind == "manifest":
                manifest = p["body"]["manifest"]
                if p["body"]["phase"] == "capture":
                    if capture_manifest is not None or sequence != 1:
                        issues["misplaced_capture_manifest"] += 1
                    capture_manifest = manifest
                else:
                    identity = manifest["manifestId"]
                    if active_run is not None or identity in run_manifests:
                        issues["overlapping_or_reused_run"] += 1
                    if capture_manifest is None:
                        issues["run_without_capture_manifest"] += 1
                    elif any(manifest.get(k) != capture_manifest.get(k) for k in ("configSha256", "code", "runtimeSha256")):
                        issues["capture_run_provenance_mismatch"] += 1
                    run_manifests[identity] = manifest["manifestSha256"]
                    active_run = identity
            if kind == "run_end":
                body = p["body"]
                if body["manifestId"] != active_run or run_manifests.get(body["manifestId"]) != body["manifestSha256"]:
                    issues["run_end_manifest_mismatch"] += 1
                active_run = None
            if type(p["sequence"]) is not int or p["sequence"] != sequence + 1:
                issues["sequence_gap_or_reorder"] += 1
            sequence += 1
            if p["previousHash"] != last_hash:
                issues["broken_hash_chain"] += 1
            try:
                if fingerprint({k: v for k, v in p.items() if k != "hash"}) != p["hash"]:
                    issues["content_hash_mismatch"] += 1
            except (ValueError, TypeError):
                issues["invalid_hash_content"] += 1
            last_hash = p["hash"]
            mono = p["processingMonoNs"]
            if not _uint(mono) or not _finite(p["processingWallSeconds"]):
                issues["invalid_processing_clock"] += 1
            elif last_mono is not None and mono < last_mono:
                issues["processing_clock_reorder"] += 1
            else:
                last_mono = mono
            if kind == "header":
                if header or sequence != 1:
                    issues["misplaced_header"] += 1
                header = True
            elif not header:
                issues["missing_header_before_input"] += 1
            if kind == "market_message":
                receipt = p["body"]["receipt_mono_ns"]
                if receipt == 0:
                    missing_receipts += 1
                elif _uint(mono) and receipt > mono:
                    issues["receipt_after_processing"] += 1
            if kind == "footer":
                if source_waits:
                    issues["unfinished_source_waits"] += 1
                if context_batches:
                    issues["unfinished_context_await"] += 1
                if waits:
                    issues["unfinished_dispatch_waits"] += 1
                if any(phase != "drained" for _, phase in connections.values()):
                    issues["unfinished_transport_attempts"] += 1
                if open_scopes:
                    issues["unclosed_scopes"] += 1
                if tasks:
                    issues["unfinished_scheduled_tasks"] += 1
                if active_run is not None:
                    issues["run_not_closed"] += 1
                if p["body"]["inputCount"] != sequence - 2:
                    issues["footer_count_mismatch"] += 1
                footer = True
        if compressed and os.fstat(stream.fileno()).st_size != size:
            issues["source_changed_during_validation"] += 1
    structural = "rejected" if issues else "checks_passed" if header and footer else "incomplete"
    missing = list(COVERAGE[observed_schema or SCHEMA])
    if observed_schema != LEGACY_SCHEMA and capture_manifest is None:
        missing.append("capture_manifest_missing")
    if observed_schema == SCHEMA and policy_snapshot is None:
        missing.append("policy_snapshot_missing")
    return {"schema": observed_schema or SCHEMA, "structuralStatus": structural, "parityReady": False,
            "coverage": "experimental_partial" if header else "not_recorded",
            "missingCoverage": missing, "captureManifestChecked": capture_manifest is not None,
            "policySnapshotChecked": policy_snapshot is not None,
            "boundRuns": len(run_manifests), "headerPresent": header,
            "footerPresent": footer, "missingReceiptCount": missing_receipts,
            "counts": dict(counts), "issues": dict(issues),
            "source": {"file": path.name, "snapshotBytes": size,
                       "bytesRead": bytes_read, "snapshotSha256": digest.hexdigest(),
                       "compression": "gzip" if compressed else "none", "hashBasis": "decoded_jsonl"}}
