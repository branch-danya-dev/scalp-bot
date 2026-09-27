"""Separate actual cash, fee-normalized diagnostics and incomplete outcomes."""
import csv
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time
from .contracts import dec, digest, PROTOCOL


def table(path,rows,fields):
    with path.open("x",newline="",encoding="utf-8") as file:
        out=csv.DictWriter(file,fieldnames=fields,extrasaction="ignore");out.writeheader();out.writerows(rows)


def write_report(session,reconciled,reason):
    root=session.output;orders=[];pairs=[];latency=[];ml=[]
    feature_events={e["payload"]["ref"]["source_sequence"]:e["payload"] for e in session.events if e["event"]=="ml_features"}
    prediction_events={(e["payload"]["source_sequence"],e["payload"]["side"]):e["payload"] for e in session.events if e["event"]=="ml_predict"}
    submit_events={(e["payload"]["source_sequence"],e["payload"]["side"]):e["payload"] for e in session.events if e["event"]=="ml_submit"}
    admissions={e["payload"]["pair_id"]:e["received_ns"] for e in session.events if e["event"]=="pair_admitted"}
    all_intents={k:v for arm in session.arms.values() for k,v in arm.intents.items()}
    for pair,intent in all_intents.items():
        plan=intent.plan();row=dict(pair_id=pair,symbol=plan.symbol,side=plan.side.value,author=intent.author,
            source_sequence=intent.source_sequence,observation_ns=intent.observed_ns,model_version=intent.model_version,
            requested_quantity=plan.quantity,plan_json=intent.plan_json)
        signatures={}
        for name,arm in session.arms.items():
            selected=[arm.venue.orders[k] for k in arm.orders_by_pair[pair] if k in arm.venue.orders]
            entries=[o for o in selected if not o.command.reduce_only]
            total=sum((o.filled for o in entries),dec(0));value=sum((o.value for o in entries),dec(0))
            fees=sum((o.fees for o in selected),dec(0))
            cash=sum((o.value*(1 if o.command.side=="Sell" else -1) for o in selected),dec(0))
            normalized=sum((dec(b["execQty"])*dec(b["execPrice"])*dec(session.config.maker_fee_rate if b["isMaker"] else session.config.taker_fee_rate)
                for b in arm.bookings if b["pair_id"]==pair),dec(0))
            funding=sum((dec(e["payload"].get("amount",e["payload"].get("row",{}).get("fundingPnlUsd",0)))
                for e in session.events if e["event"]=="funding" and e["payload"].get("arm")==name and e["payload"].get("pair_id")==pair),dec(0))
            funding_known=name=="paper" or all(
                any(p==pair and s==symbol and abs(stamp-due)<=2000 for p,s,stamp in session.observed_funding)
                for expected_pair,symbol,due in session.expected_funding if expected_pair==pair)
            known=pair in session.portfolio.completed and session.funding_complete() and all(o.terminal and o.confirmed and o.fees_known for o in selected)
            row.update({name+"_filled_quantity":str(total),name+"_entry_vwap":str(value/total) if total else None,
                name+"_fees":str(fees) if all(o.fees_known for o in selected) else None,name+"_funding":str(funding) if funding_known else None,
                name+"_observed_funding":str(funding),name+"_funding_known":funding_known,
                name+"_funding_basis":"paper_estimate" if name=="paper" else "private_settlements",name+"_net":str(cash-fees+funding) if known else None,
                name+"_normalized_net":str(cash-normalized+funding) if known else None,name+"_reconciled":known,
                name+"_fill_rate":float(total/dec(plan.quantity)),name+"_partial_fill":0<float(total)<plan.quantity,
                name+"_partial_execution_observed":any(len(o.executions)>1 or 0<o.filled<dec(o.command.qty) for o in entries),
                name+"_entry_execution_fragments":sum(len(o.executions) for o in entries)})
            signatures[name]=[(o.command.reason,o.command.side,o.command.qty,o.command.mode,o.command.price) for o in selected]
            for order in selected:
                c=order.command
                orders.append(dict(arm=name,**asdict(c),status=order.status,order_id=order.order_id,filled=str(order.filled),
                    average=str(order.average) if order.average is not None else None,fees=str(order.fees) if order.fees_known else None,
                    reported=str(order.reported_filled),fees_known=order.fees_known,reconciled=order.confirmed,unknown=order.unknown,rejection=order.rejection,
                    sent_ns=order.sent_ns,ack_ns=order.ack_ns,first_fill_ns=order.first_fill_ns,final_fill_ns=order.final_fill_ns,
                    cancel_ns=order.cancel_ns,exec_ids=json.dumps(list(order.executions))))
                feature=feature_events.get(intent.source_sequence,{})
                pred=prediction_events.get((intent.source_sequence,plan.side.value),{})
                submit=submit_events.get((intent.source_sequence,plan.side.value),{})
                latency.append(dict(pair_id=pair,arm=name,link_id=c.link_id,observation_ns=intent.observed_ns,
                    features_start_ns=feature.get("feature_start_ns"),features_end_ns=feature.get("feature_end_ns"),
                    worker_dispatch_ns=submit.get("worker_dispatch_ns"),
                    predict_start_ns=pred.get("predict_end_ns",0)-pred.get("predict_ns",0) if pred else None,
                    predict_end_ns=pred.get("predict_end_ns"),result_received_ns=pred.get("received_ns"),admission_ns=admissions.get(pair),
                    dispatch_ns=c.created_ns,send_ns=order.sent_ns,ack_ns=order.ack_ns,first_fill_receipt_ns=order.first_fill_ns,
                    last_fill_receipt_ns=order.final_fill_ns,
                    observation_to_fill_receipt_ms=(order.first_fill_ns-intent.observed_ns)/1e6 if order.first_fill_ns else None,
                    over_100ms=order.first_fill_ns-intent.observed_ns>100_000_000 if order.first_fill_ns else None,
                    clock="local perf_counter; execTime is separately retained exchange wall ms"))
        row["unmatched"]=bool(dec(row["demo_filled_quantity"]))!=bool(dec(row["paper_filled_quantity"]))
        row["commands_differ"]=signatures["demo"]!=signatures["paper"]
        if row["demo_net"] is not None and row["paper_net"] is not None:
            delta=dec(row["demo_net"])-dec(row["paper_net"])
            row["delta_usdt"]=str(delta);row["delta_bps"]=float(delta/dec(plan.notional)*10000)
            row["fee_component_usdt"]=str(dec(row["paper_fees"])-dec(row["demo_fees"]))
            row["funding_component_usdt"]=str(dec(row["demo_funding"])-dec(row["paper_funding"]))
            row["price_quantity_commands_cash_component_usdt"]=str(delta-dec(row["fee_component_usdt"])-dec(row["funding_component_usdt"]))
            row["normalized_delta_usdt"]=str(dec(row["demo_normalized_net"])-dec(row["paper_normalized_net"]))
        row["interpretation"]="unmatched" if row["unmatched"] else "different subsequent commands" if row["commands_differ"] else "same commands; compare actual price/fee fills"
        pairs.append(row)
    for e in session.events:
        if e["event"]=="ml_decision":
            p=e["payload"];f=p["forecast"]
            ml.append(dict(source_sequence=f["source"]["source_sequence"],symbol=f["source"]["symbol"],side=f["side"],
                model_version=f["model_version"],p_target_first=f["p_target_first"],reasons="|".join(p["reasons"]),
                pair_id=p.get("pair_id"),received_ns=p["received_ns"],source_ns=f["source"]["available_mono_ns"],
                predict_end_ns=f["produced_mono_ns"],adapter_end_ns=e["received_ns"],
                data_to_adapter_ms=(e["received_ns"]-f["source"]["available_mono_ns"])/1e6,
                over_100ms=e["received_ns"]-f["source"]["available_mono_ns"]>100_000_000))
    fields=lambda rows,default:list(dict.fromkeys(k for r in rows for k in r)) or default
    table(root/"paired_orders.csv",pairs,fields(pairs,["pair_id","symbol","demo_net","paper_net","delta_usdt"]))
    table(root/"execution_reconciliation.csv",orders,fields(orders,["arm","link_id","status","unknown"]))
    table(root/"latency.csv",latency,fields(latency,["pair_id","arm","observation_to_fill_receipt_ms"]))
    table(root/"ml_decision_funnel.csv",ml,fields(ml,["symbol","side","p_target_first","reasons"]))
    with (root/"experiment-events.jsonl").open("x",encoding="utf-8") as file:
        for event in session.events:file.write(json.dumps(event,default=str)+"\n")
    has_ml=any(i.author=="ml" and any(b["pair_id"]==p for arm in session.arms.values() for b in arm.bookings) for p,i in all_intents.items())
    result=dict(protocol=PROTOCOL,run_id=session.run,status="COMPLETE" if reconciled and reason=="duration_elapsed" else "INCOMPLETE",
        reason=reason,positions_reconciled=reconciled,ml_status="ML_FILLS_OBSERVED_NOT_UTILITY_PROVEN" if has_ml else "ML_TRADING_NOT_TESTED",
        hypothesis="NOT_TESTED" if not any(dec(r["demo_filled_quantity"]) and dec(r["paper_filled_quantity"]) for r in pairs) else "DESCRIPTIVE_PAIRED_EXECUTIONS_ONLY",
        pairs=len(pairs),research_balances={n:a.broker.balance if all(o.fees_known for o in a.venue.orders.values()) else None for n,a in session.arms.items()},
        unconfirmed_pairs=list(session.portfolio.reservations),funding_complete=session.funding_complete(),
        expected_funding=sorted(session.expected_funding),observed_funding=sorted(session.observed_funding),recorder_health=session.recorder.health(),
        preflight=session.preflight,start_ns=session.start_ns,finished_ns=time.perf_counter_ns(),
        interpretation="Two execution simulators; no live matching/portfolio profitability claim. Unknown net remains blank.")
    def quantiles(values):
        values=sorted(values)
        return {k:values[int((len(values)-1)*q)] if values else None for k,q in (("p50",.5),("p95",.95),("p99",.99),("max",1))}
    result["latency"]=dict(loop_lateness_ms=quantiles(session.loop_lateness_ms),
        data_to_adapter_ms=quantiles([r["data_to_adapter_ms"] for r in ml]),
        observation_to_fill_receipt_ms=quantiles([r["observation_to_fill_receipt_ms"] for r in latency if r["observation_to_fill_receipt_ms"] is not None]),
        budgets_ms=dict(loop=20,shadow_addition=5,adapter=250),shadow_addition_status="NOT_MEASURED_WITHOUT_OFF_COUNTERFACTUAL",
        clock_scope="received fills, not assumed 100ms exchange execution; exchange execTime retained in raw fills")
    result["comparison"]=dict(delta_usdt=quantiles([float(r["delta_usdt"]) for r in pairs if r.get("delta_usdt") is not None]),
        unmatched_pairs=sum(r["unmatched"] for r in pairs),changed_commands=sum(r["commands_differ"] for r in pairs),
        filled_pairs={name:sum(dec(r[name+"_filled_quantity"])>0 for r in pairs) for name in session.arms},
        partial_execution_pairs={name:sum(r[name+"_partial_execution_observed"] for r in pairs) for name in session.arms},
        final_net={name:sum(float(r[name+"_net"]) for r in pairs) if reconciled and all(r[name+"_net"] is not None for r in pairs) else None for name in session.arms},
        balance_semantics="research booked realized balance; entry fees allocated on exit; not actual Demo wallet equity")
    (root/"loop-lateness-ms.json").write_text(json.dumps(session.loop_lateness_ms)+"\n")
    (root/"result.json").write_text(json.dumps(result,indent=2,default=str)+"\n")
    (root/"DEMO_PAPER_1H_REVIEW.md").write_text("# Demo / paper execution comparison\n\n"+json.dumps(result,indent=2,default=str)+"\n")
    (root/"instrument-specifications.json").write_text(json.dumps(session.public.spec_evidence,indent=2)+"\n")
    manifest={}
    for file in root.rglob("*"):
        if file.is_file():
            with file.open("rb") as stream:checksum=hashlib.file_digest(stream,"sha256").hexdigest()
            manifest[str(file.relative_to(root))]=dict(bytes=file.stat().st_size,sha256=checksum)
    (root/"manifest.json").write_text(json.dumps(dict(protocol=PROTOCOL,files=manifest),indent=2)+"\n")
    return result
