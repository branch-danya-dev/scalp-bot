"""Validate observable L2 state, not queue position, fills or feed completeness."""
from decimal import Decimal
import heapq

from .adapters import ArchiveError


class BookValidator:
    def __init__(self, max_levels_per_side=25_000):
        if type(max_levels_per_side) is not int or max_levels_per_side <= 0:
            raise ValueError("max levels must be positive")
        self.max_levels = max_levels_per_side
        self.levels = {"bid": {}, "ask": {}}
        self.heaps = {"bid": [], "ask": []}
        self.initialized = False

    def apply(self, event):
        if event["is_snapshot"]:
            self.levels = {"bid": {}, "ask": {}}
            self.heaps = {"bid": [], "ask": []}
            self.initialized = True
        if not self.initialized:
            return "uninitialized"
        for change in event["changes"]:
            side, price, amount = change["side"], Decimal(change["price"]), Decimal(change["amount"])
            levels, heap = self.levels[side], self.heaps[side]
            if amount == 0:
                levels.pop(price, None)
            else:
                if price not in levels:
                    heapq.heappush(heap, -price if side == "bid" else price)
                levels[price] = amount  # Absolute quantity, NOT additive delta.
                if len(levels) > self.max_levels:
                    raise ArchiveError("book level limit exceeded; no silent tail truncation")
            if len(heap) > 2 * len(levels) + 1024:
                self.heaps[side] = [-p if side == "bid" else p for p in levels]
                heapq.heapify(self.heaps[side])
        bid, ask = self.best("bid"), self.best("ask")
        if bid is None or ask is None:
            return "one_sided"
        return "crossed" if bid >= ask else "two_sided_uncrossed"

    def best(self, side):
        heap, levels = self.heaps[side], self.levels[side]
        while heap:
            price = -heap[0] if side == "bid" else heap[0]
            if price in levels:
                return price
            heapq.heappop(heap)
        return None
