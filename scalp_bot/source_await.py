"""Observation boundaries around source awaits and their diagnostic outcomes."""
import asyncio
from contextlib import contextmanager
from .source_failure import describe_source_error


@contextmanager
def source_await(engine, source, symbol=None):
    engine._source_request_serial += 1
    identity = engine._source_request_serial
    body = dict(id=identity, source=source)
    engine._record_input('source_await', symbol, {**body, 'phase': 'wait'})
    try:
        yield identity
    except asyncio.CancelledError:
        engine._record_input('source_await', symbol, {**body, 'phase': 'cancelled'})
        raise
    except Exception as exc:
        failure = describe_source_error(exc)
        engine._record_input('source_await', symbol, {**body, 'phase': 'failed',
            'errorType': failure.error_type, 'errorMessage': failure.message})
        raise
    except BaseException:
        engine._record_input('source_await', symbol, {**body, 'phase': 'raised'})
        raise
    else:
        engine._record_input('source_await', symbol, {**body, 'phase': 'ready'})
