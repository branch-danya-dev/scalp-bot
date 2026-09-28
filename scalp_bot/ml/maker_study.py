"""Development-only maker segmentation; fragments are never portfolio PnL."""
from collections import Counter, defaultdict
import math
from ..research_journal import read_research
from ..setup_segments import bucket


def segments(row):
    context=row.get("context") or {}
    quantity=row.get("quantity",0)
    return dict(spread=bucket(row.get("expected_spread_bps"),(.5,1,2,5)),
        queueAhead=bucket(row["queue_ahead"]/quantity if quantity else None,(1,10,100,1000)),
        timeToFill=bucket(row.get("timeToFillMs"),(50,100,500,1000,2000)),
        ofi=bucket(context.get("ofiUsd"),(-10000,0,10000)),
        imbalance=bucket(context.get("imbalance"),(-.5,0,.5)),
        tradeImpulse=bucket(context.get("tradeImpulse"),(-.5,0,.5)),
        crossVenue=context.get("crossVenueAlignment","unavailable"),
        volatility=bucket(context.get("volatilityPct"),(.05,.1,.2,.5)),
        regime=context.get("regime") or "unavailable",side=row["side"],symbol=row["symbol"],
        depthQuality=str(context.get("depthFresh","unavailable")))


def summarize(rows):
    filled=sum(r.get("filledQuantity",0)>0 for r in rows)
    partial=sum(0<r.get("filledQuantity",0)<r["quantity"]-1e-12 for r in rows)
    windows={}
    for window in (100,500,1000):
        observations=[lot["markouts"].get(str(window),{"censor_reason":"missing_window"})
            for r in rows for lot in r["lots"]]
        complete=[v for v in observations if not v.get("censor_reason")]
        metrics={}
        for field in ("markoutBps","grossLiquidationPnl","grossSpreadCapture","fees","netAfterCosts","adverseSelection"):
            values=[v[field] for v in complete if type(v.get(field)) in (int,float,bool) and math.isfinite(v[field])]
            metrics[field]=dict(count=len(values),mean=sum(values)/len(values) if values else None)
        windows[str(window)]=dict(fragments=len(observations),complete=len(complete),
            censored=len(observations)-len(complete),metrics=metrics,
            censorReasons=dict(Counter(v.get("censor_reason") for v in observations if v.get("censor_reason"))))
    return dict(candidates=len(rows),filledCandidates=filled,fillRate=filled/len(rows) if rows else None,
        partialRate=partial/len(rows) if rows else None,captures=dict(Counter(r["capture_id"] for r in rows)),
        windows=windows,portfolioPnl=None)


def study(paths):
    rows=[];candidates=set();outcomes=set()
    for path in paths:
        for record in read_research(path):
            key=(record["captureId"],record["body"].get("identity"))
            if record["kind"]=="maker_candidate":
                if key in candidates:raise ValueError("duplicate maker candidate")
                candidates.add(key)
            elif record["kind"]=="maker_outcome":
                if key not in candidates or key in outcomes:raise ValueError("unbound/duplicate maker outcome")
                outcomes.add(key);rows.append(dict(record["body"],capture_id=record["captureId"]))
    groups=defaultdict(list)
    for row in rows:
        for dimension,value in segments(row).items():groups[(dimension,value)].append(row)
    reports=[dict(dimension=d,value=v,**summarize(r)) for (d,v),r in sorted(groups.items())]
    supported=[r for r in reports if r['dimension']!='timeToFill' and r['value'] not in {'unknown','unavailable'}
        and r['filledCandidates']>=100 and len(r['captures'])>=3
        and all((r['windows'][w]['metrics']['netAfterCosts']['mean'] or 0)>0 for w in ('500','1000'))]
    return dict(schema="maker-segment-development-v1",summary=summarize(rows),groups=reports,
        missingFinalOutcomes=len(candidates-outcomes),hypothesis=None,
        hypothesisStatus="REQUIRES_ONE_PROSPECTIVE_RULE" if supported else "NO_SUPPORTED_POSITIVE_SEGMENT",
        independentValidation="NOT_TESTED",executionGate="NOT_MET",promotionAuthorized=False,
        scope="development diagnostics; overlapping fragments are not independent portfolio returns",
        limitations=["historical_unrecorded_covariates_remain_unavailable", "time_to_fill_is_outcome_not_admission_feature",
            "minimum_support_not_proof_of_edge", "one_marginal_group_not_a_joint_executable_rule"])
