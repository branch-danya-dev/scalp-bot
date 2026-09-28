"""Real spawned V2 and relay playback on the shared causal coordinator.

Only child/relay threads wait for clock replies. Parent callbacks fail at an
unsupported synchronous cross-actor boundary instead of blocking asyncio or
locally recomputing a prediction. This bridge does not claim whole-engine
coverage: a production dispatcher must grant each parent slice before entry.
"""
import asyncio
import hashlib
import multiprocessing as mp
from pathlib import Path
from queue import Empty

from ..manifest_validation import fingerprint
from ..native_v5 import NativeEndpoint, NativeTapeError
from .worker import InferenceWorker


class _ActorRPC:
    def __init__(self, coordinator, producer):
        context = mp.get_context("spawn")
        self.requests = context.Queue(maxsize=2)
        self.responses = {name: context.Queue(maxsize=1) for name in ("inference-worker", "reply-relay")}
        self.coordinator = coordinator
        self.parent_producer = producer
        self.state = context.RawArray("Q", 7)
        self.lock = context.Lock()

    def __getstate__(self):
        return {**self.__dict__, "coordinator": None}

    def put(self, payload, observation=None):
        method = observation[0] if observation is not None else None
        producer = payload[1]
        if producer == self.parent_producer:
            return self._parent(payload, method)
        if producer not in self.responses:
            raise NativeTapeError("undeclared replay actor producer")
        self.requests.put((payload, method), timeout=5)
        try:
            ok, value = self.responses[producer].get(timeout=10)
        except Empty as exc:
            raise NativeTapeError("replay actor clock/dispatch response timeout") from exc
        if not ok:
            raise NativeTapeError(value)
        return value

    def _parent(self, payload, method):
        c = self.coordinator
        if c is None or c.index >= len(c.rows):
            raise NativeTapeError("unexpected parent replay observation")
        if c.failure:
            raise c.failure
        row = c.rows[c.index]
        module, producer, task, parent, source, kind, data = payload
        if (row["module_id"], row["producer"], row["task_id"], row["parent_task_id"], row["source"], row["kind"]) != (
                module, producer, task, parent, source, kind):
            raise NativeTapeError(f"parent slice crossed actor boundary at token {row['sequence']}; async dispatch grant required")
        if method is not None:
            if row["data"]["method"] != method:
                raise NativeTapeError("parent replay clock method mismatch")
            value = row["data"]["value"]
        else:
            if fingerprint(data) != fingerprint(row["data"]):
                raise NativeTapeError(f"parent replay boundary/output mismatch at {row['sequence']}")
            value = None
        c.index += 1
        return row["sequence"], value

    def close(self):
        for queue in (self.requests, *self.responses.values()):
            queue.close()
            queue.join_thread()


