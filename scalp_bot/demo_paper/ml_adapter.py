"""Research-only admission. ShadowAdapter and the predictor stay side-effect free."""
from dataclasses import asdict
from .contracts import Intent, SafetyError
from ..domain import Side, TradePlan
from ..ml.contracts import forecast_rejections
from ..ml.features import FEATURE_SCHEMA

class ResearchMLAdapter:
    def __init__(self,metadata,portfolio,emit):
        self.metadata=metadata;self.portfolio=portfolio;self.emit=emit;self.seen=set()
        if metadata["decision_policy"]["p_target_min"]!=.55: raise SafetyError("fixed threshold mismatch")
        policy=metadata["plan_policy"]
        if (policy["target_bps"],policy["stop_bps"],policy["horizon_seconds"],policy["nominal_usdt"],policy["sample_seconds"],policy["partial"])!=(30,15,30,100,10,False):
            raise SafetyError("V2 fixed plan mismatch")

    def accept(self,forecast,current,session,now_ns,source_quote,*,rule_ready=False):
        m=self.metadata;reasons=list(forecast_rejections(forecast,current,now_mono_ns=now_ns,
            max_data_age_ns=1_000_000_000,model_version=m["model_version"],plan_policy_version=m["policy_version"]))
        if forecast.horizon_ms!=30000:reasons.append("plan_horizon_mismatch")
        if forecast.source.feature_schema!=FEATURE_SCHEMA:reasons.append("feature_schema_mismatch")
        key=(forecast.source.symbol,forecast.source.selection_epoch,forecast.source.source_sequence,forecast.side)
        if key in self.seen:reasons.append("duplicate_forecast")
        self.seen.add(key)
        if forecast.p_target_first<.55:reasons.append("probability_abstention")
        from ..ml.arbitration import select_proposal, SymbolPhase, ProposalSource
        if select_proposal(SymbolPhase.PREPARING,rule_ready=rule_ready,ml_ready=True)==ProposalSource.RULE:
            reasons.append("ordinary_ready_priority")
        if not session.book_is_fresh() or not session.deep_book_is_fresh():reasons.append("book_unhealthy")
        side=Side(forecast.side);instrument=session.instrument;quote=session.orderbook.executable_entry(side)
        if not quote or not source_quote:reasons.append("quote_missing")
        elif abs(quote/source_quote-1)*10000>self.portfolio.config.max_entry_drift_bps:reasons.append("entry_drift")
        if reasons:
            self.emit("ml_decision",dict(forecast=asdict(forecast),reasons=reasons,received_ns=now_ns));return False,reasons
        sign=1 if side==Side.LONG else -1
        # Initial intent geometry; each arm derives 30/15 bps from its actual fill, as in V2 labels.
        # Original source time/drift remains unchanged.
        stop=instrument.stop_price(source_quote*(1-sign*.0015),side)
        target=instrument.target_price(source_quote*(1+sign*.003),side)
        normalized=instrument.normalize_quantity(entry_price=quote,requested_notional=100,market_order=True)
        if normalized is None:return False,["instrument_constraints"]
        qty,notional=normalized;cfg=self.portfolio.config
        loss=qty*abs(quote-stop)+notional*(2*cfg.taker_fee_rate+2*cfg.slippage_bps/10000)
        if loss>self.portfolio.balance*cfg.max_trade_all_in_loss_fraction:return False,["shared_trade_risk_limit"]
        if sign*(quote-stop)<=0 or sign*(target-quote)<=0:return False,["plan_invalidated"]
        costs=notional*(2*cfg.taker_fee_rate+2*cfg.slippage_bps/10000)
        profit=sign*(target-quote)*qty-costs
        plan=TradePlan(current.symbol,"trend_impulse_ml",side,source_quote,quote,stop,target,notional,
            notional/self.portfolio.balance,loss,profit+costs,costs,profit,loss,profit/loss,
            sign*(quote/source_quote-1),"ml:"+str(key),quantity=qty,
            strategy_details=dict(allowRunner=False,forecast=asdict(forecast),researchOnly=True,
                modelVersion=m["model_version"],planPolicy=m["policy_version"],economics={"partialPlanned":False}))
        intent=Intent.freeze(self.portfolio.run,plan,forecast.source.source_sequence,forecast.source.available_mono_ns,"ml",m["model_version"])
        accepted,reason=self.portfolio.admit(intent,instrument)
        self.emit("ml_decision",dict(forecast=asdict(forecast),pair_id=intent.pair_id,reasons=[reason],received_ns=now_ns))
        return accepted,[reason]
