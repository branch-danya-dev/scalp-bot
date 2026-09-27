"""No default action can connect or create an order."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from .contracts import SafetyError
from .preflight import ROOT, local_preflight, load_credentials, connected_preflight, PublicRest
from .transport import DemoRest

async def main(argv=None):
    parser=argparse.ArgumentParser(description="Explicit Demo/paper research; no automatic Start")
    parser.add_argument("action",choices=("preflight","connected-preflight","start"))
    parser.add_argument("--passport",default=str(ROOT/"docs/demo-paper-1h/passport.json"))
    parser.add_argument("--model-dir",required=True)
    parser.add_argument("--output",required=True)
    parser.add_argument("--secrets",default=str(ROOT/".env.demo-paper.local"))
    parser.add_argument("--confirm-passport")
    args=parser.parse_args(argv)
    result,cfg,metadata=local_preflight(args.passport,args.model_dir,args.output)
    print(json.dumps(result,indent=2))
    if args.action=="preflight":return 0
    if args.action=="start" and args.confirm_passport!=result["passport_sha256"]:
        raise SafetyError("Start requires owner confirmation of the displayed passport hash")
    credentials=load_credentials(args.secrets)
    if args.action=="connected-preflight":
        rest=DemoRest(credentials,enabled=True);public=PublicRest(cfg)
        try:print(json.dumps(await connected_preflight(rest,public,credentials),indent=2))
        finally:await rest.close();await public.close()
        return 0
    from .runtime import Session
    session=Session(cfg,metadata,args.model_dir,credentials,result,args.output)
    final=await session.run_session();print(json.dumps(final,indent=2))
    return 0 if final["status"]=="COMPLETE" else 2

if __name__=="__main__":
    try:sys.exit(asyncio.run(main()))
    except SafetyError as exc:print("BLOCKED: "+str(exc),file=sys.stderr);sys.exit(2)
    except (ValueError,KeyError,OSError):print("BLOCKED: invalid or missing local configuration/artifact",file=sys.stderr);sys.exit(2)
