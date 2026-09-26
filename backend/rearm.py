#!/usr/bin/env python
"""Clear a tripped risk halt. Deliberately a separate manual step: the breaker
exists because a losing streak means the regime moved, and resuming should be a
decision someone makes, not something the bot does for itself."""
import asyncio, sys
from db import Database
from config import get_settings

async def main():
    d = Database(); await d.connect(); s = get_settings()
    st = await d.get_risk_state(s.iv_risk_loss_factor, s.iv_risk_win_factor,
                                s.iv_risk_floor, s.iv_risk_halt_streak)
    if not st["halted"]:
        print(f"Not halted. multiplier={st['multiplier']:.0%} streak={st['loss_streak']}")
    elif "--yes" not in sys.argv:
        print(f"HALTED: {st['halted_reason']} (at {st['halted_at']})")
        print("Re-run with --yes to re-arm (also resets the loss streak).")
    else:
        await d.clear_halt()
        print("Re-armed. Sizing back to 100%, streak reset.")
    await d.close()

asyncio.run(main())
