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
    print(f"Output: {directory}")
