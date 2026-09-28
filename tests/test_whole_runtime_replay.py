import asyncio
from copy import deepcopy

import pytest

from scalp_bot.native_dispatch import NativeDispatch
from scalp_bot.native_v5 import NativeTapeError, NativeTapeWriter, provenance, read_native_tape
from scalp_bot.runtime_clock import ReplayRuntimeClock


async def population(dispatch, output):
    async def child():
        with dispatch.scope('segment', 'nested'):
            output.append(('nested', dispatch.clock().time_ns()))
            await asyncio.sleep(0)
            output.append(('nested-resume', dispatch.clock().monotonic()))
    async def cancelled():
        raise AssertionError('cancelled coroutine must not run')
    task = dispatch.create_task(child(), name='child')
    unused = dispatch.create_task(cancelled(), name='prestart-cancel')
    unused.cancel()
    dispatch.add_done_callback(task, lambda done: output.append(
        ('callback', dispatch.clock().perf_counter_ns())), name='done')
    output.append(('root', dispatch.clock().time()))
    await asyncio.gather(task, unused, return_exceptions=True)
    await asyncio.sleep(0)
    output.append(('end', dispatch.clock().time()))


async def record(tmp_path, root=population):
    path = tmp_path/'native.gz'
    with NativeTapeWriter(path, capture_id='whole-runtime-test',
            provenance=provenance(source_sha256='a'*64, config={}, runtime={})) as writer:
        class Clock(ReplayRuntimeClock):
            def time_ns(self): return 100_000_000_000
        dispatch = NativeDispatch(writer, clock=Clock(wall_seconds=100, mono_ns=123))
        outputs = []
        await dispatch.run(root(dispatch, outputs))
        await dispatch.join()
    return read_native_tape(path), outputs


async def test_actual_coroutine_roots_repeat_with_nested_await_and_callbacks(tmp_path):
    from scalp_bot.whole_runtime_replay import WholeRuntimeReplayCoordinator
    tape, expected = await record(tmp_path)
    for _ in range(2):
        replay = WholeRuntimeReplayCoordinator(tape)
        outputs = []
        report = await replay.run(lambda dispatch: population(dispatch, outputs))
        assert outputs == expected
        assert report['consumed'] == len(tape.events)
        assert report['leftovers'] == 0
        assert report['accountingComplete']
        assert not report['productionCoverage']


async def test_wrong_owned_clock_is_first_failure(tmp_path):
    from scalp_bot.whole_runtime_replay import WholeRuntimeReplayCoordinator
    async def root(dispatch, output):
        output.append(dispatch.clock().time())
    tape, _ = await record(tmp_path, root)
    async def wrong(dispatch):
        dispatch.clock().monotonic()
    with pytest.raises(NativeTapeError, match='clock method'):
        await WholeRuntimeReplayCoordinator(tape).run(wrong)


async def test_unknown_parent_and_duplicate_terminal_fail_before_execution(tmp_path):
    from scalp_bot.whole_runtime_replay import WholeRuntimeReplayCoordinator
    tape, _ = await record(tmp_path)
    bad = deepcopy(tape)
    bad.events[0]['parent_task_id'] = 'unknown'
    with pytest.raises(NativeTapeError, match='parent'):
        WholeRuntimeReplayCoordinator(bad)
    bad = deepcopy(tape)
    bad.events[-1]['task_id'] = bad.events[0]['task_id']
    bad.events.append(dict(bad.events[-1], sequence=len(bad.events)+1))
    with pytest.raises(NativeTapeError):
        WholeRuntimeReplayCoordinator(bad)


