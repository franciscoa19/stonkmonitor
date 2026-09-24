# Cloud routine prompt — daily check-in delivery

**This is not code and nothing reads it automatically.** It is the text you paste
into the routine's prompt field at **https://claude.ai/code/routines**
(trigger `trig_01Y1WZRHR83C3q2N9McicUGq`, cron `0 14 * * 1-5` = ~10am ET,
after the backend's 08:00 ET git push).

Kept here so the prompt is versioned and reviewable instead of living only in a
chat log. **If you edit the routine in the web UI, update this file to match.**

Why a cloud routine at all: the backend runs on a local Mac that the cloud cannot
reach, so git is the bridge. The backend pushes `backend/reports/*`, this routine
reads them and delivers.

---

Daily StonkMonitor check-in delivery.

The local backend generates its report at 08:00 ET and git-pushes it. This
routine reads that pushed data and delivers it. Do NOT try to reach localhost —
it is not reachable from here. Git is the only bridge.

=== STEP 1: GET THE REPO — DO NOT ASSUME A CHECKOUT EXISTS ===
This scheduled session may start with no repo checked out. Do not wait for one
and do not ask. Acquire it yourself, every run:

  If a stonkmonitor checkout is already present, cd into it and run:
      git pull --ff-only
  Otherwise clone it fresh:
      git clone --depth 1 https://github.com/franciscoa19/stonkmonitor.git
      cd stonkmonitor

The repo is PUBLIC (~400 KB) so no credentials, token, or SSH key is needed —
an anonymous HTTPS clone works. Use --depth 1; history is irrelevant here and a
shallow clone is fast.

If the clone fails, do not stop silently: send the email anyway with
*** WARNING: COULD NOT REACH THE REPO — no data available today. *** as the
first line, and say what the git error was.

=== STEP 2: READ THE DATA ===
From that checkout:
  backend/reports/latest.json    — today's full report payload
  backend/reports/history.jsonl  — one row per day (equity curve)
  backend/reports/trades.csv     — every closed trade, attributed

=== STEP 3: FRESHNESS CHECK — DO THIS FIRST, BEFORE WRITING ANYTHING ===
The backend runs on a Mac that can reboot, crash, or wedge. A silent outage
looks exactly like a quiet trading day, so check explicitly:

a) Compare latest.json "generated" to today's date.
   If it is NOT from today, the FIRST LINE of the email body must be:
   *** WARNING: NO FRESH REPORT TODAY. Last report <generated>. The backend may
   be down. Figures below are stale and open positions may be UNMANAGED. ***

b) Check latest.json "heartbeat".
   If "stale" is true, the FIRST LINE of the email body must be:
   *** WARNING: BACKEND HEARTBEAT STALE — hourly loop last wrote
   <age_minutes> minutes ago (threshold <threshold_minutes>). Figures may be
   stale and open positions may be UNMANAGED. ***

If both are fine, add one quiet line near the bottom:
   Heartbeat OK (equity loop wrote <age_minutes> min ago).

Never omit these checks. A normal-looking email over stale data is the single
worst failure mode of this system.

=== STEP 4: COMPOSE THE EMAIL ===
Send to francisco.esqueda@gmail.com via the Gmail connector.
Subject: StonkMonitor Daily — <YYYY-MM-DD> — equity $<equity> (<total_pnl_pct>%)

*** THE BODY MUST BE PLAIN TEXT. ABSOLUTELY NO HTML. ***
No <div>, no <table>, no <br>, no markdown tables, no inline styles. HTML in the
body renders as raw code in the inbox. Use blank lines, dashes, and plain
indentation only. This has broken before — do not let it regress.

Include, in this order:

1. Any freshness/heartbeat warning from STEP 3.

2. ACCOUNT — from "account":
   equity, total_pnl, total_pnl_pct vs start_equity, cash, days_running.

3. OPEN POSITIONS — from "open_positions" and "iv_condors":
   List each open condor leg. State iv_condors open/pending counts. If a condor
   is open, say which ticker and that its exit is handled post-earnings.

4. EXECUTED CONDORS — from "iv_condors":
   closed, wins, win_rate, total_pnl.
   Then state plainly: this is n=<closed> trades. An iron condor wins ~65-70%
   of the time BY CONSTRUCTION, so win rate alone proves nothing at this size.

5. MEASUREMENT LAYER — from "iv_variants" (a list, one entry per structure):
   For each: variant, n_events, expectancy, win_rate, profit_factor,
   tail_ratio, largest_single_loss, avg_ror_pct.
   Rank by expectancy. Note collapsed_n where > 0 — those events could not tell
   two shapes apart (coarse strike grid) and carry no shape information.
   If the list is empty, say "no resolved measurement events yet."

   Explain tail_ratio in one line: worst 5% of events vs the rest — how many
   good events one bad event erases. It is the number that catches a
   70%-win-rate strategy that still loses money.

6. GATES vs BASELINE — from "iv_gate_comparison" ("gated" and "ungated"):
   n_events, expectancy, profit_factor, tail_ratio for each.
   State the question being answered: do the three scanner gates beat selling
   every near-earnings name indiscriminately? If ungated matches gated, the
   gates are noise.

7. SAMPLE RAIL — from any entry's "events_needed" / "sufficient_sample":
   State how many more resolved events are needed before these numbers mean
   anything (the rail is 100 per arm). Do NOT project a completion date.

8. QUOTE COVERAGE — from "iv_quote_coverage":
   structures_priced / structures_attempted (priced_pct), and dropped_variants.
   One line: priceability is conditional on entry liquidity, not a random draw.

9. PROPOSALS — from "proposals":
   List them verbatim under "PROPOSED (needs your approval)".
   These are suggestions only. Never describe any change as already applied.

10. Dashboard link: https://claude.ai/code/artifact/2eac4200-625d-4669-bed0-dc5abebceb22

=== STEP 5: BACK UP TO GOOGLE DRIVE ===
Upload to the "StonkMonitor Backups" folder via the Google Drive connector:
  https://drive.google.com/drive/folders/1_P_jIun1bE26KydhgX0MJoJVmYJi5918
  trades_<YYYY-MM-DD>.csv    from backend/reports/trades.csv
  history_<YYYY-MM-DD>.jsonl from backend/reports/history.jsonl

=== HARD RULES ===
- DO NOT publish or republish an Artifact. The publish permission prompt cannot
  be answered in an autonomous run and the routine will hang. The email carries
  the dashboard link instead.
- DO NOT place, modify, or cancel any trade. This routine is read-only.
- DO NOT change any config or threshold. Report proposals; a human approves.
- If a step fails, still send the email and say which step failed. A missing
  email is indistinguishable from a healthy quiet day — that is the failure to
  avoid above all.
