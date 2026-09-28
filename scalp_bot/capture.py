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

from .engine import TradingEngine
from .recorder import SessionRecorder
from .input_journal import DEFERRED_ROWS, detach_json
from .run_manifest import code_provenance
from .capture_codec import CaptureCodec


PROFILES = {
    "current-1h": {"seconds": 3600, "trend": False, "beta": False, "freeGiB": 20},
    "current-12h": {"seconds": 43200, "trend": False, "beta": False, "freeGiB": 100},
    "all-12h": {"seconds": 43200, "trend": True, "beta": True, "freeGiB": 100},
    "current-24h": {"seconds": 86400, "trend": False, "beta": False, "freeGiB": 200,
                    "targetNetReturnFraction": 0.10},
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
    if profile == "current-12h" and config.price_action_hypothesis_enabled:
        raise ValueError("current-12h keeps beta disabled")
    return spec


class _BatchQueue(Queue):
    def get_batch(self, limit, max_bytes):
        # Queue's protected hooks run under its one condition lock. Pop a
        # bounded ready prefix, then notify producers once for the whole batch.
        with self.not_empty:
            while not self._qsize():
                self.not_empty.wait()
            batch, retained = [], 0
            while self._qsize() and len(batch) < limit and retained < max_bytes:
                item = self._get()
                batch.append(item)
                if item is _STOP:
                    break
                retained += item[1]
            self.not_full.notify_all()
            return batch


class InputWriter:
    """Compressed, bounded, append-only stream; loss invalidates the capture."""

    def __init__(self, path, *, max_queue_bytes=32 * 1024**2):
        self.path = Path(path)
        self.max_queue_bytes = max_queue_bytes
        self.pending_bytes = 0
        self.lock = Lock()
        self.queue = _BatchQueue(maxsize=32768)
        self.error = None
        self.accepted = self.written = 0
        self.rejected = 0
        self.high_water_bytes = 0
        self.compression_writes = 0
        self.hashed_rows = 0
        self.max_batch_rows = self.max_batch_encoded_bytes = 0
        self.closed = False
        self.manifest = None
        # Reserve the filename before starting the writer: never append old data.
        self.stream = self.path.open("xb")
        try:
            self.codec = CaptureCodec()
        except BaseException:
            self.stream.close()
            raise
        self.thread = Thread(target=self._run, name="paper-input-writer", daemon=True)
        self.thread.start()

    def record(self, event, symbol, payload):
        if self.error or self.closed:
            self.error = self.error or "input writer already closed"
            self.rejected += 1
            return
        deferred = isinstance(payload, DEFERRED_ROWS)
        kind = payload.kind if deferred else payload['kind']
        if kind == "manifest":
            body = payload.row if deferred else payload
            if body["body"]["phase"] == "capture":
                self.manifest = deepcopy(body["body"]["manifest"])
        if deferred:
            size = payload.retained_bytes
        else:
            payload, size = detach_json(payload)
            size += 2048
        data = dict(event=event, symbol=symbol, payload=payload)
        with self.lock:
            if self.pending_bytes + size > self.max_queue_bytes:
                self.error = "input recording queue exceeded its byte limit"
                self.rejected += 1
                return
            self.pending_bytes += size
            self.high_water_bytes = max(self.high_water_bytes, self.pending_bytes)
        try:
            self.queue.put_nowait((data, size))
        except Full:
            with self.lock:
                self.pending_bytes -= size
            self.error = "input recording queue is full"
            self.rejected += 1
            return
        self.accepted += 1

    def _run(self):
        try:
            with self.stream as output:
                while True:
                    # One codec request at a time, <=1024 ready rows and
                    # <=4MiB retained plus one complete row. All remain charged
                    # against the original 32MiB queue until file write succeeds.
                    items = self.queue.get_batch(1024, 4 * 1024**2)
                    retained = sum(item[1] for item in items if item is not _STOP)
                    rows = [item[0] for item in items if item is not _STOP]
                    stopping = items[-1] is _STOP
                    try:
                        if rows:
                            self._write_batch(output, rows)
                    finally:
                        with self.lock:
                            self.pending_bytes -= retained
                    if stopping:
                        return
        except BaseException as exc:
            self.error = self.error or f"input writer failed: {type(exc).__name__}"
        finally:
            self.codec.close()

    def _write_batch(self, output, rows):
        result = self.codec.encode(rows)
        output.write(result['data'])
        count = len(rows)
        self.compression_writes += 1
        self.hashed_rows += result['hashedRows']
        self.max_batch_rows = max(self.max_batch_rows, count)
        self.max_batch_encoded_bytes = max(self.max_batch_encoded_bytes, result['encodedBytes'])
        self.written += count

    def health(self):
        with self.lock:
            pending, high_water = self.pending_bytes, self.high_water_bytes
        return dict(error=self.error, accepted=self.accepted, written=self.written,
            rejected=self.rejected, pendingBytes=pending, highWaterBytes=high_water,
            maxQueueBytes=self.max_queue_bytes, queueCapacity=self.queue.maxsize,
            compressionWrites=self.compression_writes, maxBatchRows=self.max_batch_rows,
            maxBatchEncodedBytes=self.max_batch_encoded_bytes, closed=self.closed,
            writerAlive=self.thread.is_alive(), codecPid=self.codec.pid,
            codecAlive=self.codec.process.is_alive(), hashedRows=self.hashed_rows,
            codecError=self.codec.error)

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
            # A blocked pipe must not leave a child running after finalization.
            self.codec.abort()
            self.thread.join(2)
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
        self._close_task = None
        self._final_states = None
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
        if self.started or self.error or self.finished or self._close_task is not None:
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
        if "targetNetReturnFraction" in PROFILES.get(self.profile, {}):
            data["exam"] = {"durationSeconds": PROFILES[self.profile]["seconds"],
                            "targetNetReturnFraction": PROFILES[self.profile]["targetNetReturnFraction"]}
        temporary = self.metadata_path.with_suffix(".pending")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.metadata_path)

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
            if self.error or (self.started and not self.engine.running):
                await self.close()
                return

    def state(self, symbol=None):
        # Reading the engine after its footer would append clock/scope inputs
        # to a closed journal. Serve immutable final views while the UI stays up.
        if self._final_states is not None:
            return deepcopy(self._final_states.get(symbol, self._final_states[None]))
        return self.engine.public_state(symbol)

    async def close(self):
        # Auto-stop and ASGI shutdown may race. Cancellation of the monitor
        # must not cancel the only task draining writers and sealing the file.
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="capture-close")
        await asyncio.shield(self._close_task)

    async def _close(self):
        if self.engine.running:
            self.engine._stop_trading("shutdown")
        if not self.engine.running:
            self._final_states = {
                symbol: deepcopy(self.engine.public_state(symbol))
                for symbol in [None, *self.engine.sessions]
            }
        try:
            self._write_status("sealing")
        except OSError:
            self.error = self.error or "capture sealing metadata could not be saved"
        try:
            await self.engine.close()
        except Exception as exc:
            self.error = self.error or f"capture shutdown failed: {type(exc).__name__}"
            raise
        finally:
            self.finish()
        if self._final_states is not None:
            for state in self._final_states.values():
                state["recorderHealth"] = self.recorder.health()
                summary = state.get("run", {}).get("lastSummary")
                if summary:
                    state["run"]["elapsedSeconds"] = summary["elapsedSeconds"]

    def public(self):
        done = self.started and not self.engine.running
        return dict(profile=self.profile,
            startAllowed=not (self.started or self.error or self.finished or self._close_task is not None),
            strategiesLocked=True, error=self.error,
            status=("invalid" if self.error else "sealed" if self.finished
                    else "sealing" if self._close_task is not None else "recording"),
            message=("Запись повреждена: " + self.error if self.error else
                     "Запись сохранена. Графики и сделки показывают итог прогона." if self.finished else
                     "Прогон завершён. Сохраняем запись автоматически…" if done else
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
        try:
            self._write_status("incomplete" if self.error else "sealed")
        except OSError:
            # Never advertise a saved capture when its completion marker could
            # not be persisted, even if the market writers finished successfully.
            self.error = self.error or "capture completion metadata could not be saved"
            raise


def from_environment(config, environ):
    profile = environ.get("SCALP_CAPTURE_PROFILE", "")
    return PaperCapture(config, profile) if profile else None