class NativeChildReplayBridge:
    def __init__(self, coordinator, model_dir, *, producer="parent", target=None, expected_model_hashes=None):
        self.coordinator = coordinator
        self.model_dir = Path(model_dir)
        self.expected_model_hashes = expected_model_hashes
        if target is None and expected_model_hashes is None:
            raise NativeTapeError("real V2 replay requires recorded model/manifest hashes")
        self._check_model()
        self.rpc = _ActorRPC(coordinator, producer)
        endpoint = NativeEndpoint(self.rpc, "v2", producer)
        self.worker = InferenceWorker(model_dir, native_endpoint=endpoint,
            **({"_target": target} if target is not None else {}))
        self.stopping = False
        self.server = None
        self.requests = set()
        self.consumed = 0

    def _check_model(self):
        if self.expected_model_hashes is not None:
            if set(self.expected_model_hashes) != {"model.cbm", "manifest.json"}:
                raise NativeTapeError("incomplete replay model provenance")
            actual = {name:hashlib.sha256((self.model_dir/name).read_bytes()).hexdigest()
                for name in self.expected_model_hashes}
            if actual != self.expected_model_hashes:
                raise NativeTapeError("replay model/manifest hash mismatch")

    async def _observe(self, payload, method):
        module, producer, task, parent, source, kind, data = payload
        try:
            declarations = self.declarations
            expected = declarations.get(task)
            if expected is None or (module, parent, source) != (
                    expected["module_id"], expected["parent_task_id"], expected["source"]):
                raise NativeTapeError("child/relay source or task ownership mismatch")
            value = await self.coordinator.consume(task, kind, data, producer=producer, clock_method=method)
            self.consumed += 1
            response = (True, (self.coordinator.index, value))
        except BaseException as exc:
            self.coordinator.failure = self.coordinator.failure or exc
            response = (False, str(exc))
        self.rpc.responses[producer].put_nowait(response)

    async def _serve(self):
        while not self.stopping:
            try:
                payload, method = await asyncio.to_thread(self.rpc.requests.get, True, .05)
            except Empty:
                continue
            task = asyncio.create_task(self._observe(payload, method))
            self.requests.add(task)
            task.add_done_callback(self.requests.discard)

    async def start(self):
        self.declarations = {r["task_id"]: r for r in self.coordinator.rows if r["kind"] == "task_open"}
        self.server = asyncio.create_task(self._serve())
        self.worker.start()
        # Startup is diagnostic for this explicit-request bridge. Full runtime
        # startup/poll ownership belongs to the extracted production probe.
        async with asyncio.timeout(30):
            while not self.worker.ready:
                self.worker.poll()
                if self.worker.failed:
                    raise NativeTapeError(self.worker.failed)
                await asyncio.sleep(.001)

    async def request(self, ctx, snapshot, side):
        task = self.worker.native_endpoint.attach_task(ctx.task_id,
            parent_task_id=ctx.parent_task_id, source=ctx.source)
        self.worker.activate(snapshot.ref.symbol, snapshot.ref.selection_epoch)
        if not self.worker.submit(snapshot, side, native_task=task):
            raise NativeTapeError("recorded request was rejected during child replay")
        async with self.coordinator.changed:
            self.coordinator.changed.notify_all()
        async with asyncio.timeout(10):
            while True:
                if self.coordinator.failure:
                    raise self.coordinator.failure
                c = self.coordinator
                row = c.rows[c.index] if c.index < len(c.rows) else None
                if (row is not None and row["task_id"] == ctx.task_id
                        and row["producer"] == ctx.producer and self._reply_ready()):
                    result = self.worker.poll()
                    if result:
                        if result[0][0] != "forecast":
                            raise NativeTapeError("replayed prediction failed: "+str(result[0]))
                        self.worker.native_tasks.pop(ctx.task_id)
                        return result[0][1]
                await asyncio.sleep(.001)

    def _reply_ready(self):
        # Queue.empty/get_nowait still acquire Queue's mutex. The relay may be
        # holding it while waiting for its owned replay clock: a blocking mutex
        # acquisition here would deadlock the coordinator's event loop.
        queue = self.worker.received
        if not queue.mutex.acquire(blocking=False):
            return False
        try:
            return queue._qsize() > 0
        finally:
            queue.mutex.release()

    async def close(self):
        try:
            await asyncio.to_thread(self.worker.close)
        finally:
            self.stopping = True
            if self.server is not None:
                await self.server
            if self.requests:
                await asyncio.gather(*self.requests)
            self.rpc.close()
        if self.worker.process.is_alive() or self.worker.receiver.is_alive():
            raise NativeTapeError("replay child/relay not joined")
        if self.worker.native_error or self.worker.process.exitcode != 0:
            raise NativeTapeError(self.worker.native_error or "replay child exited abnormally")
        self._check_model()
        self.coordinator.ipc_receipts.append(dict(pid=self.worker.process.pid,
            exitCode=self.worker.process.exitcode, joined=True, relayJoined=True,
            remoteTokensConsumed=self.consumed, modelHashes=self.expected_model_hashes))
