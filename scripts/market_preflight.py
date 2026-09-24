from __future__ import annotations

import asyncio

from scalp_bot.bybit import BybitError, BybitRestClient
from scalp_bot.config import settings
from scalp_bot.preflight import probe_runtime_market


async def main() -> None:
    client = BybitRestClient(settings)
    try:
        candidates = await client.active_candidates()
        if not candidates:
            raise RuntimeError(
                "Bybit scanner returned zero eligible candidates"
            )
        symbol = candidates[0].symbol
        sample = ", ".join(
            item.symbol
            for item in candidates[:5]
        )
        instrument, fee_schedule = await asyncio.gather(
            client.instrument_info(symbol),
            client.fee_schedule(symbol),
        )
        print(
            "REST preflight passed: "
            f"{client.active_rest_url} · "
            f"{len(candidates)} candidates · top={sample}"
        )
        print(
            "Instrument preflight passed: "
            f"{symbol} · tick={instrument.tick_size} · "
            f"qtyStep={instrument.qty_step} · "
            f"minNotional={instrument.min_notional_value}"
        )
        print(
            "Fee schedule: "
            f"{fee_schedule.source} · "
            f"maker={fee_schedule.maker_fee_rate:.6f} · "
            f"taker={fee_schedule.taker_fee_rate:.6f}"
        )

        health = await probe_runtime_market(
            settings,
            symbol,
        )
        print(
            "Runtime WebSocket preflight passed: "
            f"L{health['fastDepth']} synced · "
            f"L{health['deepDepth']} synced · "
            "publicTrade=yes · kline.1=yes"
        )
    except (BybitError, RuntimeError) as exc:
        print("")
        print("MARKET PREFLIGHT FAILED")
        print(str(exc))
        print("")
        print(
            "The research run was not started because the exact "
            "runtime Bybit market-data/execution prerequisites did "
            "not become ready."
        )
        raise SystemExit(2) from None
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
