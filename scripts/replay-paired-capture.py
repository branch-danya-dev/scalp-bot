"""Stream exact output parity for a sealed E01/E06 paper capture, bounded memory."""
import argparse
import asyncio
import json
from pathlib import Path
import runpy
import sys


async def run(directory, output, source_root=None):
    repository = Path(__file__).resolve().parents[1]
    source = Path(source_root or repository).resolve()
    # Keep the collector's import path when using the installed/current source.
    # Adding the editable repository root can duplicate distribution metadata
    # and falsely change the strict runtime fingerprint.
    if source != repository and not any(Path(p).resolve()==source for p in sys.path):
        sys.path.insert(0,str(source))
    from scalp_bot.e01_comparison import E01Comparison
    from scalp_bot.e01_feed import read_header, observations
    from scalp_bot.offline_bootstrap import _RecordedSettings
    from scalp_bot.manifest_validation import fingerprint
    helpers=runpy.run_path(str(Path(__file__).with_name('replay-e01-breakout-window.py')))
    directory,output=Path(directory),Path(output)
    output.mkdir(parents=True,exist_ok=False)
    recorders={}
    def factory(name,clock):
        recorders[name]=helpers['CheckedRecorder'](directory/f'{name}-events.jsonl',clock)
        return recorders[name]
    try:
        expected=json.loads((directory/'result.json').read_text(encoding='utf-8'))
        if (directory/'failure.json').exists() or expected['status']!='comparison_completed':
            raise ValueError('incomplete or failed capture')
        with (directory/'feed.jsonl').open(encoding='utf-8') as feed:
            header=read_header(feed)
            if header['schema']=='e01-feed-v1': raise ValueError('sealed capture required')
            experiment=header.get('experiment','E01')
            if expected['experiment']!=experiment: raise ValueError('experiment mismatch')
            pair=E01Comparison(_RecordedSettings(**header['config'],bybit_api_key='',bybit_api_secret=''),
                experiment=experiment,max_events=expected['eventsConsumed'],recorder_factory=factory)
            pair.input_hash=fingerprint(header)
            for row in observations(feed,header):
                await pair.apply(row)
                if pair.count%50000==0: print(f'Replay {experiment}: {pair.count} inputs',flush=True)
            if pair.count!=expected['eventsConsumed'] or pair.input_hash!=expected['sharedInputHash']:
                raise ValueError('input count/hash mismatch')
            for name,engine in pair.engines.items():
                recorders[name].exhausted()
                original=expected['portfolios'][name]
                if (engine.running or engine.broker.positions or engine.broker.pending_entries or
                    engine.broker.balance!=original['balance'] or
                    helpers['canonical'](pair._trade_ledger(engine))!=helpers['canonical'](original['trades'])):
                    raise ValueError('portfolio differs or unsettled')
            report=dict(status='paired_full_replay_matched',experiment=experiment,inputs=pair.count,inputHash=pair.input_hash,
                comparedOutputEvents={n:r.count for n,r in recorders.items()},
                ignoredFields=['manifestId','run_summary.payload.latencyMetrics'],profitabilityProven=False)
            (output/'result.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            return report
    except BaseException as exc:
        (output/'failure.json').write_text(json.dumps(dict(errorType=type(exc).__name__,error=str(exc))),encoding='utf-8')
        raise
    finally:
        for recorder in recorders.values(): recorder.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory'); parser.add_argument('--output',required=True); parser.add_argument('--source-root')
    args=parser.parse_args()
    print(json.dumps(asyncio.run(run(args.directory,args.output,args.source_root))))
