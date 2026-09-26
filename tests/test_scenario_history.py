"""All six controls plus older winners; explicitly bounded snapshot evidence."""
import importlib.util
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location('scenario_history', Path(__file__).resolve().parents[1]/'scripts/check-scenario-episodes.py')
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)


@pytest.fixture(scope='module')
def report():
    return history.run()


def test_every_audited_case_and_older_large_winners_are_retained(report):
    assert [r['id'] for r in report['sixControls']] == [f'T{i:02}' for i in range(1,7)]
    assert len(report['olderWinners']) == 2
    assert [r['originalNet'] for r in report['olderWinners']] == pytest.approx([44.745188168918716,16.58123588167094])
    assert not report['fullPortfolioReplay'] and not report['profitabilityProven']
    assert all(r['newNet'] is None and r['newEntry'] is None and r['newExit'] is None
               for r in report['sixControls']+report['olderWinners'])


def test_t06_recorded_retry_cannot_move_target_to_pass_risk(report):
    r = report['t06Retry']
    assert r['frozenTarget'] == r['firstTarget'] == pytest.approx(4.8082684)
    assert r['oldLastTarget'] == pytest.approx(4.837100668830376)
    assert r['firstSignalMono'] == 0
    assert not r['newLastTradeable'] and r['state']=='INVALIDATED'


def test_t03_correction_is_separate_from_strategy_and_keeps_profitable_stop(report):
    r = report['t03Execution']
    assert r['correctedFill'] == pytest.approx(4.7874787)
    assert r['finalQuantity'] == pytest.approx(145.46)
    assert r['originalNet'] == pytest.approx(.8355616037618623)
    assert r['correctedSameFinalLegNet'] == pytest.approx(.39889793276082325)
    assert r['executionQuality']['deepQuoteConflict']
    assert report['sixControls'][2]['originalExit']=='stop'
