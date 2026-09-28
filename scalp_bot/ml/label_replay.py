"""Replay full label-driving inputs; external quote replay is not label parity."""
from collections import deque
from types import SimpleNamespace
import msgspec

from ..domain import OrderBook, TradeTick, StrategyDecision, Trend
from ..strategy.market_context import MarketContext
from .prepared_labels import PreparedLabelEngine, LabelPolicy
from .dataset_promotion import digest


class ContextObservation:
    """Read-through wrapper records exactly the freshness reads already made."""
    def __init__(self, session):
        self.session, self.reads = session, []
        self.frozen = msgspec.to_builtins(dict(symbol=session.symbol,market_context=session.market_context,
            trend=session.trend,last_price=session.last_price,orderbook=session.orderbook,
            depth=session.depth_orderbook(), decisions=session.decisions), str_keys=True)

    def __getattr__(self, name):
        return getattr(self.session,name)

    def _fresh(self,name):
        result=getattr(self.session,name)()
        self.reads.append([name,result])
        return result

    def book_is_fresh(self):return self._fresh("book_is_fresh")
    def deep_book_is_fresh(self):return self._fresh("deep_book_is_fresh")

    def payload(self):return dict(self.frozen,freshnessReads=self.reads)


class ReplayedContext(SimpleNamespace):
    def _fresh(self,name):
        if not self.reads or self.reads[0][0]!=name:
            raise ValueError("label context freshness order differs")
        return self.reads.popleft()[1]
    def book_is_fresh(self):return self._fresh("book_is_fresh")
    def deep_book_is_fresh(self):return self._fresh("deep_book_is_fresh")
    def depth_orderbook(self):return self.depth


def context_from_payload(body):
    return ReplayedContext(symbol=body["symbol"],trend=Trend(body["trend"]),last_price=body["last_price"],
        market_context=msgspec.convert(body["market_context"],type=MarketContext,strict=False)
            if body["market_context"] is not None else None,
        orderbook=msgspec.convert(body["orderbook"],type=OrderBook,strict=False),
        depth=msgspec.convert(body["depth"],type=OrderBook,strict=False),
        decisions={k:msgspec.convert(v,type=StrategyDecision,strict=False) for k,v in body["decisions"].items()},
        reads=deque(body["freshnessReads"]))


def replay_label_inputs(records, config):
    actual, expected = [], []
    engine=PreparedLabelEngine(config,lambda k,v:actual.append([k,v]))
    inputs=0
    try:
        for record in records:
            kind,body=record["kind"],record["body"]
            if kind in {"prepared_label","prepared_censored"}:
                expected.append([kind,body])
            elif kind=="label_input":
                inputs+=1
                method=body["method"]
                if method=="add":
                    if body["policy"]!=msgspec.to_builtins(engine.policy):
                        raise ValueError("label policy mismatch")
                    engine.add(body["prepared"])
                elif method=="market":
                    args=dict(body["arguments"])
                    for key,typ in (("book",OrderBook),("depth",OrderBook),("tick",TradeTick)):
                        # Preserve the persisted numeric representation as well
                        # as values; float coercion changes exact provenance hashes.
                        if args.get(key) is not None:args[key]=typ(**args[key])
                    engine.market(body["symbol"],**args)
                elif method=="context":
                    session=context_from_payload(body["session"])
                    path=engine.pending[body["identity"]]
                    engine._done(body["identity"],path.context(session,**body["arguments"]))
                    if session.reads:raise ValueError("unused label freshness observations")
                elif method=="invalidate":
                    engine.invalidate(**{k:v for k,v in body.items() if k!="method"})
                else:raise ValueError("unsupported label input")
        if inputs and (digest(actual)!=digest(expected) or engine.pending):
            raise ValueError("label lifecycle/cost replay mismatch or pending path")
        labels=[v for k,v in actual if k=="prepared_label"]
        executable=sum(v.get("trainingReady") is True and not v.get("censor_reason") for v in labels)
        return dict(status="PASS" if inputs and executable else "INCONCLUSIVE", inputs=inputs,
            labels=len(labels),complete=executable,censored=len(labels)-executable,
            matched=bool(inputs),expectedHash=digest(expected),actualHash=digest(actual),
            nativeSourceGate="REQUIRES_PRIMARY_SOURCE_AND_NATIVE_PATH_PROOF",promotionAuthorized=False)
    except (ValueError,KeyError,TypeError) as exc:
        return dict(status="FAIL",inputs=inputs,reason=str(exc),promotionAuthorized=False)
