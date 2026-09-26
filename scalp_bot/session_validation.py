"""Bounded-memory checks of recorded research runs, not an edge/backtest gate."""
from __future__ import annotations

import hashlib
import math
from collections import Counter
from pathlib import Path
from typing import Any

import msgspec
from .manifest_validation import check_manifest


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


class _Run:
    def __init__(self, index: int, row: dict, prior: dict) -> None:
        payload = row.get("payload") or {}
        self.result: dict[str, Any] = {
            "index": index,
            "label": payload.get("runLabel"),
            "startedAt": payload.get("startedAt", row.get("ts")),
            "configuredDurationSeconds": payload.get("durationSeconds"),
            "completed": False,
            "counts": {},
            "clock": {
                "frameSamples": 0,
                "tradeAfterRecord": 0,
                "tradeAfterContext": 0,
                "maxTradeAfterRecordMs": 0.0,
                "maxTradeAfterContextMs": 0.0,
            },
            "pnl": {"trades": 0, "gross": 0.0, "fees": 0.0, "funding": 0.0, "net": 0.0},
            "findings": [],
        }
        self.counts: Counter[str] = Counter()
        self.findings: dict[str, dict] = {}
        self.prior = prior
        self.last_ts = _number(row.get("ts"))
        self.manifest_present = "manifest" in payload
        self.result["provenance"], findings = check_manifest(payload.get("manifest"))
        self.result["provenance"]["summaryLink"] = "pending" if self.manifest_present else "unknown"
        if self.manifest_present and payload["manifest"] is None:
            findings.append(("invalid_manifest", "error"))
            self.result["provenance"]["status"] = "invalid"
        for code, severity in findings:
            self.flag(code, severity, row)

    def flag(self, code: str, severity: str, row: dict) -> None:
        finding = self.findings.setdefault(code, {
            "code": code, "severity": severity, "count": 0, "examples": [],
        })
        finding["count"] += 1
        if len(finding["examples"]) < 3:
            finding["examples"].append({
                "ts": row.get("ts"), "symbol": row.get("symbol"),
                "event": row.get("event"),
            })

    def consume(self, row: dict, tolerance_ms: float) -> None:
        event = row["event"]
        payload = row.get("payload") or {}
        self.counts[event] += 1
        ts = _number(row.get("ts"))
        if ts is None:
            self.flag("invalid_record_timestamp", "error", row)
        elif self.last_ts is not None and ts < self.last_ts:
            self.flag("record_clock_moved_backwards", "error", row)
        if ts is not None:
            self.last_ts = ts

        if event == "strategy_toggle":
            self.flag("strategy_changed_during_run", "error", row)
            if self.manifest_present and payload.get("manifestId") != self.result["provenance"]["manifestId"]:
                self.flag("strategy_toggle_manifest_mismatch", "error", row)
        if event == "strategy_error":
            self.flag("strategy_error", "error", row)
        if event == "research_frame":
            sample_clock = payload.get("clock")
            aligned_clock = isinstance(sample_clock, dict) and sample_clock.get("contract") == "exchange-bounds-v1"
            if aligned_clock:
                lower = _number(sample_clock.get("exchange_lower_ms"))
                upper = _number(sample_clock.get("exchange_upper_ms"))
                evaluation = _number(sample_clock.get("evaluationMs"))
                if sample_clock.get("valid") is not True:
                    self.flag("runtime_clock_invalid", "error", row)
                elif lower is None or upper is None or evaluation is None or lower > upper or not upper <= evaluation < upper + 1.001:
                    self.flag("runtime_clock_invalid_bounds", "error", row)
            if payload.get("tradeDeltaGap"):
                self.flag("trade_delta_gap", "error", row)
            context = payload.get("marketContext") or {}
            if not isinstance(context, dict):
                self.flag("invalid_frame_context", "error", row)
                return
            forming = payload.get("formingCandle") or context.get("formingCandle") or {}
            if not isinstance(forming, dict):
                self.flag("invalid_frame_context", "error", row)
                return
            trade_ms = _number(forming.get("lastTradeTsMs"))
            observed_ms = _number(forming.get("observedAtMs"))
            if trade_ms is not None and ts is not None:
                clock = self.result["clock"]
                clock["frameSamples"] += 1
                for name, reference in (("Record", ts * 1000), ("Context", observed_ms)):
                    if reference is None:
                        continue
                    offset = trade_ms - reference
                    clock[f"maxTradeAfter{name}Ms"] = max(
                        clock[f"maxTradeAfter{name}Ms"], offset,
                    )
                    if offset > tolerance_ms:
                        clock[f"tradeAfter{name}"] += 1
                        if name != "Record" or not aligned_clock:
                            self.flag(f"trade_timestamp_after_{name.lower()}", "error", row)
                if aligned_clock and sample_clock.get("valid") is True:
                    upper = _number(sample_clock.get("exchange_upper_ms"))
                    if upper is not None and trade_ms > upper:
                        self.flag("trade_timestamp_after_exchange_bound", "error", row)

        if event == "trade_closed":
            values = {key: _number(payload.get(key)) for key in ("grossPnl", "fees", "netPnl")}
            funding = _number(payload.get("fundingPnlUsd", 0))
            if funding is None or any(value is None for value in values.values()):
                self.flag("invalid_trade_accounting", "error", row)
            else:
                pnl = self.result["pnl"]
                pnl["trades"] += 1
                for source, dest in (("grossPnl", "gross"), ("fees", "fees"), ("netPnl", "net")):
                    pnl[dest] += values[source]
                pnl["funding"] += funding
                if abs(values["grossPnl"] - values["fees"] + funding - values["netPnl"]) > 1e-6:
                    self.flag("trade_pnl_mismatch", "error", row)

    def finish(self, row: dict | None) -> dict:
        if row is None:
            self.flag("missing_run_summary", "incomplete", {})
        else:
            summary = row["payload"]
            provenance = self.result["provenance"]
            if self.manifest_present:
                provenance["summaryLink"] = "checked"
                if not provenance["manifestId"] or summary.get("manifestId") != provenance["manifestId"]:
                    self.flag("summary_manifest_id_mismatch", "error", row)
                    provenance["summaryLink"] = "invalid"
                if not provenance["manifestSha256"] or summary.get("manifestSha256") != provenance["manifestSha256"]:
                    self.flag("summary_manifest_hash_mismatch", "error", row)
                    provenance["summaryLink"] = "invalid"
            elif summary.get("manifestId") or summary.get("manifestSha256"):
                self.flag("summary_manifest_without_start_manifest", "error", row)
            self.result["completed"] = True
            self.result["stoppedAt"] = summary.get("stoppedAt", row.get("ts"))
            self.result["elapsedSeconds"] = summary.get("elapsedSeconds")
            self.result["stopReason"] = summary.get("reason")
            if summary.get("reason") != "duration_elapsed":
                self.flag("run_not_stopped_by_duration", "warning", row)
            # Broker counters are process-cumulative across Start/Stop cycles.
            net = _number(summary.get("realizedPnl"))
            count = _number(summary.get("closedTrades"))
            prior_net = self.prior.get("net", 0.0)
            prior_count = self.prior.get("trades", 0.0)
            if net is None or count is None or prior_net is None or prior_count is None:
                self.flag("summary_accounting_unavailable", "incomplete", row)
            else:
                if abs(net - prior_net - self.result["pnl"]["net"]) > 1e-6:
                    self.flag("summary_pnl_mismatch", "error", row)
                if count - prior_count != self.result["pnl"]["trades"]:
                    self.flag("summary_trade_count_mismatch", "error", row)
            self.prior.update(net=net, trades=count)
            health = summary.get("recorderHealth")
            if not isinstance(health, dict) or _number(health.get("droppedRows")) is None:
                self.flag("recorder_health_unavailable", "incomplete", row)
            elif health["droppedRows"] > 0 or health.get("writerError"):
                self.flag("recorder_data_loss", "error", row)
            # Existing bot_started.config is a partial snapshot, not full provenance.
            # Never certify reproducibility simply because some config was recorded.
        if not self.result["clock"]["frameSamples"]:
            self.flag("clock_evidence_unavailable", "incomplete", {})
        self.result["counts"] = dict(self.counts)
        self.result["findings"] = list(self.findings.values())
        severities = {row["severity"] for row in self.findings.values()}
        self.result["status"] = (
            "rejected" if "error" in severities else
            "incomplete" if "incomplete" in severities else "checks_passed"
        )
        return self.result