async def test_sync_slice_yields_to_other_producer_without_blocking_event_loop(tmp_path):
    from threading import Thread
    from scalp_bot.native_v5 import OwnedClock
    from scalp_bot.whole_runtime_replay import WholeRuntimeReplayCoordinator
    async def root(dispatch, output):
        remote = dispatch.writer.endpoint('v2', 'reply-relay').open_task(
            'relay-lifetime', 'runtime', parent_task_id=dispatch.owner().task_id)
        def actor():
            remote.start()
            output.append(('remote', OwnedClock(remote, dispatch.source_clock).time()))
            remote.end()
        if isinstance(dispatch.writer.ring, type(None)):
            raise AssertionError('missing sequencer')
        coordinator = getattr(dispatch, 'coordinator', None)
        if coordinator is None:
            # Test-only rendezvous forces a producer interleave *inside* the
            # captured synchronous slice. Replay must never use this join.
            thread = Thread(target=actor)
            thread.start()
            thread.join(1)
            assert not thread.is_alive()
        else:
            async def replay_actor():
                await asyncio.sleep(.01)  # Parent must yield for this to run.
                await coordinator.consume('relay-lifetime', 'task_start', {}, producer='reply-relay')
                value = await coordinator.consume('relay-lifetime', 'clock', clock_method='time', producer='reply-relay')
                output.append(('remote', value))
                await coordinator.consume('relay-lifetime', 'task_end', dict(reason='completed', outputs={}), producer='reply-relay')
            coordinator.remote = asyncio.create_task(replay_actor())
        output.append(('parent-after-remote', dispatch.clock().time()))
    tape, expected = await record(tmp_path, root)
    replay = WholeRuntimeReplayCoordinator(tape, timeout_seconds=1)
    actual = []
    report = await replay.run(lambda d: root(d, actual))
    await replay.remote
    assert actual == expected
    assert report['consumed'] == len(tape.events)


async def test_disabled_owned_scope_projection_and_leftovers(tmp_path):
    from scalp_bot.whole_runtime_replay import WholeRuntimeReplayCoordinator
    async def root(dispatch, output, enabled=True):
        dispatch.clock().time()
        if enabled:
            with dispatch.scope('segment', 'assess'):
                dispatch.clock().time()
        dispatch.clock().time()
    tape, _ = await record(tmp_path, root)
    report = await WholeRuntimeReplayCoordinator(tape, variant='A').run(lambda d:root(d,[],False))
    assert report['excludedOwnedTokens'] == 4
    assert report['accountingComplete']
    with pytest.raises(NativeTapeError, match='disabled module'):
        await WholeRuntimeReplayCoordinator(tape, variant='A').run(lambda d:root(d,[]))


async def test_divergent_suspend_closes_coroutine_before_owner_is_reset(tmp_path):
    from scalp_bot.whole_runtime_replay import WholeRuntimeReplayCoordinator
    async def captured(dispatch, output):
        dispatch.clock().time()
    tape, _ = await record(tmp_path,captured)
    cleaned = []
    async def divergent(dispatch):
        try:
            await asyncio.sleep(0)
        finally:
            cleaned.append(dispatch.owner().task_id)
    replay = WholeRuntimeReplayCoordinator(tape)
    with pytest.raises(NativeTapeError,match='first divergence'):
        await replay.run(divergent)
    assert cleaned == ['runtime:core:1:session']
    assert not replay.stacks


async def test_undeclared_runtime_root_hard_fails(tmp_path):
    from scalp_bot.whole_runtime_replay import WholeRuntimeReplayCoordinator
    async def captured(dispatch, output):
        dispatch.clock().time()
    tape, _ = await record(tmp_path,captured)
    async def wrong(dispatch):
        await dispatch.create_task(asyncio.sleep(0),name='undeclared')
    with pytest.raises(NativeTapeError,match='unknown replay owner'):
        await WholeRuntimeReplayCoordinator(tape).run(wrong)


async def test_impossible_resume_has_bounded_failure_and_no_live_stack(tmp_path):
    from scalp_bot.whole_runtime_replay import WholeRuntimeReplayCoordinator
    async def captured(dispatch, output):
        await asyncio.sleep(0)
        dispatch.clock().time()
    tape, _ = await record(tmp_path,captured)
    async def wrong(dispatch):
        await asyncio.Event().wait()
        dispatch.clock().time()
    replay = WholeRuntimeReplayCoordinator(tape,timeout_seconds=.03)
    with pytest.raises(NativeTapeError):
        await asyncio.wait_for(replay.run(wrong),1)
    assert not replay.stacks
