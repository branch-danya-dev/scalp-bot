"""Audit complete native populations. Does not start a market or replay run."""
import argparse
from pathlib import Path

from scalp_bot.native_qualification import audit

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('primary','supplemental','session','output'):
        parser.add_argument(name,type=Path)
    parser.add_argument('--freeze',type=Path)
    parser.add_argument('--archive',type=Path)
    args=parser.parse_args()
    if bool(args.freeze) != bool(args.archive): parser.error('--freeze and --archive must be supplied together')
    report=audit(args.primary,args.supplemental,args.session,args.output,
        source_root=Path(__file__).resolve().parents[1],freeze=args.freeze,archive=args.archive)
    print(report['controlledGate']+': '+'; '.join(report['blockers']))
    raise SystemExit(2 if report['controlledGate'] != 'MET' else 0)
