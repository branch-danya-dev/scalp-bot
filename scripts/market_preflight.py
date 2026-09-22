from __future__ import annotations

import asyncio

from scalp_bot.config import settings
from scalp_bot.bybit import BybitRestClient


async def main() -> None:
    client = BybitRestClient(settings)
    try:
        candidates = await client.active_candidates()
        if not candidates:
            raise RuntimeError(
                "Bybit scanner returned zero eligible candidates"
            )
        sample = ", ".join(
            item.symbol
            for item in candidates[:5]
        )
        print(
            "Market preflight passed: "
            f"{len(candidates)} candidates; top={sample}"
        )
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
