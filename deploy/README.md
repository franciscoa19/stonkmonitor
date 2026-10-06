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
