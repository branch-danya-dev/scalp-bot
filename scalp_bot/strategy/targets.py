"""Causal targets independent of desired reward/risk."""
from ..domain import Action
from .common import typical_range_abs


def structural_target(entry, action, ladder, *, movement):
    direction=1 if action==Action.LONG else -1
    nearest=min((r for r in ladder if direction*(r.price-entry)>0),
                key=lambda r: abs(r.price-entry), default=None)
    if nearest is not None:
        return nearest.price,nearest,"liquidity_ladder"
    # Two recent typical candle ranges describe observed movement, not stop risk.
    if movement is None or movement<=0:
        return entry,None,"insufficient_movement_data"
    return entry+direction*movement,None,"observed_range_projection"


def movement_budget(candles):
    return 2*typical_range_abs(candles)
