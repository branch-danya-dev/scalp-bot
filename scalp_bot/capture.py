"""Full input recording for the ordinary single-engine paper UI.

Capture completeness is checked separately from strategy profitability and
offline output parity. No additional trading engine or exchange orders.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
from queue import Full, Queue
import shutil
from threading import Lock, Thread
import zipfile

import msgspec

from .engine import TradingEngine
from .recorder import SessionRecorder
from .run_manifest import code_provenance


PROFILES = {
    "current-1h": {"seconds": 3600, "trend": False, "beta": False, "freeGiB": 20},
    "all-12h": {"seconds": 43200, "trend": True, "beta": True, "freeGiB": 100},
}
_STOP = object()


def validate_profile(config, profile):
    if profile not in PROFILES:
        raise ValueError("unknown paper capture profile")
    spec = PROFILES[profile]
    if (config.paper_run_duration_seconds != spec["seconds"]
            or config.trend_structure_enabled != spec["trend"]
            or not all((config.breakout_enabled, config.weak_level_rejection_enabled, config.density_enabled))
            or config.e01_breakout_obstacle_veto or config.e06_conditional_breakout_hold
            or not config.exchange_clock_enabled or not config.event_driven_evaluation_enabled
            or config.research_policy_mode != "off" or config.fee_rate_mode != "configured"):
        raise ValueError("capture settings differ from the selected profile")
    return spec


class InputWriter:
    """Compressed, bounded, append-only stream; loss invalidates the capture."""

    def __init__(self, path, *, max_queue_bytes=32 * 1024**2):
        self.path = Path(path)
        self.max_queue_bytes = max_queue_bytes
        self.pending_bytes = 0
        self.lock = Lock()
        self.queue = Queue(maxsize=32768)
        self.error = None
        self.accepted = self.written = 0
        self.closed = False
        self.manifest = None
        self.encoder = msgspec.json.Encoder()
        # Reserve the filename before starting the writer: never append old data.
        self.stream = self.path.open("xb")
        self.thread = Thread(target=self._run, name="paper-input-writer", daemon=True)
        self.thread.start()

    def record(self, event, symbol, payload):
        if self.error or self.closed:
            self.error = self.error or "input writer already closed"
            return
        if payload["kind"] == "manifest" and payload["body"]["phase"] == "capture":
            self.manifest = deepcopy(payload["body"]["manifest"])
        data = self.encoder.encode(dict(event=event, symbol=symbol, payload=payload)) + b"\n"
        with self.lock:
            if self.pending_bytes + len(data) > self.max_queue_bytes:
                self.error = "input recording queue exceeded its byte limit"
                return
            self.pending_bytes += len(data)
        try:
            self.queue.put_nowait(data)
        except Full:
            with self.lock:
                self.pending_bytes -= len(data)
            self.error = "input recording queue is full"
            return
        self.accepted += 1

    def _run(self):
        try:
            with self.stream, gzip.GzipFile(fileobj=self.stream, mode="wb", compresslevel=1, mtime=0) as output:
                while True:
                    item = self.queue.get()
                    if item is _STOP:
                        return
                    try:
                        output.write(item)
                        self.written += 1
                    finally:
                        with self.lock:
                            self.pending_bytes -= len(item)
        except BaseException as exc:
            self.error = f"input writer failed: {type(exc).__name__}"

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.thread.is_alive():
            try:
                self.queue.put(_STOP, timeout=10)
                self.thread.join(15)
            except Full:
                self.error = self.error or "input writer shutdown queue timeout"
        if self.thread.is_alive():
            self.error = self.error or "input writer shutdown timeout"
        if self.written != self.accepted:
            self.error = self.error or "input writer did not finish all accepted rows"


class CaptureRecorder(SessionRecorder):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inputs = InputWriter(self.path.with_suffix(".inputs.jsonl.gz"))

    def record(self, event, symbol, payload):
        if event == "replay_input":
            self.inputs.record(event, symbol, payload)
        else:
            super().record(event, symbol, payload)

    def close(self, timeout=15):
        try:
            super().close(timeout)
        finally:
            self.inputs.close()


class PaperCapture:
    def __init__(self, config, profile, *, engine_factory=TradingEngine):
        spec = validate_profile(config, profile)
        self.profile = profile
        self.directory = Path(config.session_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        # A launcher allocates a new directory for every invocation.
        self.metadata_path = self.directory / "capture.json"
        with self.metadata_path.open("x", encoding="utf-8") as f:
            json.dump({"status": "preparing", "profile": profile}, f)
        if shutil.disk_usage(self.directory).free < spec["freeGiB"] * 1024**3:
            raise ValueError(f"capture needs at least {spec['freeGiB']} GiB free")
        self.error = None
        self.started = False
        self.finished = False
        self.source_root = Path(__file__).resolve().parents[1]
        self.recorder = CaptureRecorder(config.session_dir, queue_size=config.recorder_queue_size,
            critical_enqueue_timeout_seconds=config.recorder_critical_enqueue_timeout_seconds)
        try:
            self.engine = engine_factory(config, recorder=self.recorder, capture_inputs=True)
            self.manifest = self.recorder.inputs.manifest
            if self.manifest is None:
                raise ValueError("capture manifest was not recorded")
            self._archive_source()
            # This is a recorded control, before service startup/Start. The run
            # manifest captures the enabled beta, while normal startup stays off.
            if spec["beta"]:
                self.engine.toggle_strategy("price_action_hypothesis", True)
            self.expected_strategies = dict(self.engine.strategy_enabled)
            self._write_status("recording")
        except BaseException:
            self.recorder.close()
            raise

    def _archive_source(self):
        with zipfile.ZipFile(self.directory / "source-at-capture.zip", "x", zipfile.ZIP_DEFLATED) as archive:
            for name, expected in self.manifest["code"]["fileHashes"].items():
                content = (self.source_root / name).read_bytes()
                if hashlib.sha256(content).hexdigest() != expected:
                    raise ValueError("source changed while archiving")
                archive.writestr(name, content)
        (self.directory / "capture-manifest.json").write_text(
            json.dumps(self.manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _source_matches(self):
        return code_provenance(self.source_root)["sourceSha256"] == self.manifest["code"]["sourceSha256"]

    def before_start(self):
        self.error = self.error or self.recorder.inputs.error
        if self.started or self.error:
            raise RuntimeError("Эта запись уже запускалась или повреждена; нужен новый запуск сервера")
        if not self._source_matches():
            raise RuntimeError("Код изменён после начала записи; перезапустите сервер")
        if self.engine.strategy_enabled != self.expected_strategies:
            raise RuntimeError("Набор стратегий отличается от профиля записи")

    def _write_status(self, status):
        data = dict(schema="paper-capture-v1", status=status, profile=self.profile,
            session=self.recorder.path.name, inputs=self.recorder.inputs.path.name,
            sourceSha256=self.manifest["code"]["sourceSha256"],
            error=self.error, inputRows=self.recorder.inputs.accepted,
            inputRowsWritten=self.recorder.inputs.written,
            recorderHealth=self.recorder.health(),
            fullReplayVerified=False, profitabilityProven=False)
        self.metadata_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async def monitor(self):
        while True:
            await asyncio.sleep(1)
            health = self.recorder.health()
            reason = self.recorder.inputs.error
            if health["droppedRows"] or health["writerError"]:
                reason = reason or "session recorder lost data"
            try:
                if shutil.disk_usage(self.directory).free < 5 * 1024**3:
                    reason = reason or "less than 5 GiB free disk space remains"
            except OSError:
                reason = reason or "capture disk is unavailable"
            if reason and not self.error:
                self.error = reason
                # Normal recorded operator stop retains the usual exit handling.
                self.engine.set_running(False)
                try:
                    self._write_status("invalid")
                except OSError:
                    pass  # UI still exposes error if the disk cannot be written.

    def public(self):
        done = self.started and not self.engine.running
        return dict(profile=self.profile, startAllowed=not self.started and not self.error,
            strategiesLocked=True, error=self.error,
            message=("Запись повреждена: " + self.error if self.error else
                     "Прогон завершён. Остановите сервер через Ctrl+C для завершения записи." if done else
                     "Полная запись для симуляции включена · состав стратегий зафиксирован"))

    def finish(self):
        if self.finished:
            return
        self.finished = True
        health = self.recorder.health()
        self.error = self.error or self.recorder.inputs.error
        if health["droppedRows"] or health["writerError"] or health["pendingRows"]:
            self.error = self.error or "session recording incomplete"
        if not self._source_matches():
            self.error = self.error or "source changed during capture"
        if not self.engine.input_journal.closed:
            self.error = self.error or "input journal footer missing"
        if not self.recorder.inputs.closed or self.recorder.inputs.thread.is_alive():
            self.error = self.error or "input writer not closed"
        self._write_status("invalid" if self.error else "sealed")


def from_environment(config, environ):
    profile = environ.get("SCALP_CAPTURE_PROFILE", "")
    return PaperCapture(config, profile) if profile else None
