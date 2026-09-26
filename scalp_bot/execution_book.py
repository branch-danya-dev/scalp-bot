"""One quote contract for risk, fills and diagnostic markouts.

The current L50 owns its price interval. Never append overlapping stale L1000
liquidity or fill a fast-triggered stop at a better, obsolete deep quote.
"""
from dataclasses import dataclass, field

from .domain import OrderBook


@dataclass
class ExecutionBook(OrderBook):
    execution: dict = field(default_factory=dict)


def coherent_execution_book(fast: OrderBook, deep: OrderBook | None = None) -> ExecutionBook:
    def side(head, tail, bid):
        head = [(p,q) for p,q in head if p>0 and q>0]
        if not head:
            return []  # No guessed executable side when the fast stream is absent.
        edge = head[-1][0]
        rows = head + [(p,q) for p,q in tail if p>0 and q>0 and (p<edge if bid else p>edge)]
        return sorted(dict(rows).items(), reverse=bid)
    deep = deep or OrderBook()
    conflict = bool((fast.best_bid and deep.best_bid and deep.best_bid>fast.best_bid)
                    or (fast.best_ask and deep.best_ask and deep.best_ask<fast.best_ask))
    bids=side(fast.bids,deep.bids,True); asks=side(fast.asks,deep.asks,False)
    tail=bool(len(bids)>len(fast.bids) or len(asks)>len(fast.asks))
    return ExecutionBook(bids,asks,dict(source="fast_l50_with_deep_tail" if tail else "fast_l50",
        quality="observed_depth", fastHeadAuthoritative=True, deepQuoteConflict=conflict,
        syntheticLiquidity=False))


def execution_quality(book, visible_quantity, requested_quantity):
    result=dict(getattr(book,"execution",{}) or {"source":"provided_book","quality":"observed_depth"})
    result.update(visibleQuantity=visible_quantity, requestedQuantity=requested_quantity)
    if visible_quantity+max(1e-12,requested_quantity*1e-12)<requested_quantity:
        result.update(quality="estimated_missing_depth", syntheticLiquidity=True,
                      reason="protective paper close estimates unseen tail with configured adverse penalty")
    return result
