"""Collect 30m technical smoke, audit, then fixed independent 12h E06 paper.

No orders are sent to the exchange. No .env or API credentials are loaded.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import zipfile

from scalp_bot.e01_live import LiveSmoke
from scalp_bot.e06_capture import load_profiles, verify_smoke, MAX_EVENTS, PROTOCOL, PROFILES
from scalp_bot.manifest_validation import fingerprint
from scalp_bot.run_manifest import code_provenance


def contract(root):
    files = ['scripts/run-e06-paper.py','scripts/audit-e01-capture.py','scripts/replay-paired-capture.py',
             'scripts/replay-e01-breakout-window.py'] + ['configs/'+p for p in PROFILES.values()]
    return dict(protocol=PROTOCOL, sourceSha256=code_provenance(root)['sourceSha256'],
                fileHashes={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in files})


async def run(args):
    root = Path(__file__).resolve().parents[1]
    configs = load_profiles(root)
    frozen = contract(root)
    output = Path(args.output).resolve()
    if args.phase == 'independent' and not args.smoke:
        raise ValueError('--smoke is required for independent phase')
    if output.exists(): raise ValueError('output directory already exists; evidence will not be overwritten')
    ancestor = output.parent
    while not ancestor.exists(): ancestor = ancestor.parent
    free = shutil.disk_usage(ancestor).free
    required = (100 if args.phase in ('auto','independent') else 10)*1024**3
    if free < required: raise ValueError(f'at least {required/1024**3:.0f} GiB free is required')
    print(f'E06 paper: {args.phase}; available {free/1024**3:.1f} GiB; output {output}',flush=True)
    if args.check:
        print('Local preflight passed. Auto: 30m smoke + audit/full replay + 12h independent, plus warmup. No network started.')
        return
    audit = runpy.run_path(str(root/'scripts/audit-e01-capture.py'))['audit']
    replay = runpy.run_path(str(root/'scripts/replay-paired-capture.py'))['run']
    if args.phase == 'independent':
        if not args.smoke: raise ValueError('--smoke is required for independent phase')
        verify_smoke(Path(args.smoke),configs['smoke'],audit)
    output.mkdir(parents=True,exist_ok=False)
    (output/'protocol.json').write_text(json.dumps(dict(**frozen,contractSha256=fingerprint(frozen)),ensure_ascii=False,indent=2),encoding='utf-8')
    phases = ('smoke','independent') if args.phase=='auto' else (args.phase,)
    try:
        with zipfile.ZipFile(output/'protocol-sources.zip','x',zipfile.ZIP_DEFLATED) as archive:
            for name,expected in frozen['fileHashes'].items():
                content=(root/name).read_bytes()
                if hashlib.sha256(content).hexdigest()!=expected:
                    raise ValueError('launcher/profile changed while archiving protocol')
                archive.writestr(name,content)
        if args.phase == 'independent':
            await replay(Path(args.smoke),output/'smoke-replay')
        for phase in phases:
            if contract(root) != frozen: raise ValueError('code/profile/protocol changed after freezing')
            folder = output/phase
            result = await LiveSmoke(configs[phase],folder,experiment='E06',
                purpose='technical_smoke' if phase=='smoke' else 'independent_validation',
                max_events=MAX_EVENTS[phase],min_free_bytes=5*1024**3).run()
            if contract(root) != frozen: raise ValueError('code/profile/protocol changed during capture')
            report = verify_smoke(folder,configs['smoke'],audit) if phase=='smoke' else audit(folder)
            (folder/'audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(f'{phase} completed and audited: {result["eventsConsumed"]} observations.',flush=True)
            if phase=='smoke' and args.phase=='auto':
                print('Verifying full paired smoke replay before independent capture.',flush=True)
                await replay(folder,output/'smoke-replay')
                if contract(root) != frozen: raise ValueError('code/profile changed during smoke replay')
                if shutil.disk_usage(output).free < 90*1024**3: raise ValueError('not enough free space for independent phase')
                print('Technical smoke passed; starting frozen independent 12h period.',flush=True)
        (output/'completed.json').write_text(json.dumps(dict(status='capture_completed',phases=list(phases),
            contractSha256=fingerprint(frozen),fullReplayPerformed=False,productionReady=False),indent=2),encoding='utf-8')
    except BaseException as exc:
        (output/'failure.json').write_text(json.dumps(dict(errorType=type(exc).__name__,error=str(exc))),encoding='utf-8')
        raise


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--phase',choices=('auto','smoke','independent'),default='auto')
    parser.add_argument('--smoke',help='Completed smoke directory for a separate independent phase')
    parser.add_argument('--check',action='store_true',help='Local preflight only; no network, no output files')
    args=parser.parse_args()
    try: asyncio.run(run(args))
    except (Exception,KeyboardInterrupt) as exc:
        parser.exit(1,f'E06 capture stopped: {type(exc).__name__}: {exc}\nArtifacts: {args.output}\n')
