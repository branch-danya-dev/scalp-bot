from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any

from ..domain import Candle, TradeTick, Trend


@dataclass(frozen=True, slots=True)
class FormingCandleContext:
    start_ms: int
    observed_at_ms: int
    age_seconds: float
    progress_ratio: float
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float
    body_pct: float
    range_pct: float
    body_to_range: float
    upper_wick_pct: float
    lower_wick_pct: float
    close_position: float
    volume_pace_ratio: float | None
    range_expansion_ratio: float | None
    velocity_bps_per_second: float
    direction: Trend
    price_source: str = "kline"
    volume_source: str = "kline_snapshot"
    tape_updates: int = 0
    current_minute_trade_count: int = 0
    last_trade_ts_ms: int | None = None
    last_trade_age_seconds: float | None = None
    kline_snapshot_age_seconds: float | None = None
    micro_move_5s_bps: float | None = None
    micro_range_5s_bps: float | None = None
    micro_move_15s_bps: float | None = None
    micro_range_15s_bps: float | None = None

    def public(self) -> dict[str, Any]:
        return {
            "startMs": self.start_ms,
            "observedAtMs": self.observed_at_ms,
            "ageSeconds": self.age_seconds,
            "progressRatio": self.progress_ratio,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "turnover": self.turnover,
            "bodyPct": self.body_pct,
            "rangePct": self.range_pct,
            "bodyToRange": self.body_to_range,
            "upperWickPct": self.upper_wick_pct,
            "lowerWickPct": self.lower_wick_pct,
            "closePosition": self.close_position,
            "volumePaceRatio": self.volume_pace_ratio,
            "rangeExpansionRatio": self.range_expansion_ratio,
            "velocityBpsPerSecond": self.velocity_bps_per_second,
            "direction": self.direction.value,
            "priceSource": self.price_source,
            "volumeSource": self.volume_source,
            "tapeUpdates": self.tape_updates,
            "currentMinuteTradeCount": self.current_minute_trade_count,
            "lastTradeTsMs": self.last_trade_ts_ms,
            "lastTradeAgeSeconds": self.last_trade_age_seconds,
            "klineSnapshotAgeSeconds": self.kline_snapshot_age_seconds,
            "microMove5sBps": self.micro_move_5s_bps,
            "microRange5sBps": self.micro_range_5s_bps,
            "microMove15sBps": self.micro_move_15s_bps,
            "microRange15sBps": self.micro_range_15s_bps,
        }

    def fingerprint(self) -> tuple:
        return (
            self.start_ms,
            self.direction.value,
            round(self.progress_ratio, 1),
            round(self.close_position, 1),
            (
                round(self.volume_pace_ratio, 1)
                if self.volume_pace_ratio is not None
                else None
            ),
            (
                round(self.range_expansion_ratio, 1)
                if self.range_expansion_ratio is not None
                else None
            ),
            round(self.velocity_bps_per_second, 2),
        )


def _median_positive(values: list[float]) -> float | None:
    rows = [float(value) for value in values if value > 0]
    return float(median(rows)) if rows else None


def _micro_window(
    trades: list[TradeTick],
    *,
    observed_at_ms: int,
    seconds: int,
) -> tuple[float | None, float | None]:
    if len(trades) < 2:
        return None, None
    cutoff = observed_at_ms - seconds * 1000
    rows = [
        trade
        for trade in trades
        if cutoff <= trade.ts_ms <= observed_at_ms
    ]
    if len(rows) < 2:
        return None, None
    rows.sort(key=lambda trade: trade.ts_ms)
    first = rows[0].price
    last = rows[-1].price
    if first <= 0:
        return None, None
    move_bps = (last - first) / first * 10_000
    high = max(trade.price for trade in rows)
    low = min(trade.price for trade in rows)
    range_bps = (high - low) / first * 10_000
    return move_bps, range_bps


