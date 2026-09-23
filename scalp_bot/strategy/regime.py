from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from statistics import median
from typing import Any

from ..domain import Candle, Trend
from .common import classify_context_trend, classify_trend, clamp


class HTFBias(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class LocalRegime(StrEnum):
    BULLISH_IMPULSE = "bullish_impulse"
    BEARISH_IMPULSE = "bearish_impulse"
    BULLISH_TREND = "bullish_trend"
    BEARISH_TREND = "bearish_trend"
    PULLBACK = "pullback"
    TRANSITION = "transition"
    RANGE = "range"
    UNCLEAR = "unclear"


@dataclass(slots=True)
class HTFBiasSnapshot:
    bias: HTFBias
    strength: float
    trend_15m: Trend
    trend_1h: Trend
    alignment: str
    legacy_trend: Trend

    def public(self) -> dict[str, Any]:
        return {
            "bias": self.bias.value,
            "strength": self.strength,
            "trend15m": self.trend_15m.value,
            "trend1h": self.trend_1h.value,
            "alignment": self.alignment,
            "legacyTrend": self.legacy_trend.value,
        }


@dataclass(slots=True)
class LocalRegimeSnapshot:
    regime: LocalRegime
    direction: Trend
    parent_direction: Trend
    strength: float
    structure_1m: Trend
    structure_5m: Trend
    recent_move_pct: float
    recent_range_pct: float
    baseline_range_pct: float
    range_expansion_ratio: float
    directional_efficiency: float
    impulse_threshold_pct: float
    reasons: list[str]

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["regime"] = self.regime.value
        data["direction"] = self.direction.value
        data["parent_direction"] = self.parent_direction.value
        data["structure_1m"] = self.structure_1m.value
        data["structure_5m"] = self.structure_5m.value
        return data


def classify_htf_bias(
    context_15m: list[Candle],
    context_1h: list[Candle],
) -> HTFBiasSnapshot:
    trend_15m = classify_trend(context_15m)
    trend_1h = classify_trend(context_1h)
    legacy = classify_context_trend(context_15m, context_1h)

    directional = {Trend.UP, Trend.DOWN}
    if trend_15m in directional and trend_1h == trend_15m:
        bias = (
            HTFBias.BULLISH
            if trend_15m == Trend.UP
            else HTFBias.BEARISH
        )
        return HTFBiasSnapshot(
            bias=bias,
            strength=1.0,
            trend_15m=trend_15m,
            trend_1h=trend_1h,
            alignment="aligned",
            legacy_trend=legacy,
        )

    if trend_15m in directional and trend_1h == Trend.FLAT:
        bias = (
            HTFBias.BULLISH
            if trend_15m == Trend.UP
            else HTFBias.BEARISH
        )
        return HTFBiasSnapshot(
            bias=bias,
            strength=0.70,
            trend_15m=trend_15m,
            trend_1h=trend_1h,
            alignment="15m_only",
            legacy_trend=legacy,
        )

    if trend_1h in directional and trend_15m == Trend.FLAT:
        bias = (
            HTFBias.BULLISH
            if trend_1h == Trend.UP
            else HTFBias.BEARISH
        )
        return HTFBiasSnapshot(
            bias=bias,
            strength=0.60,
            trend_15m=trend_15m,
            trend_1h=trend_1h,
            alignment="1h_only",
            legacy_trend=legacy,
        )

    if (
        trend_15m in directional
        and trend_1h in directional
        and trend_15m != trend_1h
    ):
        return HTFBiasSnapshot(
            bias=HTFBias.NEUTRAL,
            strength=0.15,
            trend_15m=trend_15m,
            trend_1h=trend_1h,
            alignment="conflict",
            legacy_trend=legacy,
        )

    return HTFBiasSnapshot(
        bias=HTFBias.NEUTRAL,
        strength=0.0,
        trend_15m=trend_15m,
        trend_1h=trend_1h,
        alignment="flat",
        legacy_trend=legacy,
    )


def _range_pct(candle: Candle) -> float:
    return (
        (candle.high - candle.low) / candle.close
        if candle.close > 0 and candle.high >= candle.low
        else 0.0
    )


def _median_range(candles: list[Candle]) -> float:
    values = [
        _range_pct(candle)
        for candle in candles
        if _range_pct(candle) > 0
    ]
    return median(values) if values else 0.001


def _recent_metrics(
    candles_1m: list[Candle],
    *,
    recent_bars: int = 6,
) -> tuple[float, float, float, float]:
    recent = candles_1m[-min(recent_bars, len(candles_1m)):]
    if not recent:
        return 0.0, 0.0, 0.0, 0.0

    start = recent[0].open
    end = recent[-1].close
    recent_move_pct = (
        (end - start) / start
        if start > 0
        else 0.0
    )
    recent_range_pct = _median_range(recent)

    baseline_source = candles_1m[
        max(0, len(candles_1m) - len(recent) - 40):
        max(0, len(candles_1m) - len(recent))
    ]
    baseline_range_pct = _median_range(
        baseline_source or candles_1m[:-1] or candles_1m
    )
    range_expansion_ratio = (
        recent_range_pct / baseline_range_pct
        if baseline_range_pct > 0
        else 1.0
    )

    path = [recent[0].open, *[row.close for row in recent]]
    path_distance = sum(
        abs(right - left)
        for left, right in zip(path, path[1:])
    )
    net_distance = abs(end - start)
    directional_efficiency = (
        net_distance / path_distance
        if path_distance > 0
        else 0.0
    )
    return (
        recent_move_pct,
        recent_range_pct,
        baseline_range_pct,
        range_expansion_ratio,
        directional_efficiency,
    )


def classify_local_regime(
    candles_1m: list[Candle],
    candles_5m: list[Candle],
) -> LocalRegimeSnapshot:
    confirmed_1m = [row for row in candles_1m if row.confirmed]
    confirmed_5m = [row for row in candles_5m if row.confirmed]

    structure_1m = classify_trend(confirmed_1m)
    structure_5m = classify_trend(confirmed_5m)

    if len(confirmed_1m) < 6:
        return LocalRegimeSnapshot(
            regime=LocalRegime.UNCLEAR,
            direction=Trend.FLAT,
            parent_direction=structure_5m,
            strength=0.0,
            structure_1m=structure_1m,
            structure_5m=structure_5m,
            recent_move_pct=0.0,
            recent_range_pct=0.0,
            baseline_range_pct=0.0,
            range_expansion_ratio=0.0,
            directional_efficiency=0.0,
            impulse_threshold_pct=0.0,
            reasons=["insufficient confirmed 1m history"],
        )

    (
        recent_move_pct,
        recent_range_pct,
        baseline_range_pct,
        range_expansion_ratio,
        directional_efficiency,
    ) = _recent_metrics(confirmed_1m)

    impulse_threshold_pct = max(
        0.0015,
        baseline_range_pct * 2.4,
    )
    abs_move = abs(recent_move_pct)
    recent_direction = (
        Trend.UP
        if recent_move_pct > max(baseline_range_pct * 0.30, 0.0002)
        else (
            Trend.DOWN
            if recent_move_pct < -max(baseline_range_pct * 0.30, 0.0002)
            else Trend.FLAT
        )
    )

    impulse = (
        abs_move >= impulse_threshold_pct
        and (
            directional_efficiency >= 0.55
            or range_expansion_ratio >= 1.25
        )
    )
    if impulse:
        bullish = recent_move_pct > 0
        regime = (
            LocalRegime.BULLISH_IMPULSE
            if bullish
            else LocalRegime.BEARISH_IMPULSE
        )
        direction = Trend.UP if bullish else Trend.DOWN
        strength = clamp(
            0.55
            + min(abs_move / max(impulse_threshold_pct, 1e-9), 2.0) * 0.20
            + directional_efficiency * 0.20
            + min(range_expansion_ratio / 2.0, 1.0) * 0.05
        )
        return LocalRegimeSnapshot(
            regime=regime,
            direction=direction,
            parent_direction=structure_5m,
            strength=strength,
            structure_1m=structure_1m,
            structure_5m=structure_5m,
            recent_move_pct=recent_move_pct,
            recent_range_pct=recent_range_pct,
            baseline_range_pct=baseline_range_pct,
            range_expansion_ratio=range_expansion_ratio,
            directional_efficiency=directional_efficiency,
            impulse_threshold_pct=impulse_threshold_pct,
            reasons=[
                "recent 1m move exceeds impulse threshold",
                "directional efficiency/range expansion confirms local impulse",
            ],
        )

    if (
        structure_1m == Trend.UP
        and structure_5m == Trend.UP
    ):
        return LocalRegimeSnapshot(
            regime=LocalRegime.BULLISH_TREND,
            direction=Trend.UP,
            parent_direction=Trend.UP,
            strength=clamp(
                0.65
                + directional_efficiency * 0.20
                + min(max(recent_move_pct, 0.0) / max(impulse_threshold_pct, 1e-9), 1.0) * 0.15
            ),
            structure_1m=structure_1m,
            structure_5m=structure_5m,
            recent_move_pct=recent_move_pct,
            recent_range_pct=recent_range_pct,
            baseline_range_pct=baseline_range_pct,
            range_expansion_ratio=range_expansion_ratio,
            directional_efficiency=directional_efficiency,
            impulse_threshold_pct=impulse_threshold_pct,
            reasons=["1m and 5m structures are aligned upward"],
        )

    if (
        structure_1m == Trend.DOWN
        and structure_5m == Trend.DOWN
    ):
        return LocalRegimeSnapshot(
            regime=LocalRegime.BEARISH_TREND,
            direction=Trend.DOWN,
            parent_direction=Trend.DOWN,
            strength=clamp(
                0.65
                + directional_efficiency * 0.20
                + min(max(-recent_move_pct, 0.0) / max(impulse_threshold_pct, 1e-9), 1.0) * 0.15
            ),
            structure_1m=structure_1m,
            structure_5m=structure_5m,
            recent_move_pct=recent_move_pct,
            recent_range_pct=recent_range_pct,
            baseline_range_pct=baseline_range_pct,
            range_expansion_ratio=range_expansion_ratio,
            directional_efficiency=directional_efficiency,
            impulse_threshold_pct=impulse_threshold_pct,
            reasons=["1m and 5m structures are aligned downward"],
        )

    parent_direction = structure_5m
    opposite_parent = (
        (structure_5m == Trend.UP and recent_direction == Trend.DOWN)
        or (structure_5m == Trend.DOWN and recent_direction == Trend.UP)
    )
    moderate_move = (
        abs_move >= max(baseline_range_pct * 0.60, 0.0004)
        and abs_move < impulse_threshold_pct
    )
    if opposite_parent and moderate_move:
        return LocalRegimeSnapshot(
            regime=LocalRegime.PULLBACK,
            direction=recent_direction,
            parent_direction=parent_direction,
            strength=clamp(
                0.45
                + directional_efficiency * 0.30
                + min(abs_move / max(impulse_threshold_pct, 1e-9), 1.0) * 0.25
            ),
            structure_1m=structure_1m,
            structure_5m=structure_5m,
            recent_move_pct=recent_move_pct,
            recent_range_pct=recent_range_pct,
            baseline_range_pct=baseline_range_pct,
            range_expansion_ratio=range_expansion_ratio,
            directional_efficiency=directional_efficiency,
            impulse_threshold_pct=impulse_threshold_pct,
            reasons=["recent 1m move is a moderate counter-move against 5m structure"],
        )

    structure_conflict = (
        structure_1m in {Trend.UP, Trend.DOWN}
        and structure_5m in {Trend.UP, Trend.DOWN}
        and structure_1m != structure_5m
    )
    directional_transition = (
        structure_5m in {Trend.UP, Trend.DOWN}
        and recent_direction in {Trend.UP, Trend.DOWN}
        and recent_direction != structure_5m
        and abs_move >= max(baseline_range_pct * 0.45, 0.0003)
    )
    if structure_conflict or directional_transition:
        return LocalRegimeSnapshot(
            regime=LocalRegime.TRANSITION,
            direction=recent_direction,
            parent_direction=structure_5m,
            strength=clamp(
                0.45
                + directional_efficiency * 0.25
                + min(abs_move / max(impulse_threshold_pct, 1e-9), 1.0) * 0.30
            ),
            structure_1m=structure_1m,
            structure_5m=structure_5m,
            recent_move_pct=recent_move_pct,
            recent_range_pct=recent_range_pct,
            baseline_range_pct=baseline_range_pct,
            range_expansion_ratio=range_expansion_ratio,
            directional_efficiency=directional_efficiency,
            impulse_threshold_pct=impulse_threshold_pct,
            reasons=["short-horizon direction conflicts with the 5m structure"],
        )

    quiet_move_limit = max(
        baseline_range_pct * 0.80,
        impulse_threshold_pct * 0.35,
    )
    if (
        structure_1m == Trend.FLAT
        and structure_5m == Trend.FLAT
        and abs_move <= quiet_move_limit
        and range_expansion_ratio <= 1.20
    ):
        return LocalRegimeSnapshot(
            regime=LocalRegime.RANGE,
            direction=Trend.FLAT,
            parent_direction=Trend.FLAT,
            strength=clamp(0.75 - min(range_expansion_ratio, 1.2) * 0.25),
            structure_1m=structure_1m,
            structure_5m=structure_5m,
            recent_move_pct=recent_move_pct,
            recent_range_pct=recent_range_pct,
            baseline_range_pct=baseline_range_pct,
            range_expansion_ratio=range_expansion_ratio,
            directional_efficiency=directional_efficiency,
            impulse_threshold_pct=impulse_threshold_pct,
            reasons=["1m/5m structures are flat and recent movement is contained"],
        )

    if (
        structure_5m == Trend.UP
        and recent_direction in {Trend.UP, Trend.FLAT}
    ):
        return LocalRegimeSnapshot(
            regime=LocalRegime.BULLISH_TREND,
            direction=Trend.UP,
            parent_direction=Trend.UP,
            strength=0.55,
            structure_1m=structure_1m,
            structure_5m=structure_5m,
            recent_move_pct=recent_move_pct,
            recent_range_pct=recent_range_pct,
            baseline_range_pct=baseline_range_pct,
            range_expansion_ratio=range_expansion_ratio,
            directional_efficiency=directional_efficiency,
            impulse_threshold_pct=impulse_threshold_pct,
            reasons=["5m structure remains upward without a material counter-move"],
        )

    if (
        structure_5m == Trend.DOWN
        and recent_direction in {Trend.DOWN, Trend.FLAT}
    ):
        return LocalRegimeSnapshot(
            regime=LocalRegime.BEARISH_TREND,
            direction=Trend.DOWN,
            parent_direction=Trend.DOWN,
            strength=0.55,
            structure_1m=structure_1m,
            structure_5m=structure_5m,
            recent_move_pct=recent_move_pct,
            recent_range_pct=recent_range_pct,
            baseline_range_pct=baseline_range_pct,
            range_expansion_ratio=range_expansion_ratio,
            directional_efficiency=directional_efficiency,
            impulse_threshold_pct=impulse_threshold_pct,
            reasons=["5m structure remains downward without a material counter-move"],
        )

    return LocalRegimeSnapshot(
        regime=LocalRegime.UNCLEAR,
        direction=recent_direction,
        parent_direction=structure_5m,
        strength=0.25,
        structure_1m=structure_1m,
        structure_5m=structure_5m,
        recent_move_pct=recent_move_pct,
        recent_range_pct=recent_range_pct,
        baseline_range_pct=baseline_range_pct,
        range_expansion_ratio=range_expansion_ratio,
        directional_efficiency=directional_efficiency,
        impulse_threshold_pct=impulse_threshold_pct,
        reasons=["local structure and recent movement do not form a stable regime"],
    )
