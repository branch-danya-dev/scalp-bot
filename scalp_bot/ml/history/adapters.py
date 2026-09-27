"""Streaming CSV/gzip adapters. Preserve file order and exact decimal prices.

Tardis arrival time belongs to the vendor, never to our bot. Bybit public trades
have no arrival clock. L2 snapshots are buffered until the consecutive snapshot
block ends; deltas are grouped by local_timestamp, not exchange timestamp.
"""
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import gzip
from pathlib import Path
from typing import Iterator

from .sources import ArchiveSpec


class ArchiveError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ReadLimits:
    max_rows: int = 50_000_000
    max_text_bytes: int = 8 * 1024**3
    max_batch_rows: int = 50_000
    max_line_chars: int = 65_536

    def __post_init__(self):
        for name in ("max_rows", "max_text_bytes", "max_batch_rows", "max_line_chars"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError("read limits must be positive integers")


def decimal_text(text: str, *, zero=False) -> str:
    try:
        if not text or len(text) > 80:
            raise ArchiveError("invalid numeric field")
        value = Decimal(text)
        if not value.is_finite() or value < 0 or (not zero and value == 0) or abs(value.adjusted()) > 30:
            raise ArchiveError("numeric field outside supported range")
        if value == 0:
            return "0"
        result = format(value, "f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    except InvalidOperation as exc:
        raise ArchiveError("invalid decimal") from exc


def timestamp_us(text: str, *, seconds=False) -> int:
    try:
        if not text or len(text) > 40:
            raise ArchiveError("invalid timestamp")
        with localcontext() as ctx:
            ctx.prec = 50
            value = Decimal(text) * (1_000_000 if seconds else 1)
        if not value.is_finite() or value != value.to_integral_value() or not 0 < value < 4_102_444_800_000_000:
            raise ArchiveError("timestamp must be exact epoch microseconds")
        return int(value)
    except InvalidOperation as exc:
        raise ArchiveError("invalid timestamp") from exc


def _lines(stream, limits):
    total = 0
    while True:
        line = stream.readline(limits.max_line_chars + 1)
        if not line:
            return
        total += len(line.encode("utf-8"))
        if len(line) > limits.max_line_chars or total > limits.max_text_bytes:
            raise ArchiveError("decompressed text limit exceeded")
        yield line


def read_rows(path: Path, spec: ArchiveSpec, limits: ReadLimits) -> Iterator[dict]:
    required = ({"timestamp", "symbol", "side", "size", "price", "trdMatchID"}
                if spec.provider == "bybit-public-trades" else
                {"exchange", "symbol", "timestamp", "local_timestamp", "side", "price", "amount"}
                | ({"is_snapshot"} if spec.provider == "tardis-l2" else {"id"}))
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(_lines(stream, limits), strict=True)
        header = reader.fieldnames or []
        if len(header) != len(set(header)) or not required.issubset(header):
            raise ArchiveError("missing/duplicate columns; unsupported archive schema")
        count = 0
        previous_arrival = None
        for ordinal, row in enumerate(reader, 1):
            count += 1
            try:
                if ordinal > limits.max_rows:
                    raise ArchiveError("row limit exceeded; partial import is not a dataset")
                if None in row or any(v is None for v in row.values()):
                    raise ArchiveError("malformed CSV record")
                if row["symbol"] != spec.symbol or (spec.has_arrival_time and row["exchange"] != "bybit"):
                    raise ArchiveError("instrument/exchange mismatch")
                exchange_us = timestamp_us(row["timestamp"], seconds=not spec.has_arrival_time)
                arrival_us = timestamp_us(row["local_timestamp"]) if spec.has_arrival_time else None
                partition_us = arrival_us if arrival_us is not None else exchange_us
                if not spec.day_start_us <= partition_us < spec.day_start_us + 86_400_000_000:
                    raise ArchiveError("record outside declared UTC partition")
                if arrival_us is not None and previous_arrival is not None and arrival_us < previous_arrival:
                    raise ArchiveError("arrival clock regressed; no silent sort/repair")
                previous_arrival = arrival_us
                is_book = spec.provider == "tardis-l2"
                side = row["side"]
                if not spec.has_arrival_time:
                    side = {"Buy": "buy", "Sell": "sell"}.get(side)
                if side not in ({"bid", "ask"} if is_book else {"buy", "sell", "unknown"}):
                    raise ArchiveError("unknown side encoding")
                snap = row.get("is_snapshot")
                if is_book and snap not in {"true", "false"}:
                    raise ArchiveError("invalid snapshot flag")
                trade_id = None if is_book else row["id" if spec.has_arrival_time else "trdMatchID"] or None
                if trade_id is not None and len(trade_id) > 256:
                    raise ArchiveError("trade id too long")
                yield dict(row=ordinal, exchange_time_us=exchange_us, available_time_us=arrival_us,
                           side=side, price=decimal_text(row["price"]),
                           amount=decimal_text(row["amount" if spec.has_arrival_time else "size"], zero=is_book),
                           trade_id=trade_id, is_snapshot=snap == "true" if is_book else None)
            except (ArchiveError, KeyError, TypeError) as exc:
                raise ArchiveError(f"record {ordinal}: {exc}") from exc
        if not count:
            raise ArchiveError("archive contains no records")


def normalized_events(path: Path, spec: ArchiveSpec, limits: ReadLimits) -> Iterator[dict]:
    pending = []
    sequence = 0

    def batch():
        first, last = pending[0], pending[-1]
        return dict(kind="book_batch", source_sequence=sequence, row_start=first["row"], row_end=last["row"],
                    exchange_time_us=last["exchange_time_us"], available_time_us=last["available_time_us"],
                    is_snapshot=first["is_snapshot"],
                    changes=[{key: row[key] for key in ("side", "price", "amount", "exchange_time_us")}
                             for row in pending])

    for row in read_rows(path, spec, limits):
        if spec.provider != "tardis-l2":
            sequence += 1
            yield {key: value for key, value in row.items() if key not in {"row", "is_snapshot"}} | dict(
                kind="trade", source_sequence=sequence, row_start=row["row"], row_end=row["row"])
            continue
        if pending:
            same = ((pending[0]["is_snapshot"] and row["is_snapshot"]) or
                    (not pending[0]["is_snapshot"] and not row["is_snapshot"] and
                     pending[-1]["available_time_us"] == row["available_time_us"]))
            if not same:
                sequence += 1
                yield batch()
                pending = []
        pending.append(row)
        if len(pending) > limits.max_batch_rows:
            raise ArchiveError("L2 batch limit exceeded; refusing incomplete snapshot")
    if pending:
        sequence += 1
        yield batch()
