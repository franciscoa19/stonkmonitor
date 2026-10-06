# deploy/

## `com.stonkmonitor.backend.plist` — keep the backend alive

The backend used to run as a bare `nohup` process. A macOS update rebooted the
machine on 2026-09-23 and it simply never came back — silently, for ~9 hours,
while a $4k COST condor sat open. Nothing alerted; the outage looked exactly
like a quiet day. Had it happened 24h later the monitor would have missed that
condor's post-print exit entirely.

This LaunchAgent fixes that: `RunAtLoad` starts it at login, `KeepAlive` restarts
it if it dies for any reason, `ThrottleInterval` stops a hot-loop when it dies on
startup (bad .env, port in use). `WorkingDirectory` is load-bearing — config.py
resolves `.env` relative to cwd.

Install:

    cp deploy/com.stonkmonitor.backend.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.stonkmonitor.backend.plist

Check / restart / remove:

    launchctl print     gui/$(id -u)/com.stonkmonitor.backend
    launchctl kickstart -k gui/$(id -u)/com.stonkmonitor.backend
    launchctl bootout   gui/$(id -u)/com.stonkmonitor.backend

**Sleep.** The agent runs uvicorn under `/usr/bin/caffeinate -i`, which holds a
`PreventUserIdleSystemSleep` assertion while the backend is alive. This Mac's
power settings idle-sleep it after one minute (`pmset -g custom` → `sleep 1`,
on battery and on AC), and a sleeping Mac runs nothing: on 2026-10-05 it slept
through 208 of 390 market-hours minutes, and on 10-06 the PEP condor was only
entered because the lid happened to be opened at 09:51. Check with:

    pmset -g assertions | grep -A1 caffeinate     # the assertion is held
    pmset -g log | grep -E " (Sleep|Wake) " | tail # no idle sleeps since

