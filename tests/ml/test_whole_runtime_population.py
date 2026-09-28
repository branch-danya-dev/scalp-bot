import pytest

from scalp_bot.ml.whole_runtime_fixture import CAPTURE_ID, WholeRuntimePopulation
from scalp_bot.native_dispatch import NativeDispatch
from scalp_bot.native_v5 import NativeTapeWriter, provenance, read_native_tape
from scalp_bot.whole_runtime_replay import WholeRuntimeReplayCoordinator


async def test_whole_production_source_task_graph_replays(tmp_path):
    population = WholeRuntimePopulation(tmp_path/'capture',trade_path=False)
    with NativeTapeWriter(tmp_path/'tape.gz',capture_id=CAPTURE_ID+'.jsonl',
            provenance=provenance(source_sha256='a'*64,config={},runtime={})) as writer:
        dispatch = NativeDispatch(writer)
        await dispatch.run(population.run(dispatch))
        await dispatch.join()
    tape = read_native_tape(tmp_path/'tape.gz')
    expected = population.result()
    for i in range(2):
        replay = WholeRuntimePopulation(tmp_path/f'replay-{i}',trade_path=False)
        coordinator = WholeRuntimeReplayCoordinator(tape,timeout_seconds=5)
        report = await coordinator.run(replay.run)
        actual = replay.result()
        assert actual['sourceSha256'] == expected['sourceSha256']
        assert actual['ordinaryHashes'] == expected['ordinaryHashes']
        assert actual['labelSha256'] == expected['labelSha256']
        assert report['consumed'] == len(tape.events)
        assert not report['productionCoverage']
