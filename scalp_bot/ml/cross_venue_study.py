"""Causal, strategy-stratified external context study. Never a portfolio sum."""
from collections import defaultdict, Counter
import math

from .prepared_dataset import purged_walk_forward

METRICS = ("target_before_stop", "realized_net_r", "executable_mfe_r", "executable_mae_r",
    "time_to_mfe_ms", "no_follow_through", "stop", "partial", "target")


def summary(rows):
    metrics = {}
    for name in METRICS:
        values = [r[name] for r in rows if r.get(name) is not None and isinstance(r[name], (int, float)) and math.isfinite(r[name])]
        metrics[name] = dict(samples=len(values), mean=sum(values)/len(values) if values else None)
    return dict(samples=len(rows), metrics=metrics,
        concentration={key:dict(Counter(r.get(key, "unknown") for r in rows)) for key in ("symbol", "capture_id")},
        regimes=dict(Counter(r.get("segment", {}).get("localRegime", "unknown") for r in rows)),
        portfolioPnl=None)


def study(rows, windows, *, embargo_ms=60_000):
    if embargo_ms < 60_000:
        raise ValueError("pre-registered embargo cannot be reduced")
    seen = set()
    for row in rows:
        identity = (row["capture_id"], row["identity"])
        if identity in seen:
            raise ValueError("duplicate causal prepared identity")
        seen.add(identity)
    complete = [r for r in rows if r.get("trainingReady") and not r.get("censor_reason")]
    results = []
    for fold_index, fold in enumerate(purged_walk_forward(complete, windows, embargo_ms=embargo_ms)):
        groups = defaultdict(list)
        for index in fold["validation"]:
            row = complete[index]
            if row["strategy"] in {"level_breakout", "weak_level_rejection"}:
                groups[(row["strategy"], row.get("crossVenueAlignment", "unavailable"))].append(row)
        for strategy in ("level_breakout", "weak_level_rejection"):
            for group in ("aligned", "neutral", "opposed", "unavailable"):
                items = groups[(strategy, group)]
                results.append(dict(fold=fold_index, strategy=strategy, alignment=group, **summary(items),
                    bySymbol={s:summary([r for r in items if r["symbol"] == s]) for s in sorted({r["symbol"] for r in items})},
                    byRegime={g:summary([r for r in items if r.get("segment", {}).get("localRegime", "unknown") == g])
                        for g in sorted({r.get("segment", {}).get("localRegime", "unknown") for r in items})}))
    return dict(schema="cross-venue-study-v1", population="identical_causal_prepared_membership",
        samples=len(rows), complete=len(complete), censored=len(rows)-len(complete), results=results,
        promotionAuthorized=False, mode="telemetry_only", status="INCONCLUSIVE" if not complete else "REQUIRES_STABILITY_REVIEW",
        limitations=["overlapping_labels_not_portfolio_returns", "observed_leadership_not_exchange_causality",
            "independent_capture_symbol_regime_stability_required"])
