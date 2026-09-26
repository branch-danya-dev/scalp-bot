"""Local profile/disk checks only; never starts an engine or opens the network."""
import os
from pathlib import Path
import shutil

from scalp_bot.capture import validate_profile
from scalp_bot.config import settings


if __name__ == '__main__':
    profile = os.environ.get('SCALP_CAPTURE_PROFILE', '')
    spec = validate_profile(settings, profile)
    directory = Path(settings.session_dir).resolve()
    existing = directory
    while not existing.exists():
        existing = existing.parent
    free = shutil.disk_usage(existing).free / 1024**3
    if free < spec['freeGiB']:
        raise SystemExit(f"Need {spec['freeGiB']} GiB free for this capture; available {free:.1f} GiB")
    print(f"Profile {profile}: {spec['seconds'] // 3600}h, trend={spec['trend']}, beta={spec['beta']}")
    print(f"Single PAPER portfolio, full inputs, fixed configured fees; disk free {free:.1f} GiB")
    if 'targetNetReturnFraction' in spec:
        target = settings.start_balance * spec['targetNetReturnFraction']
        print(f"Exam: net profit >= {target:.2f} USDT ({spec['targetNetReturnFraction']:.0%}) after the full 24h")
        print(f"Start balance {settings.start_balance:.2f} USDT; session loss gate enabled={settings.enforce_session_loss_limit}")
    print(f"Balance {settings.start_balance:.2f} USDT; structural risk {settings.risk_fraction:.2%}; "
          f"all-in trade cap {settings.max_trade_all_in_loss_fraction:.2%}; "
          f"portfolio risk {settings.max_total_risk_fraction:.2%}")
    print(f"Exposure caps: position {settings.max_position_leverage:g}x / "
          f"portfolio {settings.max_leverage:g}x; max positions {settings.max_open_positions}")
    print(f"Fees: taker {settings.taker_fee_rate:.3%}, maker {settings.maker_fee_rate:.3%}; "
          f"slippage {settings.slippage_bps:g} bps; hard net-R {settings.absolute_min_net_reward_risk:g}")
    print(f"Gates: min-profit={settings.enforce_min_net_profit_gate}; "
          f"net-R={settings.enforce_net_reward_risk_gate}; "
          f"winner-cost={settings.enforce_winner_cost_share_gate}; "
          f"session-loss={settings.enforce_session_loss_limit}")
    if not settings.enforce_session_loss_limit:
        print("WARNING: session loss stop is OFF. PAPER research only; existing risk policy unchanged.")
    if profile == 'current-12h':
        print("Owners: breakout/rejection; density evidence-only; trend/beta disabled.")
        print("No profit-target exam. Duration completion and capture integrity are separate from PnL.")
    print(f"Output: {directory}")
