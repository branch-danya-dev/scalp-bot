from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class StrategyExpectancy:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl_usd: float = 0.0
    sum_r: float = 0.0
    win_pnl_usd: float = 0.0
    loss_pnl_usd: float = 0.0

    def record(
        self,
        net_pnl_usd: float,
        initial_risk_usd: float,
    ) -> None:
        self.trades += 1
        self.net_pnl_usd += net_pnl_usd
        if initial_risk_usd > 0:
            self.sum_r += net_pnl_usd / initial_risk_usd
        if net_pnl_usd > 0:
            self.wins += 1
            self.win_pnl_usd += net_pnl_usd
        elif net_pnl_usd < 0:
            self.losses += 1
            self.loss_pnl_usd += net_pnl_usd

    def public(
        self,
        *,
        min_samples: int,
        minimum_expectancy_r: float,
    ) -> dict:
        win_rate = (
            self.wins / self.trades
            if self.trades > 0
            else 0.0
        )
        avg_win = (
            self.win_pnl_usd / self.wins
            if self.wins > 0
            else 0.0
        )
        avg_loss = (
            self.loss_pnl_usd / self.losses
            if self.losses > 0
            else 0.0
        )
        expectancy_r = (
            self.sum_r / self.trades
            if self.trades > 0
            else 0.0
        )
        ready = self.trades >= max(1, min_samples)
        if not ready:
            status = "insufficient_samples"
        elif expectancy_r >= minimum_expectancy_r:
            status = "positive"
        else:
            status = "negative"
        return {
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "winRate": win_rate,
            "avgWinUsd": avg_win,
            "avgLossUsd": avg_loss,
            "netPnlUsd": self.net_pnl_usd,
            "expectancyR": expectancy_r,
            "minimumExpectancyR": minimum_expectancy_r,
            "minSamples": min_samples,
            "sampleReady": ready,
            "status": status,
        }


class StrategyExpectancyBook:
    def __init__(self, strategies: list[str]) -> None:
        self._stats = {
            strategy: StrategyExpectancy()
            for strategy in strategies
        }

    def record(
        self,
        strategy: str,
        *,
        net_pnl_usd: float,
        initial_risk_usd: float,
    ) -> None:
        self._stats.setdefault(
            strategy,
            StrategyExpectancy(),
        ).record(
            net_pnl_usd,
            initial_risk_usd,
        )

    def snapshot(
        self,
        strategy: str,
        *,
        min_samples: int,
        minimum_expectancy_r: float,
    ) -> dict:
        return self._stats.setdefault(
            strategy,
            StrategyExpectancy(),
        ).public(
            min_samples=min_samples,
            minimum_expectancy_r=minimum_expectancy_r,
        )
