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
