"""Run the read-only E01 paired paper collector for at most one hour."""
import argparse
import asyncio
import json
from pathlib import Path

from scalp_bot.e01_live import LiveSmoke, smoke_settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='configs/e01-smoke.json')
    parser.add_argument('--output', required=True, help='New directory for all run artifacts')
    parser.add_argument('--max-events', type=int, default=5_000_000)
    args = parser.parse_args()
    config = smoke_settings(json.loads(Path(args.profile).read_text(encoding='utf-8')))
    try:
        result = asyncio.run(LiveSmoke(config, args.output, max_events=args.max_events).run())
    except (Exception, KeyboardInterrupt) as exc:
        parser.exit(1, f'E01 smoke invalid: {type(exc).__name__}: {exc}\nArtifacts: {args.output}\n')
    print(f"E01 smoke completed: {result['eventsConsumed']} observations. Artifacts: {args.output}")
    print('Technical capture only; profitability and readiness for a 72-hour run are not established.')


if __name__ == '__main__':
    main()
