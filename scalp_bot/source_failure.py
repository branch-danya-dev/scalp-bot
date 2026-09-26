"""Serializable source failure observation, not an executable exception class."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceFailure:
    error_type: str
    message: str = ''


class RecordedSourceError(Exception):
    """Fixed control-flow carrier for a recorded failure, never a dynamic class."""
    def __init__(self, failure: SourceFailure):
        super().__init__(failure.message)
        self.failure = failure


def describe_source_error(exc: Exception) -> SourceFailure:
    if isinstance(exc, RecordedSourceError):
        return exc.failure
    return SourceFailure(type(exc).__name__, str(exc))
