from __future__ import annotations

import argparse
import sys
from collections import Counter

from scalp_bot.research import OfflineStrategyReplay, load_session


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-run current strategy logic over recorded research frames."
    )
    parser.add_argument("session", help="Path to session-*.jsonl")
    parser.add_argument("--symbol", default=None)
    args = parser.parse_args()

    print("Snapshot shadow analysis only: not execution/PnL replay; "
          "does not reproduce the run manifest, clock admission, arbiter or broker.", file=sys.stderr)

    rows = load_session(args.session)
    signals = OfflineStrategyReplay().run_rows(rows, symbol=args.symbol)
    counts = Counter(signal.strategy for signal in signals)
    print(f"shadow signals: {len(signals)}")
    for strategy, count in counts.most_common():
        print(f"{strategy}: {count}")


if __name__ == "__main__":
    main()
