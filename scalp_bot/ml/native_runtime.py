"""Prospective full worker/probe control inputs and real actor replay transport."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from queue import Empty

from ..native_v5 import NativeTapeError
from .native_bridge import _ActorRPC


def model_hashes(directory):
    return {name:hashlib.sha256((Path(directory)/name).read_bytes()).hexdigest()
        for name in ('model.cbm','manifest.json')}


def _reply_identity(item):
    if item is None:
        return dict(kind=None, request=None)
    timing = item[-1].get('pipelineTiming') if isinstance(item[-1],dict) else None
    return dict(kind=item[0],request=(timing or {}).get('nativeTask',{}).get('task_id'))


class NativeWorkerRuntime:
    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.coordinator = getattr(dispatch, 'coordinator', None)
        self.descriptors = {}

    def open(self):
        parent = self.dispatch.owner()
        identity = self.dispatch.identifier('worker_lifetime')
        for producer in ('inference-worker','reply-relay'):
            task = self.dispatch.writer.endpoint('v2',producer).open_task(
                f'worker:{identity}:{producer}', 'runtime', parent_task_id=parent.task_id,
                inputs=dict(name=producer, values={}))
            self.descriptors[producer] = dict(task_id=task.task_id,
                parent_task_id=task.parent_task_id,source=task.source)

    def take_reply(self, worker):
        native = self.dispatch.owner()
        if self.coordinator is None:
            try: item = worker.received.get_nowait()
            except Empty: item = None
            native.boundary('worker_reply_available',_reply_identity(item))
            return item
        expected = self.coordinator.worker_input(native,'worker_reply_available')
        if expected == dict(kind=None,request=None):
            return None
        # The relay may still be holding Queue's mutex while awaiting a native
        # grant. No mutex acquisition or blocking receive runs on the event loop.
        try:
            item = self.coordinator.suspend(asyncio.to_thread(worker.received.get,True,5))
        except Empty as exc:
            raise NativeTapeError('recorded real worker reply did not arrive') from exc
        if _reply_identity(item) != expected:
            raise NativeTapeError('real worker reply/control identity mismatch')
        return item

    def process_state(self, worker):
        actual = dict(alive=worker.process.is_alive(),receiveError=worker.receive_error)
        native = self.dispatch.owner()
        if self.coordinator is None:
            native.boundary('worker_process_state',actual)
        else:
            expected = self.coordinator.worker_input(native,'worker_process_state')
            if actual != expected:
                raise NativeTapeError('real worker process state mismatch')
        return actual

    def join(self, owner, timeout):
        if self.coordinator is None:
            owner.join(timeout)
        else:
            self.coordinator.suspend(asyncio.to_thread(owner.join,timeout))


class NativeRuntimeActorTransport:
    """One RPC transport for the existing production worker's full lifetime."""
    def __init__(self, coordinator, model_dir, *, expected_model_hashes):
        self.coordinator, self.model_dir = coordinator, Path(model_dir)
        self.expected = expected_model_hashes
        self._check_model()
        self.rpc = _ActorRPC(coordinator,'parent')
        coordinator.external_endpoints['v2','parent'] = self.rpc
        self.server = None
        self.pending = set()
        self.consumed = 0

    def _check_model(self):
        if self.expected is None or model_hashes(self.model_dir) != self.expected:
            raise NativeTapeError('whole-runtime model/manifest hash mismatch')

    async def start(self):
        self.server = asyncio.create_task(self._serve(),name='native-actor-rpc')

    async def _observe(self,payload,method):
        module,producer,task,parent,source,kind,data = payload
        try:
            self.coordinator._validate_owner(payload)
            value = await self.coordinator.consume(task,kind,data,
                producer=producer,clock_method=method)
            self.consumed += 1
            response = True,(self.coordinator.index,value)
        except BaseException as exc:
            self.coordinator.poison(exc)
            response = False,str(exc)
        self.rpc.responses[producer].put_nowait(response)

    async def _serve(self):
        while True:
            request = await asyncio.to_thread(self.rpc.requests.get)
            if request is None:
                return
            task = asyncio.create_task(self._observe(*request))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

    async def close(self,worker=None):
        # Caller must close/join the actual production worker before transport.
        if worker is not None:
            if worker.process.is_alive() or worker.receiver.is_alive():
                raise NativeTapeError('whole-runtime worker or relay not joined')
            if worker.native_error or worker.process.exitcode != 0:
                raise NativeTapeError(worker.native_error or 'whole-runtime child exited abnormally')
        self.rpc.requests.put_nowait(None)
        if self.server is not None:
            await self.server
        if self.pending:
            await asyncio.gather(*self.pending)
        self.rpc.close()
        self._check_model()
        if worker is not None:
            self.coordinator.ipc_receipts.append(dict(pid=worker.process.pid,
                exitCode=worker.process.exitcode,joined=True,relayJoined=True,
                remoteTokensConsumed=self.consumed,modelHashes=self.expected))

    async def abort_worker(self,worker):
        """Failure-only teardown; no observations are consumed or repaired."""
        if self.coordinator.failure is None:
            raise NativeTapeError('worker abort requires invalid replay')
        worker.receiver_stop.set()
        if worker.process is not None:
            if worker.process.is_alive():
                worker.process.terminate()
            await asyncio.to_thread(worker.process.join,2)
            if worker.process.is_alive():
                raise NativeTapeError('invalid replay child failed to terminate')
        if worker.receiver is not None:
            await asyncio.to_thread(worker.receiver.join,12)
            if worker.receiver.is_alive():
                raise NativeTapeError('invalid replay relay failed to terminate')
        for queue in (worker.inbox,worker.outbox):
            queue.cancel_join_thread()
            queue.close()
        worker.closed = True
