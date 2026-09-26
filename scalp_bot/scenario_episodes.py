"""Causal per-object sweep identity; candle rollover is not a new excursion.

Bootstrap can infer one episode from an already available bar witness. Afterwards
quotes and newly observed bar breaches update that episode. Both forming and closed
representations share the SAME bar start. No future extrema are inspected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from .scenario_identity import level_ref


def breach_witness(level, side: str, candles, forming, now_ms: int) -> int | None:
    rows = [c for c in candles if c.confirmed and c.start_ms + 60_000 <= now_ms][-1:]
    if forming is not None and forming.start_ms <= now_ms:
        rows.append(forming)
    witnesses = [c.start_ms for c in rows if
                 (c.low < level.low if side == "long" else c.high > level.high)]
    return max(witnesses) if witnesses else None


@dataclass(slots=True)
class _Excursion:
    episode: str
    pending: str | None = None
    completed: bool = False
    outside: bool = False
    last_ms: int = -1
    serial: int = 0
    seen_witnesses: set[int] = field(default_factory=set)


class EpisodeTracker:
    def __init__(self):
        self._states: dict[tuple[str, str], _Excursion] = {}

    def observe(self, symbol, context, candles, structure) -> dict[str, tuple[str, bool]]:
        result = {}
        if context is None or not context.execution.ready or structure is None:
            return result
        price, now = context.last_price, context.observed_at_ms
        if not price:
            return result
        for level in structure.levels:
            ref = level_ref(level)
            if ref is None:
                continue
            # The directional kind function is deliberately imported locally to
            # keep the identity module independent of strategy package imports.
            from .strategy.structure import directional_level_kind
            kind = directional_level_kind(level.kind)
            if kind is None:
                continue
            side = "long" if kind == "support" else "short"
            outside = price < level.low if side == "long" else price > level.high
            reclaimed = price > level.high if side == "long" else price < level.low
            witness = breach_witness(level, side, candles, context.forming_candle, now)
            state = self._states.setdefault((symbol, ref.token), _Excursion(ref.token + ":approach"))
            # Ignore out-of-order observations; duplicate quotes do not mint episodes.
            if now >= state.last_ms:
                new_witness = witness is not None and witness not in state.seen_witnesses
                if witness is not None:
                    state.seen_witnesses.add(witness)
                    state.seen_witnesses = {x for x in state.seen_witnesses if x >= now - 180_000}
                if outside and not state.outside:
                    state.serial += 1
                    state.pending = f"quote:{now}:{state.serial}"
                    state.episode = ref.token + ":cross:" + state.pending
                    state.completed = False
                elif new_witness and state.pending is None:
                    state.pending = f"bar:{witness}"
                    state.completed = False
                if reclaimed and state.pending is not None:
                    state.episode = ref.token + ":failed:" + state.pending
                    state.pending = None
                    state.completed = True
                state.outside = outside
                state.last_ms = now
            result[ref.token] = (state.episode, state.completed and reclaimed)
        return result
