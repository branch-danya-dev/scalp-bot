"""Bounded local queue benchmark at archived packet cadence; no sockets/network."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
from time import perf_counter_ns
import zlib

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/"tests"))
from scalp_bot.bybit import MarketMessage,_process_market_queue
from scalp_bot.scenario import ScenarioRouter
from scalp_bot.parallel_scenarios import ParallelScenarioRouter
from test_scenario_remediation import market,level


def percentile(values,p):
    values=sorted(values)
    return values[min(len(values)-1,int((len(values)-1)*p))] if values else None


def source_rows(database):
    connection=sqlite3.connect(f"file:{Path(database).resolve().as_posix()}?mode=ro",uri=True)
    result=[];diagnostics=[]
    # The audited ETH gap starts at I15061226. Read a bounded surrounding range.
    for (blob,) in connection.execute("SELECT payload FROM inputs WHERE symbol='ETHUSDT' AND kind='market_message' AND idx BETWEEN 15000000 AND 15061226 ORDER BY idx"):
        row=json.loads(zlib.decompress(blob));b=row["body"]
        if b.get("topic","").startswith("orderbook.50"):
            diagnostics.append(dict(sequence=row["sequence"],mono=row["processingMonoNs"],
                queue_depth=b.get("queue_depth",0),queue_lag_ms=b.get("queue_lag_ms",0),
                receipt_to_processing_ms=(row["processingMonoNs"]-b["receipt_mono_ns"])/1e6,
                parse_ms=(b["parsed_mono_ns"]-b["receipt_mono_ns"])/1e6))
    connection.close()
    if not diagnostics:raise ValueError("no audited packets")
    end=diagnostics[-1]["mono"]
    result=[r for r in diagnostics if r["mono"]>=end-2_000_000_000]
    return result,diagnostics


async def run_case(rows,parallel,slow):
    router=ParallelScenarioRouter() if parallel else ScenarioRouter()
    candles,book,context,structure=market([level(),level("support","B",mature=False,center=100)])
    enabled={"level_breakout":True,"weak_level_rejection":True}
    queue=asyncio.Queue(maxsize=512);stop=asyncio.Event();elapsed=[];processed=[]
    async def callback(message):
        start=perf_counter_ns()
        router.observe("REVIEWUSDT",context,candles,structure,enabled,start/1e9)
        if slow:await asyncio.sleep(.08)
        elapsed.append((perf_counter_ns()-start)/1e6);processed.append(message.queue_lag_ms)
    consumer=asyncio.create_task(_process_market_queue(queue,callback,stop,max_lag_seconds=.5))
    started=perf_counter_ns();origin=rows[0]["mono"]
    max_depth=0
    try:
        for row in rows:
            target=started+row["mono"]-origin
            await asyncio.sleep(max(0,(target-perf_counter_ns())/1e9))
            if consumer.done():break
            now=perf_counter_ns()
            queue.put_nowait(MarketMessage(topic="orderbook.50.REVIEWUSDT",received_at_ns=now,parsed_mono_ns=now))
            max_depth=max(max_depth,queue.qsize())
        try:await asyncio.wait_for(queue.join(),1)
        except TimeoutError:pass
        failure=type(consumer.exception()).__name__ if consumer.done() and not consumer.cancelled() and consumer.exception() else None
    finally:
        stop.set();consumer.cancel();await asyncio.gather(consumer,return_exceptions=True)
    return dict(parallel=parallel,slow_handler=slow,processed=len(processed),max_depth=max_depth,failure=failure,
        handler_ms={f"p{int(p*100)}":percentile(elapsed,p) for p in (.5,.95,.99)},
        queue_lag_ms={f"p{int(p*100)}":percentile(processed,p) for p in (.5,.95,.99)},
        consumer_finished=consumer.done())


async def main(database,output):
    rows,diagnostics=source_rows(database)
    results=[]
    for parallel,slow in ((False,False),(True,False),(False,True),(True,True)):
        results.append(await run_case(rows,parallel,slow))
    report=dict(source_range=[diagnostics[0]["sequence"],diagnostics[-1]["sequence"]],packets=len(rows),
        recorded={name:{f"p{int(p*100)}":percentile([r[name] for r in diagnostics],p) for p in (.5,.95,.99)}
                  for name in ("queue_depth","queue_lag_ms","receipt_to_processing_ms","parse_ms")},
        cases=results,scope="2-second recorded processing cadence, shared applicability and scenario bookkeeping; not full socket or strategy benchmark",
        unobserved=["DNS/TLS handshake phases","per-packet writer/strategy/context costs before old gap"],
        handshake_cause="unresolved; close_timeout does not repair network availability")
    Path(output).write_text(json.dumps(report,indent=2)+"\n");print(json.dumps(report))


if __name__=="__main__":asyncio.run(main(sys.argv[1],sys.argv[2]))
