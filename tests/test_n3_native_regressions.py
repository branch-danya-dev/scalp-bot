import pytest

from scalp_bot.config import Settings
from scalp_bot.input_scope import InputScopes
from scalp_bot.offline_segment import _Cursor, SegmentMismatch
from scalp_bot.run_manifest import build_run_manifest


def test_capture_rejects_unvalidated_config_before_recording_unreplayable_manifest():
    # Exact defect in the preserved 300s smoke: model_copy bypasses float validation.
    config = Settings(_env_file=None).model_copy(update={'paper_run_duration_seconds': 300})
    with pytest.raises(ValueError, match='replay-stable'):
        build_run_manifest(config, {}, code={}, policy={})


def test_native_first_clock_divergence_survives_scope_cleanup():
    rows = [dict(sequence=41, kind='scope', symbol='AAA',
                 body=dict(phase='begin', id=1, parentId=None, name='evaluate')),
            dict(sequence=42, kind='clock_read', symbol=None,
                 body=dict(scopeId=1, method='time', value=100.0))]
    cursor = _Cursor(rows)
    scopes = InputScopes(cursor)
    cursor.scopes = scopes
    with pytest.raises(SegmentMismatch) as caught:
        with scopes.enter('evaluate', 'AAA'):
            cursor.monotonic()
    error = caught.value
    assert 'monotonic' in str(error) and 'time' in str(error)
    assert error.receipt['sequence'] == 42
    assert error.receipt['actual']['method'] == 'monotonic'
    assert error.receipt['expected']['body']['method'] == 'time'
    assert cursor.index == 1
    with pytest.raises(SegmentMismatch) as repeated:
        cursor.time()
    assert repeated.value is error  # A mismatched tape can never resume.
