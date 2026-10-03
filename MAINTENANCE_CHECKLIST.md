# StonkMonitor — Maintenance Checklist

Run this whenever we make changes or fixes. Claude follows these steps in order.
Nothing gets pushed until the code is reviewed, vetted for secrets, and verified running.

---

## 0. Before starting
- [ ] Confirm current git state is clean or note what's already in flight (`git status`).
- [ ] Note the ask and which components it touches (backend / frontend / DB / infra).

## 1. Security scan (dependencies + code)
- [ ] Backend CVEs: `uvx pip-audit --path backend/venv/lib/python3.13/site-packages` — patch anything actionable.
- [ ] Frontend CVEs: `cd frontend && npm audit` — patch non-breaking; flag breaking-only fixes.
- [ ] New/changed npm deps: every lockfile `resolved` URL is registry.npmjs.org, every entry has `integrity`, and no new `hasInstallScript` packages.
- [ ] Update pins in `requirements.txt` / `package.json` for anything upgraded.
- [ ] Scan changed code for injected-secret / unsafe patterns (eval, shell=True, hardcoded creds,
      base64/encoded blobs, new external hosts, reads of ~/.ssh / keychain, hidden unicode).
      This includes `deploy/*-prompt.md` and `CLAUDE.md` — Claude acts on those.

## 2. Package / dependency updates
- [ ] Apply safe upgrades; dry-run first for backend (`pip install --dry-run`).
- [ ] Verify imports still work: `cd backend && venv/bin/python -c "import main"`, `cd frontend && npm run build`.

## 3. Code review — correctness + efficiency + refactor
- [ ] Read the touched modules; look for bugs, race conditions, dead code, N+1 / wasted calls.
- [ ] Note refactor / simplification / efficiency opportunities.
- [ ] Keep changes matching surrounding style and altitude.
- [ ] For earnings-timing changes, run the backend tests with both `TZ=UTC` and
      `TZ=America/New_York`. Earnings eligibility must use the code's ET clock,
      not depend on the host timezone.
- [ ] For market-date changes, `rg -n "datetime\\.now\\(\\)" backend --glob '*.py'`
      must return no hits. Use `market_time.et_now()` / `et_today()` rather than
      the host-local clock.

## 4. Propose changes → wait for approval
- [ ] Summarize proposed changes (what, why, risk) and **prompt the user**.
- [ ] Do NOT proceed to restart/commit until the user accepts.

## 5. Apply + restart
- [ ] Make the approved edits.
- [ ] Restart backend: `launchctl kickstart -k gui/$(id -u)/com.stonkmonitor.backend`.
- [ ] Frontend: `cd frontend && npm run build`, then `launchctl kickstart -k gui/$(id -u)/com.stonkmonitor.frontend`
      (it serves the production build — no hot reload).
- [ ] Verify: backend `GET /health` 200; frontend `http://localhost:3000` 200 with no console errors;
      scan `backend/logs/backend.log` tail for ERROR/Traceback.

## 6. Secret scan (pre-commit)
- [ ] `git status` — confirm no `.env` / `*.pem` staged (both must stay gitignored).
- [ ] `git diff | grep -niE "api[_-]?key|secret|token|password|webhook|BEGIN.*PRIVATE|[0-9a-f]{32}|PK[0-9A-Z]{16}"`
- [ ] Repo-wide check for the real key values across tracked files (belt and suspenders).
- [ ] Confirm `CLAUDE.md` and docs contain placeholders only — never real secrets.

## 7. Commit + push
- [ ] Anything that pushes counts — including turning on `REPORT_GIT_PUSH`, which lets the
      backend push on its own (it did within minutes on 2026-10-02).
- [ ] Stage only the intended files (never `git add -A` blindly).
- [ ] Clear commit message (what + why); co-author trailer.
- [ ] `git push origin main`; report the commit hash.

---

## Infrastructure reference (macOS)
| Piece | Path | Notes |
|-------|------|-------|
| Backend service | `~/Library/LaunchAgents/com.stonkmonitor.backend.plist` (source: `deploy/`) | launchd KeepAlive, logs to `backend/logs/backend.log` |
| Frontend service | `~/Library/LaunchAgents/com.stonkmonitor.frontend.plist` (source: `deploy/`) | `next start` on the production build, logs to `frontend/logs/frontend.log` |
| Credentials | `backend/.env` (+ the Kalshi `.pem`, kept outside the repo) | Gitignored — never commit |
| GitHub auth | `gh` (`~/.local/gh`), token in the macOS keychain | git's credential helper calls gh by absolute path, so launchd pushes work |

**Restart pattern:** `launchctl kickstart -k gui/$(id -u)/com.stonkmonitor.<backend|frontend>`.
Status: `launchctl print gui/$(id -u)/com.stonkmonitor.backend`. Port owners: `lsof -nP -iTCP:8000 -sTCP:LISTEN`.