def validate_session(path: str | Path, *, future_tolerance_ms: float = 0.0) -> dict:
    """Validate a finite byte snapshot; ignore market activity outside run boundaries.

    Hash identifies precisely the bytes read, even if the source keeps appending.
    Memory use is bounded by a JSONL row plus compact per-run summaries.
    checks_passed only refers to implemented checks, never production readiness.
    """
    if not math.isfinite(future_tolerance_ms) or future_tolerance_ms < 0:
        raise ValueError("future_tolerance_ms must be finite and nonnegative")
    source = Path(path)
    runs: list[dict] = []
    active: _Run | None = None
    prior: dict = {}
    issues: Counter[str] = Counter()
    digest = hashlib.sha256()
    rows = 0
    decoder = msgspec.json.Decoder(type=dict)
    manifest_ids: set[str] = set()
    with source.open("rb") as stream:
        stream.seek(0, 2)
        size = stream.tell()
        stream.seek(0)
        remaining = size
        while remaining:
            line = stream.readline(remaining)
            if not line:
                issues["source_truncated_during_read"] += 1
                break
            remaining -= len(line)
            digest.update(line)
            if not line.strip():
                continue
            try:
                row = decoder.decode(line)
            except msgspec.DecodeError:
                issues["invalid_json_row"] += 1
                if active is not None:
                    active.flag("invalid_json_row", "error", {})
                continue
            rows += 1
            event = row.get("event")
            if not isinstance(event, str) or not isinstance(row.get("payload"), dict):
                issues["invalid_event_envelope"] += 1
                if active is not None:
                    active.flag("invalid_event_envelope", "error", row)
                continue
            if event == "bot_started":
                if active is not None:
                    active.flag("overlapping_run_start", "error", row)
                    runs.append(active.finish(None))
                    prior.update(net=None, trades=None)
                active = _Run(len(runs) + 1, row, prior)
                manifest_id = active.result["provenance"]["manifestId"]
                if manifest_id:
                    if manifest_id in manifest_ids:
                        active.flag("reused_manifest_id", "error", row)
                    manifest_ids.add(manifest_id)
            if active is not None:
                active.consume(row, future_tolerance_ms)
            if event == "run_summary":
                if active is None:
                    issues["summary_without_start"] += 1
                else:
                    runs.append(active.finish(row))
                    active = None
        if active is not None:
            runs.append(active.finish(None))
    statuses = {run["status"] for run in runs}
    status = (
        "rejected" if issues or "rejected" in statuses else
        "incomplete" if not runs or "incomplete" in statuses else "checks_passed"
    )
    return {
        "schemaVersion": 1,
        "validatorVersion": "session-integrity-v3",
        "status": status,
        "scope": "recorded_run_integrity_only",
        "productionReady": False,
        "source": {
            "file": str(source.resolve()), "snapshotBytes": size,
            "bytesRead": size - remaining, "snapshotSha256": digest.hexdigest(),
            "parsedRows": rows,
        },
        "policy": {"futureToleranceMs": future_tolerance_ms},
        "fileIssues": dict(issues),
        "runs": runs,
        "notValidated": [
            "full_code_and_configuration_provenance", "live_replay_parity",
            "execution_model_accuracy", "strategy_edge", "network_event_completeness",
        ],
    }