def build_forming_candle_context(
    forming: Candle | None,
    closed_1m: list[Candle],
    *,
    observed_at_ms: int,
    baseline_bars: int = 20,
    recent_trades: list[TradeTick] | None = None,
    tape_updates: int = 0,
    last_trade_ts_ms: int | None = None,
    kline_snapshot_observed_at_ms: int | None = None,
) -> FormingCandleContext | None:
    if forming is None or forming.confirmed or forming.open <= 0:
        return None

    elapsed_seconds = max(
        0.0,
        min(60.0, (observed_at_ms - forming.start_ms) / 1000),
    )
    effective_seconds = max(elapsed_seconds, 0.25)
    progress = max(0.0, min(1.0, elapsed_seconds / 60.0))

    body_pct = (forming.close - forming.open) / forming.open
    range_abs = max(0.0, forming.high - forming.low)
    range_pct = range_abs / forming.open
    body_abs = abs(forming.close - forming.open)
    body_to_range = body_abs / range_abs if range_abs > 0 else 0.0
    upper_wick = max(
        0.0,
        forming.high - max(forming.open, forming.close),
    )
    lower_wick = max(
        0.0,
        min(forming.open, forming.close) - forming.low,
    )
    upper_wick_pct = upper_wick / forming.open
    lower_wick_pct = lower_wick / forming.open
    close_position = (
        (forming.close - forming.low) / range_abs
        if range_abs > 0
        else 0.5
    )

    baseline = closed_1m[-max(1, baseline_bars):]
    baseline_volume = _median_positive(
        [candle.volume for candle in baseline]
    )
    baseline_range_pct = _median_positive(
        [
            (candle.high - candle.low) / candle.open
            for candle in baseline
            if candle.open > 0 and candle.high >= candle.low
        ]
    )

    volume_pace_ratio = None
    if baseline_volume is not None and baseline_volume > 0:
        current_rate = max(0.0, forming.volume) / effective_seconds
        baseline_rate = baseline_volume / 60.0
        if baseline_rate > 0:
            volume_pace_ratio = current_rate / baseline_rate

    range_expansion_ratio = (
        range_pct / baseline_range_pct
        if baseline_range_pct is not None and baseline_range_pct > 0
        else None
    )
    velocity_bps_per_second = (
        body_pct * 10_000 / effective_seconds
    )

    if abs(body_pct) < 1e-9:
        direction = Trend.FLAT
    else:
        direction = Trend.UP if body_pct > 0 else Trend.DOWN

    minute_trades = sorted(
        [
            trade
            for trade in (recent_trades or [])
            if (
                forming.start_ms
                <= trade.ts_ms
                < forming.start_ms + 60_000
                and trade.ts_ms <= observed_at_ms
            )
        ],
        key=lambda trade: trade.ts_ms,
    )
    resolved_last_trade_ts = (
        max(
            [
                value
                for value in (
                    last_trade_ts_ms,
                    (
                        minute_trades[-1].ts_ms
                        if minute_trades
                        else None
                    ),
                )
                if isinstance(value, int)
            ],
            default=None,
        )
    )
    last_trade_age_seconds = (
        max(
            0.0,
            (observed_at_ms - resolved_last_trade_ts) / 1000,
        )
        if resolved_last_trade_ts is not None
        else None
    )
    kline_snapshot_age_seconds = (
        max(
            0.0,
            (
                observed_at_ms
                - kline_snapshot_observed_at_ms
            )
            / 1000,
        )
        if kline_snapshot_observed_at_ms is not None
        else None
    )
    micro_move_5s_bps, micro_range_5s_bps = _micro_window(
        minute_trades,
        observed_at_ms=observed_at_ms,
        seconds=5,
    )
    micro_move_15s_bps, micro_range_15s_bps = _micro_window(
        minute_trades,
        observed_at_ms=observed_at_ms,
        seconds=15,
    )

    return FormingCandleContext(
        start_ms=forming.start_ms,
        observed_at_ms=observed_at_ms,
        age_seconds=elapsed_seconds,
        progress_ratio=progress,
        open=forming.open,
        high=forming.high,
        low=forming.low,
        close=forming.close,
        volume=forming.volume,
        turnover=forming.turnover,
        body_pct=body_pct,
        range_pct=range_pct,
        body_to_range=body_to_range,
        upper_wick_pct=upper_wick_pct,
        lower_wick_pct=lower_wick_pct,
        close_position=close_position,
        volume_pace_ratio=volume_pace_ratio,
        range_expansion_ratio=range_expansion_ratio,
        velocity_bps_per_second=velocity_bps_per_second,
        direction=direction,
        price_source=(
            "hybrid_tape"
            if tape_updates > 0
            else "kline"
        ),
        volume_source="kline_snapshot",
        tape_updates=max(0, int(tape_updates)),
        current_minute_trade_count=len(minute_trades),
        last_trade_ts_ms=resolved_last_trade_ts,
        last_trade_age_seconds=last_trade_age_seconds,
        kline_snapshot_age_seconds=kline_snapshot_age_seconds,
        micro_move_5s_bps=micro_move_5s_bps,
        micro_range_5s_bps=micro_range_5s_bps,
        micro_move_15s_bps=micro_move_15s_bps,
        micro_range_15s_bps=micro_range_15s_bps,
    )