`caffeinate -i` does **not** override a closed lid ("Clamshell Sleep"), and on
battery it drains until the machine shuts down. Keep the lid open (or use an
external display) and stay on AC. For a lid-closed, headless setup the system
setting has to change instead (System Settings → Battery → Options → "Prevent
automatic sleeping on power adapter when the display is off").

**Limit:** a LaunchAgent starts at *login*, not at boot. If the Mac reboots and
nobody logs in, the backend stays down. Covering that needs a LaunchDaemon in
/Library/LaunchDaemons, which requires sudo — do that by hand if the machine is
ever expected to run headless.

## `com.stonkmonitor.frontend.plist` — keep the dashboard alive

Same shape as the backend agent, serving the dashboard on 127.0.0.1:3000. It runs
`next start` against the production build, not `next dev`, so it does **not**
pick up source changes on its own. Build first, and rebuild after edits:

    (cd frontend && npm run build)
    cp deploy/com.stonkmonitor.frontend.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.stonkmonitor.frontend.plist

After a frontend change: `npm run build`, then
`launchctl kickstart -k gui/$(id -u)/com.stonkmonitor.frontend`.

`ProgramArguments` calls node by absolute path (`~/.local/node/bin/node`) because
launchd's PATH doesn't include it. Update that path if node is reinstalled
elsewhere (e.g. Homebrew).

## One database per broker account

`DB_PATH` (in `backend/.env`) selects the SQLite file; blank means
`backend/stonkmonitor.db`, and a relative name is taken from `backend/`. On its
first run an empty account ledger records a hash of the broker account ID. Every later start
checks it, and the backend aborts with `REFUSING TO START` if the keys in `.env`
belong to a different account. If identity cannot be read, startup retries every
10 seconds before serving HTTP/WebSocket routes or starting any trading tasks.
Account verification also guards entries, exits, cancellations, manual requests,
and broker-ledger reconciliation. Check the backend log if startup is waiting;
HTTP health checks become available after verification succeeds.

A populated ledger or restored backup from before account binding was added
must **not** automatically adopt the connected account. Stop the backend and
make a SQLite backup first. Verify the original broker account against its
historical orders/positions in the broker dashboard. Using that original
account's credentials, obtain its fingerprint with the read-only probe from
`backend/` (this mode does not open the database):

```sh
venv/bin/python bind_account.py --show-fingerprint
```

Then explicitly bind that legacy ledger with:

```sh
venv/bin/python bind_account.py --expected-fingerprint 'paper:VERIFIED_FINGERPRINT'
```

Use the independently verified full fingerprint in place of the placeholder
(`live:` for an original live ledger). `DB_PATH` selects the existing ledger.
The tool checks the connected account against the supplied fingerprint before
recording the binding; it cannot replace an existing different binding and
does not send broker orders. Do not derive an override from unfamiliar new
credentials simply to bypass a mismatch. A different account needs a new DB.

To run a different account (for example a live one), give it its own file
(`DB_PATH=live.db`) rather than reusing the paper ledger. The paper database
keeps its own history. Note that `backend/reports/` and `REPORT_GIT_PUSH` are
not per-account: with a live account they would publish its balances and trades
to this repository.

## Account-wide limits

Two settings in `backend/.env` bound the whole account, whatever the per-trade
settings say. Both are fractions of **current** equity and are checked at every
automated entry (condors and flow trades):

    ACCOUNT_MAX_RISK_PCT=0.30       # most the account may have at risk at once
    ACCOUNT_CASH_RESERVE_PCT=0.20   # free cash a new entry must leave (0 = off)

"At risk" is the remaining max loss of every open or pending condor, the unfilled
commitment of flow, dashboard, and externally placed opening orders, and the
current value of any other holding. Filled quantities count in holdings once;
contingent bracket/OCO exits add no opening risk. "Free cash" is the lower of cash and the buying power
that funds the entry. A new entry is made smaller to fit, or skipped; nothing is
ever closed to get back under a limit. If a balance or a position cannot be
read, the entry is skipped.

Unlisted pending condors reserve their full wider-wing collateral, separately
from max loss. Accepted dashboard orders missing from the open-order list are
looked up by their saved broker ID and reserve unfilled commitments locally.
Unresolved manual submissions, unpriceable opening market/stop orders, opening
short orders, and unavailable entry-order evidence defer automated entries.
Manual requests share the entry lock but remain user-directed orders.

Entry-order status is read before positions and balance on both entry paths and
in risk views. Reports and `GET /api/risk/account` do not retire reservations;
periodic reconciliation persists terminal statuses after all snapshots validate.
Deployment adds nullable `manual_order_requests.broker_order_status` automatically
on database connection. Back up the ledger first; existing request identities
and broker order IDs remain intact.

**When the limits cannot be read, entries are skipped.** `GET /api/risk/account`
then answers 503 with the reason, the daily report shows a red "Risk limits:
unreadable" tile, and the log says `IV-exec skip: account limits unavailable (…)`.
Most causes clear by themselves: a manual market order waiting for the open is
counted once it fills, and a dashboard order still being confirmed is re-checked
by the backend every 15 minutes whether or not the dashboard is open.

One does not clear by itself: a dashboard order whose submission timed out and
that the broker never received stays `pending`, because a missing order is not
proof it was never accepted. Look for it in the broker's own order list (the
request's client ID is `sm-manual-…`). Only if it is not there, mark it:

    sqlite3 backend/stonkmonitor.db "UPDATE manual_order_requests SET status='rejected', error='never reached the broker (checked by hand)' WHERE status IN ('submitting','pending');"

If the order does exist at the broker, do nothing: the next 15-minute pass will
pick it up.

The defaults match what `IV_EXEC_RISK_PCT` (10%) x `IV_EXEC_MAX_POSITIONS` (3)
already allowed, so they only bite after a drawdown or withdrawal, or when
something else is using the budget. Lower them for a tighter account-wide limit.
Current use, without touching anything:

    curl -s http://localhost:8000/api/risk/account

The daily report shows the same two figures. Log lines to look for:
`IV-exec skip …: account risk cap reached`, `cash reserve reached`, and
`size reduced to xN by the account risk cap / cash reserve`.

## Flow-entry sizing (flow trading is off by default)

Flow entries floor quantities to the percentage budget and revalidate them
at confirmation using fresh equity, cash, and options/non-marginable buying
power. Orders only shrink from the suggested quantity. Missing buying power or
unresolved manual orders block submission. Unlisted pending flow/condor entries
reserve funds locally; listed broker orders are already included in available
buying power. `AUTO_TRADE_MAX_RISK_USD` is optional and unset by default; an old
explicit value in `.env` remains an intentional ceiling until removed.

## SQLite backup and recovery

The execution database contains ownership, pending order IDs, fill history and
risk state. Reports alone cannot restore those records. From `backend/`, use a
fresh destination name for each snapshot:

    venv/bin/python backup_db.py stonkmonitor.db backups/stonkmonitor-20261006.db

This uses SQLite's online backup API while the source stays open, includes
committed WAL data, and reopens the snapshot to verify integrity and required
execution tables before publishing it. It refuses to overwrite any destination
and sets file permissions to 0600. Copy verified snapshots off the trading
machine; `backend/backups/` is ignored by Git.

Before restoring, stop the backend and preserve the current database and its
WAL/SHM files together. Restore into a separate location and check strategy
ownership, namespace, pending requests and fills before replacing the live DB.
Do not combine a restored database with WAL/SHM files from a different snapshot.
Reconcile the restored ledger with the broker before arming entries. Unknown
option holdings are quarantined from automatic single-leg exits and block new
automated entries until their ownership is resolved. The regression suite tests
a live WAL backup/restore plus the missing-ledger protective-wing case.
