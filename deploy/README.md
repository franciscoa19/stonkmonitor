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

**Limit:** a LaunchAgent starts at *login*, not at boot. If the Mac reboots and
nobody logs in, the backend stays down. Covering that needs a LaunchDaemon in
/Library/LaunchDaemons, which requires sudo — do that by hand if the machine is
ever expected to run headless.
