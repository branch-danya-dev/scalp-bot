"""Causal failed-break identities that survive forming/closed candle rollover."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SweepEpisode:
    token: str | None = None
    outside: bool = False
    latest_evidence_bar: int = -1
    event_bar: int = -1
    sequence: int = 0


class FailedBreakEpisodes:
    def __init__(self) -> None:
        self._levels: dict[str, SweepEpisode] = {}

    def observe(self, key: str, level: Any, side: str, price: float,
                observed_ms: int, bars: list[Any]) -> str | None:
        """A new excursion, not a renamed current candle, creates an episode.

        Bootstrap may use an already observed wick. Thereafter the same bar
        becoming closed cannot recreate it. A fresh quote crossing or a new
        candle starting on the inside and piercing the boundary is new evidence.
        OHLC alone cannot distinguish several intrabar sweeps; quote crossings
        can. No future candle, new timeout, or outcome is used for this identity.
        """
        long = side == "long"
        outside = price < level.low if long else price > level.high
        reclaimed = price > level.high if long else price < level.low
        visible = [bar for bar in bars if bar is not None and bar.start_ms <= observed_ms]
        evidence = [bar for bar in visible
                    if (bar.low < level.low if long else bar.high > level.high)]
        inside_open = lambda bar: bar.open >= level.low if long else bar.open <= level.high
        current_bar = observed_ms // 60_000 * 60_000
        state = self._levels.get(key)
        if state is None:
            state = SweepEpisode(outside=outside)
            self._levels[key] = state
            if evidence:
                # A new bar opening back inside and sweeping is a new excursion;
                # a bar opening outside may only continue the preceding one.
                new_excursions = [bar for bar in evidence if inside_open(bar)]
                source = (max(new_excursions, key=lambda bar: bar.start_ms)
                          if new_excursions else min(evidence, key=lambda bar: bar.start_ms))
                state.event_bar = source.start_ms
                state.token = f"wick:{source.start_ms}"
            elif outside:
                state.event_bar = current_bar
                state.token = f"quote:{observed_ms}:0"
        else:
            new_bars = [bar for bar in evidence if bar.start_ms > state.latest_evidence_bar
                        and inside_open(bar)]
            if outside and not state.outside:
                state.sequence += 1
                state.event_bar = current_bar
                state.token = f"quote:{observed_ms}:{state.sequence}"
            elif new_bars:
                source_bar = max(bar.start_ms for bar in new_bars)
                # A delayed wick for the SAME quote-observed excursion is not
                # another event, even when the quote has already reclaimed.
                if not state.outside and source_bar > state.event_bar:
                    state.event_bar = source_bar
                    state.token = f"wick:{source_bar}"
        if evidence:
            state.latest_evidence_bar = max(state.latest_evidence_bar,
                                            max(bar.start_ms for bar in evidence))
        state.outside = outside
        # Do not resurrect an old wick indefinitely once it has left the
        # observable failed-break window (previous closed + forming candle).
        supported_bars = {bar.start_ms for bar in visible} | {current_bar}
        return state.token if reclaimed and state.event_bar in supported_bars else None
