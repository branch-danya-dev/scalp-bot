"""Independent exit paths: shadow must not change ordinary decisions or ledger."""
import asyncio,importlib.util,json,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('shadow_check',Path(__file__).with_name('check-ml-shadow-offline.py'))
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

async def main(model,output):
    results=[]
    for kind in ('partial_then_stop','stalled_giveback','duration_elapsed'):
        off=await module.run(model,False,exit_kind=kind)
        shadow=await module.run(model,True,exit_kind=kind)
        assert off['events']==shadow['events']
        assert off['ledger']==shadow['ledger'] and off['balance']==shadow['balance']
        results.append(dict(episode=kind,ordinary_events=len(off['events']),trades=len(off['ledger']),
            ledger_identical=True,decisions_identical=True,balance=off['balance'],forecasts=shadow['forecasts']))
    Path(output).write_text(json.dumps(dict(scope='synthetic actual engine regression, not latency or profitability',episodes=results),indent=2)+'\n')
    print(json.dumps(results))

if __name__=='__main__':asyncio.run(main(sys.argv[1],sys.argv[2]))
