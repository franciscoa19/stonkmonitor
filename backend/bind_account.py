"""One-time legacy ledger binding, after independently verifying its account.

Stop the backend and back up the ledger first. Pass the fingerprint of the
account that originally owned this ledger, not simply any connected account.
No broker orders, cancellations, or account changes are made.
"""
import argparse
import asyncio

from config import get_settings
from db import AccountMismatch, Database, DatabaseError, resolve_db_path
from trading.alpaca_trader import AlpacaTrader


async def bind_legacy_account(db, trader, expected_fingerprint):
    fingerprint = await asyncio.to_thread(trader.account_fingerprint)
    if not fingerprint:
        raise DatabaseError("Broker identity unavailable; ledger binding was not changed")
    if fingerprint != expected_fingerprint:
        raise AccountMismatch("Connected account does not match the independently verified expected account")
    await db.bind_account(fingerprint, legacy_fingerprint=expected_fingerprint)


async def main(expected_fingerprint=None, *, show_fingerprint=False):
    settings = get_settings()
    trader = AlpacaTrader(settings.alpaca_api_key, settings.alpaca_secret_key,
                          paper=settings.alpaca_paper, options_feed=settings.alpaca_options_feed)
    if show_fingerprint:
        fingerprint = await asyncio.to_thread(trader.account_fingerprint)
        if not fingerprint:
            raise DatabaseError("Broker identity unavailable")
        print(fingerprint)
        return  # A read-only probe never opens or migrates a database.
    path = resolve_db_path(settings.db_path)
    if not path.is_file():
        raise FileNotFoundError(f"Existing ledger required: {path}")
    db = Database(path)
    await db.connect()
    try:
        await bind_legacy_account(db, trader, expected_fingerprint)
        print(f"Verified account binding recorded for {path}")
    finally:
        await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--show-fingerprint", action="store_true",
                      help="Read the connected account fingerprint without opening a database")
    mode.add_argument("--expected-fingerprint",
                        help="Independently verified paper:/live: fingerprint of the ledger's original account")
    args = parser.parse_args()
    asyncio.run(main(args.expected_fingerprint, show_fingerprint=args.show_fingerprint))
