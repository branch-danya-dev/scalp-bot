"""Entry-time segmentation and separate realized/diagnostic outcome populations."""
from collections import defaultdict
import argparse
import json
import math
from pathlib import Path
from .domain import Action

DIMENSIONS = ("strategy", "trendRelation", "localRegime", "htfAlignment", "flowAlignment",
              "targetSource", "stopDistanceBucket", "costShareBucket")


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def bucket(value, limits):
    if not finite(value):
        return "unknown"
    for edge in limits:
        if value < edge:
            return f"lt_{edge:g}"
    return f"ge_{limits[-1]:g}"


def setup_segment(strategy, side, details, *, entry=None, stop=None, context=None):
    assessment = details.get("finalEntryContextAssessment") or details.get("entryContextAssessment") or {}
    direction = assessment.get("directionPlan") or details.get("playbookContext") or {}
    local = context.local_regime if context is not None else None
    regime = local.regime.value if local else direction.get("localRegime", "unknown")
    local_direction = local.direction.value if local else (
        "up" if str(regime).startswith("bullish") else "down" if str(regime).startswith("bearish") else "flat")
    sign = "up" if side == "long" else "down"
    relation = ("unknown" if regime in (None, "unknown") else "non_directional"
                if local_direction == "flat" else "trend_aligned" if local_direction == sign else "countertrend")
    bias = context.htf_bias.bias.value if context is not None and context.htf_bias else direction.get("htfBias")
    htf = ("unknown" if bias is None else "neutral" if bias == "neutral" else
           "aligned" if bias == ("bullish" if side == "long" else "bearish") else "opposed")
    flow = context.flow_alignment_for(Action(side)) if context else None
    flow_class = flow.classification.value if flow else (assessment.get("flowClassification")
        or (details.get("flowAlignment") or {}).get("classification") or "unknown")
    economics = details.get("economics") or {}
    distance = abs(entry-stop)/entry*10000 if finite(entry) and entry > 0 and finite(stop) else None
    source = details.get("targetSource") or "unknown"
    if isinstance(source, dict):
        source = source.get("source") or source.get("kind") or json.dumps(source, sort_keys=True)
    return dict(zip(DIMENSIONS, (strategy, relation, regime or "unknown", htf, flow_class,
        str(source), bucket(distance, (5, 10, 20, 40, 80)),
        bucket(economics.get("winnerCostShare"), (.15, .25, .35, .5)))))


def segment_key(segment):
    return tuple(str(segment.get(k) or "unknown") for k in DIMENSIONS)


def summarize(rows, *, population):
    """One row per closed position or per causal label; never combine populations."""
    groups = defaultdict(list)
    seen = set()
    duplicates = 0
    for row in rows:
        identity = row["identity"]
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        groups[segment_key(row["segment"])].append(row)
    output = []
    for key, items in sorted(groups.items()):
        pnl = [r["netPnl"] for r in items if finite(r.get("netPnl"))]
        rs = [r["netPnl"]/r["initialRiskUsd"] for r in items
              if finite(r.get("netPnl")) and finite(r.get("initialRiskUsd")) and r["initialRiskUsd"] > 0]
        flags = {name: sum(bool(r.get(name)) for r in items if r.get(name) is not None)
                 for name in ("target", "partial", "stop", "noFollowThrough")}
        coverage = {name: sum(r.get(name) is not None for r in items) for name in flags}
        excursions = {}
        for name in ("mfeR", "maeR"):
            values = [r[name] for r in items if finite(r.get(name))]
            excursions[name] = sum(values)/len(values) if values else None
        output.append(dict(segment=dict(zip(DIMENSIONS, key)), count=len(items), pnlSamples=len(pnl),
            winRate=sum(v > 0 for v in pnl)/len(pnl) if pnl else None,
            expectancyR=sum(rs)/len(rs) if rs else None, rSamples=len(rs),
            netPnl=sum(pnl) if pnl else None,
            **excursions,
            frequencies={k: flags[k]/coverage[k] if coverage[k] else None for k in flags},
            frequencySamples=coverage))
    return dict(population=population, count=len(seen), duplicatesExcluded=duplicates, segments=output,
        portfolioNetPnl=sum(r["netPnl"] or 0 for r in output) if population == "closed_positions" else None)


def closed_observation(payload, symbol=None):
    details = payload.get("strategyDetails") or {}
    segment = details.get("setupSegment") or setup_segment(payload.get("strategy", "unknown"),
        payload.get("side", "long"), details, entry=payload.get("entry"), stop=payload.get("initialStop", payload.get("stop")))
    identity = json.dumps([symbol or payload.get("symbol"), payload.get("strategy"),
        payload.get("setupId"), payload.get("openedAt"), payload.get("closedAt")])
    reason = str(payload.get("reason") or "")
    return dict(identity=identity, segment=segment, netPnl=payload.get("netPnl"),
        initialRiskUsd=payload.get("initialRiskUsd"), mfeR=payload.get("mfeR"), maeR=payload.get("maeR"),
        target=reason == "target", stop=reason == "stop", partial=bool(payload.get("partialTaken")),
        noFollowThrough="no_follow" in reason)


def analyze_events(path):
    rows = []
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event.get("event") == "trade_closed":
                rows.append(closed_observation(event["payload"], event.get("symbol")))
    return summarize(rows, population="closed_positions")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("events", nargs="+")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    reports = {}
    for name in args.events:
        reports[name] = analyze_events(name)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(dict(schemaVersion=1, runs=reports,
        limitation="Previously studied captures; no independent holdout. Missing dimensions stay unknown."), indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__":
    main()
