# StonkMonitor — Maintenance Checklist

Run this whenever we make changes or fixes. Claude follows these steps in order.
Nothing gets pushed until the code is reviewed, vetted for secrets, and verified running.

---

## 0. Before starting
- [ ] Confirm current git state is clean or note what's already in flight (`git status`).
- [ ] Note the ask and which components it touches (backend / frontend / DB / infra).

## 1. Security scan (dependencies + code)
- [ ] Backend CVEs: `cd backend && python -m pip_audit` — patch anything actionable.
- [ ] Frontend CVEs: `cd frontend && npm.cmd audit` — patch non-breaking; flag breaking-only fixes.
- [ ] Update pins in `requirements.txt` / `package.json` for anything upgraded.
- [ ] Scan changed code for injected-secret / unsafe patterns (eval, shell=True, hardcoded creds).

## 2. Package / dependency updates
- [ ] Apply safe upgrades; dry-run first for backend (`pip install --dry-run`).
- [ ] Verify imports still work: `python -c "import main"` (backend), `npm.cmd run build` (frontend).

## 3. Code review — correctness + efficiency + refactor
- [ ] Read the touched modules; look for bugs, race conditions, dead code, N+1 / wasted calls.
- [ ] Note refactor / simplification / efficiency opportunities.
- [ ] Keep changes matching surrounding style and altitude.

## 4. Propose changes → wait for approval
- [ ] Summarize proposed changes (what, why, risk) and **prompt the user**.
- [ ] Do NOT proceed to restart/commit until the user accepts.

## 5. Apply + restart
- [ ] Make the approved edits.
- [ ] Restart backend: kill the `python.exe` on port 8000 → `start_service.bat` watchdog respawns it.
- [ ] Frontend: `start_frontend.bat` watchdog respawns it; rebuild if a production build is used.
- [ ] Verify: backend `GET /api/uw/budget` 200; frontend `http://localhost:3000` 200; scan
      `backend/logs/service.log` tail for ERROR/Traceback and confirm signals are flowing.

## 6. Secret scan (pre-commit)
- [ ] `git status` — confirm no `.env` / `*.pem` staged (both must stay gitignored).
- [ ] `git diff | grep -niE "api[_-]?key|secret|token|password|webhook|BEGIN.*PRIVATE|[0-9a-f]{32}|PK[0-9A-Z]{16}"`
- [ ] Repo-wide check for the real key values across tracked files (belt and suspenders).
- [ ] Confirm `CLAUDE.md` and docs contain placeholders only — never real secrets.

## 7. Commit + push
- [ ] Stage only the intended files (never `git add -A` blindly).
- [ ] Clear commit message (what + why); co-author trailer.
- [ ] `git push origin main`; report the commit hash.

---

## Infrastructure reference
| Piece | Path | Notes |
|-------|------|-------|
| Backend watchdog | `backend/start_service.bat` | Restart loop, logs to `backend/logs/service.log` |
| Backend autostart | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\StonkMonitor.vbs` | Silent launch on login |
| Frontend watchdog | `frontend/start_frontend.bat` | Restart loop, logs to `frontend/logs/frontend.log` |
| Frontend autostart | `%APPDATA%\...\Startup\StonkMonitorFrontend.vbs` | Silent launch on login |
| Credentials | `backend/.env` (+ `backend/kalshi_private.pem`) | Gitignored — never commit |

**Restart pattern (Windows):** `Stop-Process -Id <pid> -Force` on the port owner, wait ~15s,
the watchdog `.bat` respawns it. Find owners with `Get-NetTCPConnection -LocalPort <8000|3000>`.
