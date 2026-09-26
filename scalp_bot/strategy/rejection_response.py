"""Predeclared research candidate, independent of routing/risk policy."""
from .flow import price_response_bps_since


def assess_reclaim_response(side, reclaim_ms, anchor_quote, quote, trades, now_ms, minimum_bps=1.5):
    if side not in {"long", "short"}:
        raise ValueError("invalid response side")
    sign = 1 if side == "long" else -1
    tape, count = price_response_bps_since(trades, reclaim_ms+1, now_ms=now_ms)
    response = sign*(quote-anchor_quote)/anchor_quote*10000 if quote and anchor_quote>0 else None
    tape = sign*tape if tape is not None else None
    return dict(policy="quote_tape_v1", reclaimMs=reclaim_ms, observedAtMs=now_ms,
                quoteResponseBps=response, tapeResponseBps=tape, tradeCount=count,
                allowed=bool(0<reclaim_ms<now_ms and response is not None and tape is not None
                             and count>=2 and response>=minimum_bps and tape>=minimum_bps))
