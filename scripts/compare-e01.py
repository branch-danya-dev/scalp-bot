"""Compare normalized E01 feed observations, not legacy session JSONL files."""
import argparse
import json
from pathlib import Path
import asyncio

from scalp_bot.e01_comparison import E01Comparison
from scalp_bot.e01_feed import read_header, observations
from scalp_bot.offline_bootstrap import _RecordedSettings
from scalp_bot.manifest_validation import fingerprint


async def run(path, max_events):
    with Path(path).open(encoding='utf-8') as stream:
        header = read_header(stream)
        comparison = E01Comparison(_RecordedSettings(**header['config'],
            bybit_api_key='', bybit_api_secret=''), max_events=max_events, experiment=header.get('experiment','E01'))
        if header['schema'] != 'e01-feed-v1':
            comparison.input_hash = fingerprint(header)
        for observation in observations(stream, header):
            await comparison.apply(observation)
        return comparison.finish()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('feed')
    parser.add_argument('--output', required=True)
    parser.add_argument('--max-events', type=int, default=100_000)
    args = parser.parse_args()
    result = asyncio.run(run(args.feed, args.max_events))
    # Create only a new result; never replace an existing experiment artifact.
    with Path(args.output).open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(f"E01 comparison completed; net difference: {result['netDifference']:.6f}; profitability not established")


if __name__ == '__main__':
    main()
