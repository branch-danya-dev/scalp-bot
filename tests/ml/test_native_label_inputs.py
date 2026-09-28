from dataclasses import asdict
import msgspec
from scalp_bot.config import Settings
from scalp_bot.ml.prepared_labels import PreparedLabelEngine
from scalp_bot.ml.label_replay import replay_label_inputs, ContextObservation, context_from_payload
from test_prepared_labels import prepared, book


def test_persisted_full_label_input_replay_matches_broker_costs_and_rejects_tamper():
    cfg=Settings(_env_file=None)
    records=[]
    def emit(kind,body):records.append(dict(kind=kind,body=msgspec.to_builtins(body)))
    engine=PreparedLabelEngine(cfg,emit,audit=lambda body:emit("label_input",body))
    p=prepared();p["economicsAllowed"]=True
    engine.add(p)
    args=dict(now_ns=1_250_000_000,wall_ms=100250,sequence=50,epoch=1,
        book=book(99.99,100),depth=book(99.99,100),fresh=True,exchange_ms=100250)
    engine.market("AAAUSDT",**args)
    engine.market("AAAUSDT",**dict(args,now_ns=1_300_000_000,wall_ms=100300,sequence=51,
        book=book(99.4,99.41),depth=book(99.4,99.41),exchange_ms=100300))
    report=replay_label_inputs(records,cfg)
    assert report["status"]=="PASS" and report["complete"]==1, report
    assert report["actualHash"]==report["expectedHash"]
    records[-1]["body"]["netPnl"]+=1
    assert replay_label_inputs(records,cfg)["status"]=="FAIL"


def test_missing_label_tape_never_promotes_normalized_venue_replay():
    result=replay_label_inputs([],Settings(_env_file=None))
    assert result["status"]=="INCONCLUSIVE" and not result["matched"]


def test_context_round_trip_preserves_typed_features_and_exact_freshness_reads(tmp_path):
    from types import SimpleNamespace
    from test_features import context
    from scalp_bot.domain import Trend
    original=SimpleNamespace(symbol="AAA",market_context=context(),trend=Trend.UP,last_price=100,
        orderbook=book(99.99,100),depth_orderbook=lambda:book(99.99,100),decisions={},
        book_is_fresh=lambda:True,deep_book_is_fresh=lambda:False)
    observed=ContextObservation(original)
    assert observed.deep_book_is_fresh() is False
    # Real JSON/hash/codec boundary matters: horizons use integer keys in memory.
    from scalp_bot.research_journal import ResearchJournal, read_research
    journal=ResearchJournal(tmp_path/"context.jsonl.gz","capture",{})
    journal.append("label_input",observed.payload())
    journal.close()
    rows=list(read_research(journal.path))
    replay=context_from_payload(rows[1]["body"])
    assert replay.market_context==original.market_context
    assert replay.deep_book_is_fresh() is False and not replay.reads
