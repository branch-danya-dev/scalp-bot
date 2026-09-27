"""Read-only preflight and frozen public passport, with no order authority."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import httpx
from pydantic import SecretStr
from .contracts import PROTOCOL, SafetyError, dec, digest
from .transport import Credentials, REST, PRIVATE_WS, ACCOUNT_SCOPES
from ..bybit import BybitRestClient
from ..config import Settings
from ..instrument import InstrumentSpec
from ..ml.features import FEATURE_SCHEMA

ROOT=Path(__file__).resolve().parents[2]
MODEL_SHA="a9bb5445db534b93bc6a246150c5c959ae58318b9139311da85b5e7ea05888d2"
MODEL_MANIFEST_SHA="15b55fa2c360729cebb37d8cb9cbe432249907873d7016b3b81929df173082f3"
PUBLIC_REST="https://api.bybit.com"
PUBLIC_WS="wss://stream.bybit.com/v5/public/linear"

class PublicRest(BybitRestClient):
    def __init__(self,config):
        if config.bybit_rest_url!=PUBLIC_REST or config.bybit_rest_fallback_urls or config.bybit_public_ws_url!=PUBLIC_WS:
            raise SafetyError("public destinations mismatch")
        if config.bybit_api_key.get_secret_value() or config.bybit_api_secret.get_secret_value():
            raise SafetyError("public client must have no credentials")
        super().__init__(config)
        self.client=httpx.AsyncClient(timeout=10,follow_redirects=False,trust_env=False)
        self.spec_evidence={}
    async def _private_get(self,*args,**kwargs):raise SafetyError("public private access forbidden")
    async def _get(self,path,params,**kwargs):
        if not path.startswith("/v5/market/"):raise SafetyError("public read-only market path required")
        result=await super()._get(path,params,**kwargs)
        if path=="/v5/market/instruments-info":
            for row in result.get("list",[]):
                if row.get("contractType")!="LinearPerpetual" or row.get("settleCoin")!="USDT" or row.get("quoteCoin")!="USDT":
                    raise SafetyError("only verified linear USDT perpetual supported")
                mandatory=[row.get("priceFilter",{}).get("tickSize")]+[row.get("lotSizeFilter",{}).get(k) for k in
                    ("qtyStep","minOrderQty","minNotionalValue","maxOrderQty","maxMktOrderQty")]
                if any(v is None or dec(v)<=0 for v in mandatory):raise SafetyError("unknown mandatory current instrument specification")
                self.spec_evidence[row["symbol"]]=dict(observed_wall_ms=int(time.time()*1000),source=PUBLIC_REST+path,row=row)
        return result


def load_credentials(path):
    # File-only secrets keep credentials out of worker environment and command lines.
    path=Path(path)
    if not path.is_file():raise SafetyError("configure ignored .env.demo-paper.local on this computer")
    values={}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):continue
        if "=" not in line:raise SafetyError("invalid local secret profile")
        key,value=line.split("=",1);values[key.strip()]=value.strip()
    if set(values)!={"DEMO_API_KEY","DEMO_API_SECRET","DEMO_EXPECTED_UID","DEMO_ENVIRONMENT"} or values["DEMO_ENVIRONMENT"]!="demo":
        raise SafetyError("explicit Demo environment profile required")
    return Credentials(values["DEMO_API_KEY"],values["DEMO_API_SECRET"],values["DEMO_EXPECTED_UID"])


def settings(output):
    captured=json.loads((ROOT/"docs/pr58-readiness/run-provenance.json").read_text())["B"]["config"]
    # Preserve the complete ordinary decision/risk/management profile. These
    # changes affect destinations, run lifetime and recording only.
    values=dict(captured)
    values.update(start_balance=1000,bybit_rest_url=PUBLIC_REST,bybit_rest_fallback_urls="",
        bybit_public_ws_url=PUBLIC_WS,bybit_api_key=SecretStr(""),bybit_api_secret=SecretStr(""),
        fee_rate_mode="configured",paper_run_duration_seconds=3600,session_dir=str(output),run_label=PROTOCOL)
    # Explicit defaults prevent inherited SCALP_* environment overrides from
    # changing an unclassified setting. Nothing is read from .env.
    defaults={k:f.default for k,f in Settings.model_fields.items()}
    defaults.update(values)
    return Settings(_env_file=None,**defaults)


def source_hashes():
    return {str(p.relative_to(ROOT)).replace("\\","/"):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT/"scalp_bot").rglob("*.py"))}


def local_preflight(passport_path,model_dir,output):
    passport=json.loads(Path(passport_path).read_text(encoding="utf-8-sig"))
    required=dict(protocol=PROTOCOL,duration_seconds=3600,research_equity_usdt_each_arm=1000,
        trigger_semantics="local-executable-bid-ask-with-resting-maker-limits-v1",private_rest=REST,private_ws=PRIVATE_WS,
        threshold=.55,sample_seconds=10,horizon_seconds=30,target_bps=30,stop_bps=15)
    if any(passport.get(k)!=v for k,v in required.items()):raise SafetyError("passport invariant mismatch")
    bounds=passport["bounds"]
    if bounds!={"session_loss_usdt_each_arm":30,"reconciliation_seconds":90,"private_snapshot_max_age_seconds":30,
               "execution_queue_capacity":32,"rest_timeout_seconds":5,"source_to_adapter_p99_ms":250,
               "event_loop_p99_ms":20,"shadow_added_p99_ms":5}:
        raise SafetyError("predeclared numeric bounds mismatch")
    manifest_bytes=(Path(model_dir)/"manifest.json").read_bytes()
    manifest_sha=hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha!=MODEL_MANIFEST_SHA:raise SafetyError("V2 calibration manifest identity mismatch")
    metadata=json.loads(manifest_bytes)
    sha=hashlib.sha256((Path(model_dir)/"model.cbm").read_bytes()).hexdigest()
    if sha!=MODEL_SHA or metadata["model_sha256"]!=sha or metadata["feature_schema"]!=FEATURE_SCHEMA:
        raise SafetyError("V2 artifact/schema mismatch")
    cfg=settings(output)
    clean=cfg.model_dump(mode="json");clean.pop("bybit_api_key");clean.pop("bybit_api_secret")
    config_hash=digest({k:v for k,v in clean.items() if k not in ("session_dir",)})
    if passport["config_sha256"]!=config_hash:raise SafetyError("frozen profile mismatch")
    return dict(protocol=PROTOCOL,status="LOCAL_PREFLIGHT_ONLY",passport_sha256=digest(passport),
        source_sha256=digest(source_hashes()),config_sha256=config_hash,model_sha256=sha,
        model_manifest_sha256=manifest_sha,
        private_rest=REST,private_ws=PRIVATE_WS,public_rest=PUBLIC_REST,public_ws=PUBLIC_WS,
        strategy_roles={"rule":"unchanged parallel legacy, shared risk","ml":"research adapter; V2 .55; separate process"},
        sizing="1000 USDT per arm; one shared rule+ML reservation per intent; requested quantities identical",
        risk={k:clean[k] for k in ("risk_fraction","max_trade_all_in_loss_fraction","max_total_risk_fraction","max_leverage","max_position_leverage","max_open_positions")},
        bounds=bounds,trigger_semantics=required["trigger_semantics"],output=str(Path(output).resolve()),
        connected=False,orders_sent=0,launch_authorized=False),cfg,metadata


async def connected_preflight(rest,public,credentials):
    if rest.write_enabled:raise SafetyError("preflight must prohibit orders")
    key=await rest.request("GET","/v5/user/query-api")
    if str(key.get("userID"))!=credentials.expected_uid or key.get("readOnly")!=0:
        raise SafetyError("Demo account/key identity or permission mismatch")
    permissions=key.get("permissions",{})
    if not {"Order","Position"}.issubset(permissions.get("ContractTrade",[])) and "DerivativesTrade" not in permissions.get("Derivatives",[]):
        raise SafetyError("mandatory trading permissions unavailable")
    account=await rest.request("GET","/v5/account/info")
    if account.get("unifiedMarginStatus") not in (3,4,5,6):raise SafetyError("supported unified Demo account required")
    wallet=await rest.request("GET","/v5/account/wallet-balance",dict(accountType="UNIFIED"))
    for category,settle_coin in ACCOUNT_SCOPES:
        params=dict(category=category)
        if settle_coin:params["settleCoin"]=settle_coin
        orders=await rest.pages("/v5/order/realtime",params)
        if orders:raise SafetyError("dedicated account has pre-existing orders")
        if category!="spot":
            positions=await rest.pages("/v5/position/list",params)
            if any(dec(p["size"])!=0 for p in positions):raise SafetyError("dedicated account has pre-existing positions")
            if category=="linear" and any(int(p["positionIdx"])!=0 for p in positions):raise SafetyError("one-way position mode required")
    # Probe mandatory funding-history endpoint without transactions/orders.
    await rest.pages("/v5/account/transaction-log",dict(accountType="UNIFIED",category="linear",currency="USDT",limit=50))
    return dict(status="CONNECTED_NO_ORDER_PREFLIGHT",masked_uid="***"+credentials.expected_uid[-4:],
        account_mode=account.get("marginMode"),unified_margin_status=account.get("unifiedMarginStatus"),
        wallet=wallet,position_mode="no occupied hedge rows; each admitted symbol rechecked positionIdx=0",
        connected=True,orders_sent=0)
