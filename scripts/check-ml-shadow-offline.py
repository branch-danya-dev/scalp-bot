"""Run actual deterministic engine fixture twice; no network adapter survives mocks."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/"tests"))
from scalp_bot.ml.contracts import SnapshotRef
from scalp_bot.ml.features import FEATURE_SCHEMA,ContextCoverage,extract_context_features
from scalp_bot.ml.worker import InferenceWorker
from scalp_bot.ml.shadow import ShadowAdapter


async def run(model,shadow,*,exit_kind="partial_then_stop"):
    import pytest
    from scalp_bot.engine import TradingEngine
    from test_offline_portfolio import fixture
    worker=InferenceWorker(model) if shadow else None
    if worker:
        worker.start()
        start=time.perf_counter()
        while not worker.ready and not worker.failed and time.perf_counter()-start<30:
            worker.poll();await asyncio.sleep(.005)
        if not worker.ready:raise RuntimeError(worker.failed)
    monkeypatch=pytest.MonkeyPatch();original=TradingEngine._evaluate
    adapter=ShadowAdapter(json.loads((Path(model)/"manifest.json").read_text())) if shadow else None
    sequence=0;times=[];ticks=[];end_to_end=[];forecasts=0;stop=asyncio.Event()
    current={};sessions={}
    def consume(items):
        nonlocal forecasts
        for item in items:
            if item[0]!="forecast":continue
            f=item[1];session=sessions[f.source.symbol];now=time.perf_counter_ns()
            if session.instrument is not None:
                adapter.accept(f,current[f.source.symbol],now,quote=session.orderbook.best_ask,instrument=session.instrument)
            end_to_end.append((time.perf_counter_ns()-f.source.available_mono_ns)/1e6);forecasts+=1
    async def heartbeat():
        while not stop.is_set():
            before=time.perf_counter_ns();await asyncio.sleep(.001)
            ticks.append((time.perf_counter_ns()-before)/1e6)
    async def observed(engine,session):
        nonlocal sequence
        before=time.perf_counter_ns()
        await original(engine,session)
        context=session.market_context
        if context is not None:
            sequence+=1
            ref=SnapshotRef("synthetic-engine",session.symbol,1,sequence,context.observed_at_ms,
                            before,"local-shadow-fixture",FEATURE_SCHEMA)
            snapshot=extract_context_features(context,ref,ContextCoverage())
            if worker:
                current[session.symbol]=ref;sessions[session.symbol]=session
                worker.activate(session.symbol,1);worker.submit(snapshot,"long");consume(worker.poll())
        times.append((time.perf_counter_ns()-before)/1e6)
    monkeypatch.setattr(TradingEngine,"_evaluate",observed)
    heartbeat_task=asyncio.create_task(heartbeat())
    try:
        with tempfile.TemporaryDirectory() as temporary:
            engine,prefix,rows=await fixture(Path(temporary),monkeypatch,production=True,exit_kind=exit_kind)
            selected=[r for r in rows if r["event"] in {"decision","trade_opened","partial_take","trade_closed"}]
            if worker:
                deadline=time.perf_counter()+2
                while (worker.inflight or worker.latest) and time.perf_counter()<deadline:
                    consume(worker.poll());await asyncio.sleep(.0025)
            ledger=engine.broker.closed_trades
            return dict(events=selected,ledger=ledger,balance=engine.broker.balance,
                        evaluations_ms=times,event_loop_ms=ticks,end_to_end_ms=end_to_end,forecasts=forecasts,worker_info=worker.info if worker else None)
    finally:
        stop.set();await heartbeat_task
        monkeypatch.undo()
        if worker:worker.close()


def quantile(values,p):
    return sorted(values)[min(len(values)-1,int((len(values)-1)*p))] if values else None


async def main(model,output):
    off=await run(model,False);shadow=await run(model,True)
    assert off["events"]==shadow["events"], "ordinary decisions/execution differ"
    assert off["ledger"]==shadow["ledger"] and off["balance"]==shadow["balance"]
    report=dict(decisions_and_execution_identical=True,ledger_identical=True,ordinary_event_count=len(off["events"]),
        trades=len(off["ledger"]),balance=off["balance"],worker=shadow["worker_info"],
        evaluated_data_to_proposal_ms={f"p{int(p*100)}":quantile(shadow["end_to_end_ms"],p) for p in (.5,.95,.99)},
        observed_forecasts=shadow["forecasts"],
        event_loop_ms={mode:{f"p{int(p*100)}":quantile(result["event_loop_ms"],p) for p in (.5,.95,.99)}
                       for mode,result in (("off",off),("shadow",shadow))},
        evaluations_ms={mode:{f"p{int(p*100)}":quantile(result["evaluations_ms"],p) for p in (.5,.95,.99)}
                       for mode,result in (("off",off),("shadow",shadow))},
        scope="deterministic actual breakout/partial/stop synthetic engine fixture; not market PnL")
    report["event_loop_budget_passed"]=(report["event_loop_ms"]["shadow"]["p99"]<=20 and
        report["event_loop_ms"]["shadow"]["p99"]-report["event_loop_ms"]["off"]["p99"]<=5)
    Path(output).write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report))


if __name__=="__main__":asyncio.run(main(sys.argv[1],sys.argv[2]))
