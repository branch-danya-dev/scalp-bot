from scalp_bot.domain import TradeTick


def breakout_executions(
    end_ms=30_014_000, *, first=100.12, last=100.165, short=False, duration_ms=4_600,
):
    """Fresh directional executions; time advances beyond the first price break."""
    return [
        TradeTick(
            end_ms - duration_ms + round(i * duration_ms / 23),
            200 - (first + (last - first) * i / 23)
            if short else first + (last - first) * i / 23,
            32,
            "Sell" if short else "Buy",
        )
        for i in range(24)
    ]
