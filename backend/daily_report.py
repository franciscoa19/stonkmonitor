"""
daily_report.py — the paper-trading eval loop's daily check-in.

`build_report_data(db, trader)` reads the paper account, equity curve, and the
attributed trade ledger and computes the metrics that answer the project's core
questions: are we trading too much / too little, which setups & timelines work,
and how is the account doing vs its $50k start. `render_html(data)` turns that
into the dashboard artifact. Both are pure-ish and reused by the scheduler.

Proposals are rule-based seeds under PROPOSE-AND-APPROVE: the report suggests,
the human approves. Nothing here changes a parameter.
"""
from __future__ import annotations
from datetime import datetime, timezone
from statistics import mean
import html as _html


async def build_report_data(db, trader, thresholds: dict | None = None) -> dict:
    thresholds = thresholds or {}
    now = datetime.now(timezone.utc)

    # ── Account ──────────────────────────────────────────────────────────
    try:
        acct = trader.get_account()
    except Exception as e:
        acct = {"error": str(e)}
    equity = float(acct.get("equity", 0) or 0)
    cash = float(acct.get("cash", 0) or 0)
    buying_power = float(acct.get("buying_power", 0) or 0)
    try:
        positions = trader.get_positions() or []
    except Exception:
        positions = []

    # ── Equity curve ─────────────────────────────────────────────────────
    curve = await db.get_daily_equity(90)
    # Start = the earliest day's IMMUTABLE open (open_equity), not its latest
    # equity (which the hourly upsert moves), so day-1 P&L isn't zeroed out.
    if curve:
        start_equity = float(curve[0].get("open_equity") or curve[0]["equity"])
    else:
        start_equity = equity or 50000.0
    total_pnl = equity - start_equity
    total_pnl_pct = (total_pnl / start_equity * 100.0) if start_equity else 0.0
    days_running = len(curve)

    # ── Closed trades (realized) ─────────────────────────────────────────
    closed = await db._query(
        "SELECT * FROM trade_performance WHERE realized_pnl IS NOT NULL ORDER BY updated_at DESC"
    )
    n = len(closed)
    wins = [t for t in closed if (t.get("realized_pnl") or 0) > 0]
    losses = [t for t in closed if (t.get("realized_pnl") or 0) < 0]
    gross_win = sum(float(t["realized_pnl"]) for t in wins)
    gross_loss = abs(sum(float(t["realized_pnl"]) for t in losses))
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss else (gross_win and 999.0 or 0.0)
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0
    realized_total = sum(float(t["realized_pnl"]) for t in closed)
    holds = [float(t["hold_minutes"]) for t in closed if t.get("hold_minutes") is not None]
    avg_hold_min = mean(holds) if holds else None

    # ── Trade frequency ("too much / too little") ────────────────────────
    freq = await db._query(
        "SELECT COUNT(*) n FROM trade_performance WHERE side='buy' AND submitted_at >= ?",
        (_iso_days_ago(7),),
    )
    trades_7d = int(freq[0]["n"]) if freq else 0
    trade_days = max(1, min(days_running, 7))
    trades_per_day = trades_7d / trade_days

    # ── Attribution: by strategy ─────────────────────────────────────────
    by_strategy = await db._query(
        """SELECT COALESCE(NULLIF(strategy,''),'(untagged)') strategy,
                  COUNT(*) n,
                  SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) wins,
                  ROUND(SUM(realized_pnl),2) pnl,
                  ROUND(AVG(realized_pnl),2) avg_pnl,
                  ROUND(AVG(hold_minutes),1) avg_hold
           FROM trade_performance WHERE realized_pnl IS NOT NULL
           GROUP BY 1 ORDER BY pnl DESC"""
    )
    # ── IV/RV edge validation (hypothetical short-straddle hit-rate) ─────
    try:
        iv_rv = await db.get_iv_eval_summary()
    except Exception:
        iv_rv = {"resolved": 0, "wins": 0, "win_rate": 0.0, "avg_edge_pct": 0.0, "open": 0}
    try:
        iv_condors = await db.get_condor_summary()
    except Exception:
        iv_condors = {"closed": 0, "wins": 0, "win_rate": 0.0,
                      "total_pnl": 0.0, "open": 0, "pending": 0}
    try:
        iv_variants = await db.get_variant_summary()
    except Exception:
        iv_variants = []
    try:
        iv_gate_cmp = await db.get_gate_comparison()
    except Exception:
        iv_gate_cmp = {}
    try:
        from config import get_settings as _gs
        heartbeat = await db.get_heartbeat(_gs().heartbeat_stale_minutes)
    except Exception as e:
        heartbeat = {"last_seen": None, "age_minutes": None, "stale": True,
                     "threshold_minutes": 180, "note": f"heartbeat unavailable: {e}"}
    try:
        from config import get_settings as _gs2
        _s = _gs2()
        risk_state = await db.get_risk_state(_s.iv_risk_loss_factor, _s.iv_risk_win_factor,
                                             _s.iv_risk_floor, _s.iv_risk_halt_streak)
    except Exception as e:
        risk_state = {"multiplier": None, "loss_streak": None, "halted": False,
                      "halted_reason": f"risk state unavailable: {e}"}
    # Watchlist and measurement events have distinct selection rules. Keep
    # their implied-vs-realized observations separate; pooling them would turn
    # a change in cohort mix into a misleading claim about structural edge.
    implied_vs_realized = {}
    for source in ("watchlist", "measurement"):
        try:
            implied_vs_realized[source] = await db.get_implied_vs_realized(source=source)
        except Exception:
            implied_vs_realized[source] = {"n_events": 0, "pct_exceeding_implied": None,
                                           "avg_implied_pct": None, "avg_realized_pct": None,
                                           "avg_edge_pct": None, "events": []}
    try:
        iv_quote_coverage = await db.get_variant_quote_coverage()
    except Exception:
        iv_quote_coverage = {"events": 0, "structures_attempted": 0,
                             "structures_priced": 0, "priced_pct": None,
                             "dropped_variants": {}}

    # ── Attribution: by entry hour (ET) ──────────────────────────────────
    by_hour = await db._query(
        """SELECT entry_hour_et h, COUNT(*) n, ROUND(SUM(realized_pnl),2) pnl
           FROM trade_performance WHERE realized_pnl IS NOT NULL AND entry_hour_et IS NOT NULL
           GROUP BY h ORDER BY h"""
    )

    # ── Recent closed trades (for the log) ───────────────────────────────
    recent = [{
        "ticker": t.get("ticker"), "symbol": t.get("symbol"),
        "strategy": t.get("strategy") or "", "trade_type": t.get("trade_type"),
        "pnl": round(float(t.get("realized_pnl") or 0), 2),
        "pnl_pct": round(float(t.get("realized_pnl_pct") or 0), 1),
        "exit_reason": t.get("exit_reason"),
        "hold_min": t.get("hold_minutes"), "hour": t.get("entry_hour_et"),
    } for t in closed[:15]]

    open_pos = [{
        "symbol": p.get("symbol"), "qty": p.get("qty"),
        "pnl": round(float(p.get("unrealized_pl", 0) or 0), 2) if isinstance(p, dict) else None,
    } for p in positions] if positions and isinstance(positions[0], dict) else []

    # ── Proposals (propose-and-approve; rule-based seeds) ────────────────
    proposals = []
    score_thr = thresholds.get("score", 9.0)
    if n == 0 and days_running >= 3:
        proposals.append(
            f"No closed trades in {days_running} days. Consider lowering "
            f"AUTO_TRADE_SCORE_THRESHOLD ({score_thr}→8.5) to grow the sample, "
            f"or widening the watchlist so IV/earnings setups fire.")
    if trades_per_day > 4:
        proposals.append(
            f"~{trades_per_day:.1f} entries/day — trading heavy. Consider raising "
            f"thresholds or tightening filters to focus on higher-conviction setups.")
    for s in by_strategy:
        if s["n"] >= 8:
            wr = s["wins"] / s["n"] * 100
            if wr < 35:
                proposals.append(
                    f"Strategy '{s['strategy']}' win rate {wr:.0f}% over {s['n']} trades "
                    f"(P&L ${s['pnl']:.0f}). Consider disabling or tightening it.")
            elif wr >= 55 and (s["pnl"] or 0) > 0:
                proposals.append(
                    f"Strategy '{s['strategy']}' looks strong: {wr:.0f}% WR over {s['n']} "
                    f"(P&L ${s['pnl']:.0f}). Consider a modest size increase.")
    if equity and start_equity and equity < start_equity * 0.95:
        proposals.append(
            f"Drawdown: equity ${equity:,.0f} is {(equity/start_equity-1)*100:.1f}% below "
            f"the ${start_equity:,.0f} start. Review risk sizing before adding strategies.")
    if not proposals:
        proposals.append("No changes proposed — baseline accruing. Keep collecting.")

    # too much / too little verdict
    if n == 0 and trades_7d == 0:
        activity = ("QUIET", "No trades yet — the conservative thresholds are holding fire. "
                    "Expected early on; watch that we're not too selective.")
    elif trades_per_day > 4:
        activity = ("HEAVY", f"~{trades_per_day:.1f} entries/day — on the high side.")
    else:
        activity = ("MEASURED", f"~{trades_per_day:.1f} entries/day.")

    return {
        "generated": now.isoformat(),
        "account": {"equity": equity, "cash": cash, "buying_power": buying_power,
                    "start_equity": start_equity, "total_pnl": total_pnl,
                    "total_pnl_pct": total_pnl_pct, "open_positions": len(positions),
                    "days_running": days_running, "error": acct.get("error")},
        "metrics": {"closed_trades": n, "wins": len(wins), "losses": len(losses),
                    "win_rate": win_rate, "profit_factor": profit_factor,
                    "avg_win": avg_win, "avg_loss": avg_loss,
                    "realized_total": realized_total, "avg_hold_min": avg_hold_min,
                    "trades_7d": trades_7d, "trades_per_day": trades_per_day},
        "activity": {"tag": activity[0], "note": activity[1]},
        "equity_curve": [{"date": r["date"], "equity": float(r["equity"])} for r in curve],
        "iv_rv": iv_rv,
        "iv_condors": iv_condors,
        "iv_variants": iv_variants,
        "iv_gate_comparison": iv_gate_cmp,
        "iv_quote_coverage": iv_quote_coverage,
        "implied_vs_realized": implied_vs_realized,
        "risk_state": risk_state,
        "heartbeat": heartbeat,
        "by_strategy": [dict(r) for r in by_strategy],
        "by_hour": [dict(r) for r in by_hour],
        "recent": recent,
        "open_positions": open_pos,
        "proposals": proposals,
    }


def _iso_days_ago(days: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


async def export_history(db, reports_dir) -> dict:
    """Write the durable, diffable historical record for git/backup:
      - history.jsonl : one row per day (the equity curve + open/cash/positions)
      - trades.csv    : every closed (realized) trade, fully attributed
    These are small, versionable, and let the account be reconstructed off-machine.
    Returns the file paths written.
    """
    import json as _json
    import csv as _csv
    from pathlib import Path
    rd = Path(reports_dir)
    rd.mkdir(exist_ok=True)

    curve = await db.get_daily_equity(3650)
    iv_rv = await db.get_iv_eval_summary()
    hist_path = rd / "history.jsonl"
    existing: dict[str, dict] = {}
    if hist_path.exists():
        for line in hist_path.read_text().splitlines():
            try:
                row = _json.loads(line)
                if row.get("date"):
                    existing[row["date"]] = row
            except (TypeError, ValueError):
                continue
    from market_time import et_today
    today = et_today().isoformat()
    with open(hist_path, "w") as f:
        for r in curve:
            row = {k: r[k] for k in r.keys()}
            # Preserve earlier daily snapshots while recording today's IV/RV
            # state. The export is rewritten daily from the equity curve.
            if existing.get(row["date"], {}).get("iv_rv") is not None:
                row["iv_rv"] = existing[row["date"]]["iv_rv"]
            if row["date"] == today:
                row["iv_rv"] = iv_rv
            f.write(_json.dumps(row, default=str) + "\n")

    closed = await db._query(
        "SELECT * FROM trade_performance WHERE realized_pnl IS NOT NULL ORDER BY updated_at")
    cols = ["ticker", "symbol", "strategy", "trade_type", "side", "qty",
            "filled_avg_price", "exit_price", "realized_pnl", "realized_pnl_pct",
            "exit_reason", "entry_hour_et", "hold_minutes", "submitted_at", "updated_at"]
    trades_path = rd / "trades.csv"
    with open(trades_path, "w", newline="") as f:
        w = _csv.writer(f)
        w.writerow(cols)
        for t in closed:
            w.writerow([t.get(c) for c in cols])

    return {"history": str(hist_path), "trades": str(trades_path), "days": len(curve), "trades_n": len(closed)}


async def build_watchlist_review(db, watchlist, lookback_days: int = 14,
                                 add_threshold: int = 8) -> list[str]:
    """Weekly, data-driven watchlist proposals (propose-and-approve — never auto).

    ADD:    tickers generating lots of signals that aren't on the watchlist
            (you're clearly getting signal there → scan it for IV/earnings too).
    REMOVE: watchlist names with zero signals AND zero trades over the window
            (dead weight / noise / budget).
    """
    props: list[str] = []
    wl = {t.upper() for t in watchlist}
    since = _iso_days_ago(lookback_days)

    top = await db._query(
        "SELECT ticker, COUNT(*) n FROM signals WHERE created_at >= ? "
        "GROUP BY ticker ORDER BY n DESC LIMIT 30", (since,))
    adds = [r for r in top if (r["ticker"] or "").upper() not in wl and r["n"] >= add_threshold]
    for r in adds[:5]:
        props.append(f"Watchlist ADD: {r['ticker']} — {r['n']} signals in {lookback_days}d, not watchlisted.")

    sig_counts = {(r["ticker"] or "").upper(): r["n"] for r in await db._query(
        "SELECT ticker, COUNT(*) n FROM signals WHERE created_at >= ? GROUP BY ticker", (since,))}
    trade_counts = {(r["ticker"] or "").upper(): r["n"] for r in await db._query(
        "SELECT ticker, COUNT(*) n FROM trade_performance WHERE created_at >= ? GROUP BY ticker", (since,))}
    # Only flag removals for names that have had a FULL window on the watchlist —
    # a freshly-added ticker hasn't had a fair chance to produce signals yet.
    added = {(r["ticker"] or "").upper(): (r.get("added_at") or "")
             for r in await db._query("SELECT ticker, added_at FROM watchlist")}
    dead = [t for t in sorted(wl)
            if sig_counts.get(t, 0) == 0 and trade_counts.get(t, 0) == 0
            and added.get(t, "") < since]     # added before the window started
    for t in dead[:8]:
        props.append(f"Watchlist REMOVE?: {t} — 0 signals & 0 trades in {lookback_days}d.")

    if not props:
        props.append(f"Watchlist review: no changes — all {len(wl)} names active; no strong un-watchlisted names.")
    return props


# ══════════════════════════════════════════════════════════════════════════
#  HTML render
# ══════════════════════════════════════════════════════════════════════════
def render_html(d: dict) -> str:
    a, m = d["account"], d["metrics"]
    act = d["activity"]
    iv = d.get("iv_rv", {"resolved": 0, "wins": 0, "win_rate": 0.0, "avg_edge_pct": 0.0, "open": 0})
    condors = d.get("iv_condors", {"closed": 0, "wins": 0, "win_rate": 0.0,
                                     "total_pnl": 0.0, "open": 0, "pending": 0})
    variants = d.get("iv_variants", []) or []
    gate_cmp = d.get("iv_gate_comparison", {}) or {}
    rs = d.get("risk_state", {}) or {}
    if rs.get("halted"):
        _risk_banner = (
            "<div style=\"background:#7f1d1d;color:#fff;padding:12px 14px;border-radius:6px;"
            "margin-bottom:16px;font-size:14px\"><b>&#9940; TRADING HALTED.</b> "
            f"{_html.escape(str(rs.get('halted_reason') or 'circuit breaker tripped'))}. "
            "No new condors will open until this is cleared deliberately. Open positions "
            "are still managed to their exits.</div>")
    elif (rs.get("multiplier") or 1.0) < 1.0:
        _risk_banner = (
            "<div style=\"background:#78350f;color:#fff;padding:10px 14px;border-radius:6px;"
            "margin-bottom:16px;font-size:13px\"><b>Risk throttled to "
            f"{(rs.get('multiplier') or 0)*100:.0f}% of normal size</b> after "
            f"{rs.get('loss_streak')} consecutive loss(es). Wins ratchet it back up; "
            f"{rs.get('halt_streak')} in a row halts entirely.</div>")
    else:
        _risk_banner = ""
    hb = d.get("heartbeat", {}) or {}
    if hb.get("stale"):
        age = hb.get("age_minutes")
        age_txt = (f"{age/60:.1f} hours ago" if isinstance(age, (int, float))
                   else "never")
        _hb_banner = (
            "<div style=\"background:#7f1d1d;color:#fff;padding:12px 14px;border-radius:6px;"
            "margin-bottom:16px;font-size:14px\"><b>&#9888; BACKEND MAY BE DOWN.</b> "
            f"The hourly equity loop last wrote <b>{_html.escape(age_txt)}</b> "
            f"(threshold {hb.get('threshold_minutes')} min). "
            "Numbers below are STALE and open positions may be unmanaged. "
            "Check the backend before trusting this report.</div>")
    else:
        age = hb.get("age_minutes")
        _hb_banner = ("<div class=\"mut\" style=\"font-size:11px;margin-bottom:10px\">"
                      f"heartbeat OK &middot; equity loop wrote {age:.0f} min ago</div>"
                      if isinstance(age, (int, float)) else "")
    edge_by_source = d.get("implied_vs_realized", {}) or {}
    # Kept only so an older saved report can still render after deployment.
    legacy_mixed_edge = "n_events" in edge_by_source
    if legacy_mixed_edge:
        edge_by_source = {"legacy mixed cohort": edge_by_source}

    def _edge_card(source: str, ivr: dict) -> str:
        source_label = _html.escape(source.replace("_", " ").title())
        cohort_badge = "legacy mixed cohort" if legacy_mixed_edge else "separate cohort"
        n = ivr.get("n_events") or 0
        if not n:
            return (f"<div class=\"card\"><h2>Implied vs realized — {source_label} "
                    f"<span class=\"pill mut\" style=\"font-size:11px\">{cohort_badge}</span></h2>"
                    "<div class=\"mut\" style=\"font-size:12px\">No resolved events yet.</div></div>")
        pct = ivr.get("pct_exceeding_implied")
        edge = ivr.get("avg_edge_pct") or 0
        rows = "".join(
            f"<tr><td style='padding:2px 12px 2px 0'>{_html.escape(str(e['ticker']))}</td>"
            f"<td style='text-align:right'>{e['implied_pct']}%</td>"
            f"<td style='text-align:right'>{e['realized_pct']}%</td>"
            f"<td style='text-align:right' class=\"{'down' if e['exceeded'] else 'up'}\">"
            f"{e['edge_pct']:+.2f}%</td>"
            f"<td style='text-align:right'>{'EXCEEDED' if e['exceeded'] else 'inside'}</td></tr>"
            for e in ivr.get("events", []))
        return (
            f"<div class=\"card\"><h2>Implied vs realized — {source_label} "
            f"<span class=\"pill mut\" style=\"font-size:11px\">{cohort_badge}</span></h2>"
            "<div style=\"display:flex;gap:26px;flex-wrap:wrap;font-family:var(--mono)\">"
            "<div><div class=\"mut\" style=\"font-size:10.5px;text-transform:uppercase\">Exceeded implied</div>"
            f"<div style=\"font-size:22px;font-weight:700\" class=\"{'down' if (pct or 0) > 25 else 'up'}\">{pct}%</div></div>"
            "<div><div class=\"mut\" style=\"font-size:10.5px;text-transform:uppercase\">Avg implied</div>"
            f"<div style=\"font-size:22px;font-weight:700\">{ivr.get('avg_implied_pct')}%</div></div>"
            "<div><div class=\"mut\" style=\"font-size:10.5px;text-transform:uppercase\">Avg realized</div>"
            f"<div style=\"font-size:22px;font-weight:700\">{ivr.get('avg_realized_pct')}%</div></div>"
            "<div><div class=\"mut\" style=\"font-size:10.5px;text-transform:uppercase\">Avg edge</div>"
            f"<div style=\"font-size:22px;font-weight:700\" class=\"{'up' if edge > 0 else 'down'}\">{edge:+.2f}pp</div></div>"
            f"<div><div class=\"mut\" style=\"font-size:10.5px;text-transform:uppercase\">Events</div>"
            f"<div style=\"font-size:22px;font-weight:700\">{n}</div></div></div>"
            "<table style=\"width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12.5px;margin-top:12px\">"
            "<tr class=\"mut\" style=\"font-size:10.5px;text-transform:uppercase;text-align:right\">"
            "<th style=\"text-align:left\">Event</th><th>Implied</th><th>Realized</th><th>Edge</th><th></th></tr>"
            f"{rows}</table>"
            "<div class=\"mut\" style=\"font-size:12px;margin-top:10px\">How often the stock moved MORE "
            "than the options priced in. This is the premium seller's structural question for this cohort, and it converges "
            "far faster than counting max-loss events, because it is observable on every print rather than "
            "only the rare disasters. Sustained above ~25% means the premium is not rich enough and no "
            "choice of structure fixes it.</div></div>")
    edge_sources = ("legacy mixed cohort",) if legacy_mixed_edge else ("watchlist", "measurement")
    _edge_html = "".join(
        _edge_card(source, edge_by_source.get(source) or {})
        for source in edge_sources
    )
    quote_coverage = d.get("iv_quote_coverage", {}) or {}
    gated, ungated = gate_cmp.get("gated", {}), gate_cmp.get("ungated", {})

    def _sample_rail() -> str:
        """Make the two-arm evidence requirement visible even before results."""
        g_need = gated.get("events_needed", 100)
        u_need = ungated.get("events_needed", 100)
        return (
            "<div style=\"margin-top:10px;padding:8px 10px;border-left:3px solid #c88;"
            "font-size:12px\"><b>Forward-test sample rail.</b> "
            f"Gated: {gated.get('n_events', 0)} resolved / {g_need} more needed; "
            f"ungated: {ungated.get('n_events', 0)} resolved / {u_need} more needed. "
            "Both arms need 100 events; no completion date is projected until a real "
            "collection rate exists.</div>")

    def _quote_coverage() -> str:
        attempted = int(quote_coverage.get("structures_attempted") or 0)
        priced = int(quote_coverage.get("structures_priced") or 0)
        if not attempted:
            return (
                "<div class=\"mut\" style=\"font-size:12px;margin-top:10px\">"
                "<b>Quote coverage.</b> No near-earnings structures attempted yet; "
                "entry-liquidity selection is not measured yet.</div>")
        pct = quote_coverage.get("priced_pct")
        pct_text = f" ({pct:.1f}%)" if isinstance(pct, (int, float)) else ""
        dropped = quote_coverage.get("dropped_variants") or {}
        dropped_text = ""
        if dropped:
            names = ", ".join(
                f"{_html.escape(str(name))} ({count})"
                for name, count in sorted(dropped.items()))
            dropped_text = f" Dropped in at least one event: {names}."
        return (
            "<div class=\"mut\" style=\"font-size:12px;margin-top:10px\">"
            f"<b>Quote coverage.</b> {priced}/{attempted} structures priceable{pct_text}."
            f"{dropped_text}</div>")

    if variants:
        def _f(x, suffix=""):
            return "—" if x is None else f"{x}{suffix}"
        _vrows = "".join(
            f"<tr><td style='padding:3px 12px 3px 0'>{_html.escape(str(v['variant']))}</td>"
            f"<td style='text-align:right'>{v['n_events']}</td>"
            f"<td style='text-align:right'>{_f(v.get('win_rate'), '%')}</td>"
            f"<td style='text-align:right' class=\"{'up' if (v.get('expectancy') or 0)>=0 else 'down'}\">{_money(v.get('expectancy') or 0)}</td>"
            f"<td style='text-align:right'>{_f(v.get('profit_factor'))}</td>"
            f"<td style='text-align:right' class=\"{'down' if (v.get('tail_ratio') or 0) < -3 else ''}\">{_f(v.get('tail_ratio'))}</td>"
            f"<td style='text-align:right' class=\"down\">{_money(v.get('largest_single_loss') or 0)}</td>"
            f"<td style='text-align:right'>{_f(v.get('avg_ror_pct'), '%')}</td></tr>"
            for v in variants)
        _n = max((v["n_events"] for v in variants), default=0)
        _need = max((v.get("events_needed") or 0 for v in variants), default=0)
        _warn = ("" if all(v.get("sufficient_sample") for v in variants) else
                 "<div style=\"margin-top:10px;padding:8px 10px;border-left:3px solid #c88;"
                 "font-size:12px\"><b>Not yet evidence.</b> "
                 f"{_n} resolved event(s); ~{_need} more needed before these numbers "
                 "mean anything. An iron condor wins ~65–70% of the time by construction, "
                 "so win rate is noise until the tail has shown up.</div>")
        _tail_basis = "; ".join(
            f"{_html.escape(str(v['variant']))}: worst {v.get('tail_events', 0)}/"
            f"{v['n_events']} event(s) ({v.get('tail_pct_effective') or 0}%)"
            for v in variants)
        _cmp_html = ""
        if gate_cmp.get("gated") or gate_cmp.get("ungated"):
            g, u = gated, ungated
            _cmp_html = (
                "<div style=\"margin-top:14px\"><div class=\"mut\" style=\"font-size:10.5px;"
                "text-transform:uppercase\">Do the gates earn their keep?</div>"
                "<table style=\"width:100%;border-collapse:collapse;font-family:var(--mono);font-size:13px;margin-top:4px\">"
                "<tr class=\"mut\" style=\"font-size:10.5px;text-transform:uppercase;text-align:right\">"
                "<th style=\"text-align:left\">Population</th><th>N</th><th>Expectancy</th>"
                "<th>Profit factor</th><th>Tail ratio</th></tr>"
                f"<tr><td>gated (passed all 3)</td><td style='text-align:right'>{g.get('n_events',0)}</td>"
                f"<td style='text-align:right'>{_money(g.get('expectancy') or 0)}</td>"
                f"<td style='text-align:right'>{_f(g.get('profit_factor'))}</td>"
                f"<td style='text-align:right'>{_f(g.get('tail_ratio'))}</td></tr>"
                f"<tr><td>ungated (sell everything)</td><td style='text-align:right'>{u.get('n_events',0)}</td>"
                f"<td style='text-align:right'>{_money(u.get('expectancy') or 0)}</td>"
                f"<td style='text-align:right'>{_f(u.get('profit_factor'))}</td>"
                f"<td style='text-align:right'>{_f(u.get('tail_ratio'))}</td></tr></table>"
                "<div class=\"mut\" style=\"font-size:12px;margin-top:6px\">If selling "
                "indiscriminately matches the filtered set, the three gates are noise.</div>"
                f"{_sample_rail()}</div>")
        else:
            # A transient comparison-query failure must not hide the evidence
            # requirement from the morning report.
            _cmp_html = _sample_rail()
        _variant_card = (
            "<div class=\"card\"><h2>Strategy-variant comparison "
            "<span class=\"pill mut\" style=\"font-size:11px\">hypothetical · no execution</span></h2>"
            "<table style=\"width:100%;border-collapse:collapse;font-family:var(--mono);font-size:13px\">"
            "<tr class=\"mut\" style=\"font-size:10.5px;text-transform:uppercase;text-align:right\">"
            "<th style=\"text-align:left\">Variant</th><th>N</th><th>Win%</th>"
            "<th>Expectancy</th><th>PF</th><th>Tail</th><th>Worst</th><th>RoR</th></tr>"
            f"{_vrows}</table>"
            "<div class=\"mut\" style=\"font-size:12px;margin-top:10px\">Same events, "
            "different structures, priced at a conservative fill (short=bid, long=ask) net "
            "of commission and settled on the underlying's close at expiry. Per 1 spread. "
            "<b>Tail</b> = worst 5% vs the rest — how many good events one bad one erases; "
            "it is the number that catches a 70%-win-rate strategy that still loses money. "
            f"Effective tail: {_tail_basis}."
            f"</div>{_quote_coverage()}{_warn}{_cmp_html}</div>")
    else:
        _variant_card = (
            "<div class=\"card\"><h2>Strategy-variant comparison "
            "<span class=\"pill mut\" style=\"font-size:11px\">hypothetical · no execution</span></h2>"
            "<div class=\"mut\" style=\"font-size:12px\">No resolved evaluations under the "
            "current conservative pricing model yet. Earlier methodology is retained for audit "
            "but is not evidence.</div>"
            f"{_quote_coverage()}{_sample_rail()}</div>")
    e = _html.escape
    pnl_cls = "up" if a["total_pnl"] >= 0 else "down"
    pnl_sign = "+" if a["total_pnl"] >= 0 else ""
    pf = m["profit_factor"]
    pf_disp = "—" if m["closed_trades"] == 0 else (f"{pf:.2f}" if pf < 999 else "∞")
    act_cls = {"QUIET": "mut", "MEASURED": "good", "HEAVY": "warn"}.get(act["tag"], "mut")
    date_label = d["generated"][:10]

    strat_rows = "".join(
        f"<tr><td>{e(str(s['strategy']))}</td><td>{s['n']}</td>"
        f"<td>{(s['wins']/s['n']*100):.0f}%</td>"
        f"<td class='{ 'up' if (s['pnl'] or 0)>=0 else 'down'}'>{_money(s['pnl'])}</td>"
        f"<td>{_money(s['avg_pnl'])}</td>"
        f"<td>{_hold(s['avg_hold'])}</td></tr>"
        for s in d["by_strategy"]) or "<tr><td colspan='6' class='mut'>No closed trades yet — fills will populate this.</td></tr>"

    recent_rows = "".join(
        f"<tr><td>{e(str(r['ticker']))}</td><td class='mut'>{e(str(r['strategy']))}</td>"
        f"<td class='{ 'up' if r['pnl']>=0 else 'down'}'>{_money(r['pnl'])}</td>"
        f"<td class='{ 'up' if r['pnl']>=0 else 'down'}'>{r['pnl_pct']:+.0f}%</td>"
        f"<td class='mut'>{e(str(r['exit_reason'] or ''))}</td>"
        f"<td>{_hold(r['hold_min'])}</td></tr>"
        for r in d["recent"]) or "<tr><td colspan='6' class='mut'>No closed trades yet.</td></tr>"

    proposals = "".join(f"<li>{e(p)}</li>" for p in d["proposals"])
    curve_json = _json_points(d["equity_curve"], a["start_equity"])

    return f"""<title>Paper Trading — Daily Check-in</title>
<style>
  :root{{--bg:#0b0e14;--panel:#141a24;--panel2:#1b2431;--border:#26303f;--text:#d7deea;
    --muted:#7d8a9c;--accent:#38bdf8;--up:#3fb950;--down:#f0663f;--warn:#f0a336;--good:#3fb950;--gold:#e3b341;
    --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;}}
  @media (prefers-color-scheme:light){{:root{{--bg:#eef1f6;--panel:#fff;--panel2:#f3f6fa;--border:#d9e0ea;
    --text:#18202e;--muted:#5a6675;--accent:#0284c7;--up:#1a7f37;--down:#c2410c;--warn:#be6b12;--good:#1a7f37;--gold:#9a6b00;}}}}
  :root[data-theme="dark"]{{--bg:#0b0e14;--panel:#141a24;--panel2:#1b2431;--border:#26303f;--text:#d7deea;--muted:#7d8a9c;--accent:#38bdf8;--up:#3fb950;--down:#f0663f;--warn:#f0a336;--good:#3fb950;--gold:#e3b341;}}
  :root[data-theme="light"]{{--bg:#eef1f6;--panel:#fff;--panel2:#f3f6fa;--border:#d9e0ea;--text:#18202e;--muted:#5a6675;--accent:#0284c7;--up:#1a7f37;--down:#c2410c;--warn:#be6b12;--good:#1a7f37;--gold:#9a6b00;}}
  *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.5}}
  .wrap{{max-width:1040px;margin:0 auto;padding:26px 20px 60px}}
  h1,h2{{margin:0;text-wrap:balance}}
  .eyebrow{{font-family:var(--mono);font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted)}}
  header{{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:14px;padding-bottom:16px;border-bottom:1px solid var(--border)}}
  header h1{{font-size:24px;margin-top:6px}}
  .pill{{display:inline-block;font-family:var(--mono);font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px}}
  .pill.mut{{background:var(--panel2);color:var(--muted)}} .pill.good{{background:color-mix(in srgb,var(--good) 18%,transparent);color:var(--good)}}
  .pill.warn{{background:color-mix(in srgb,var(--warn) 20%,transparent);color:var(--warn)}}
  .kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin:20px 0}}
  .kpi{{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:13px 15px}}
  .kpi .k{{font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--muted)}}
  .kpi .v{{font-family:var(--mono);font-size:24px;font-weight:700;margin-top:5px;line-height:1}}
  .kpi .n{{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:4px}}
  .up{{color:var(--up)}} .down{{color:var(--down)}} .mut{{color:var(--muted)}} .warn{{color:var(--warn)}} .good{{color:var(--good)}}
  .card{{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-bottom:18px}}
  .card h2{{font-size:14px;margin-bottom:12px}}
  #eq{{width:100%;height:auto;display:block}}
  table{{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12.5px}}
  .scroll{{overflow-x:auto}}
  th,td{{text-align:right;padding:7px 9px;border-bottom:1px solid var(--border);white-space:nowrap}}
  th:first-child,td:first-child{{text-align:left}}
  thead th{{color:var(--muted);font-weight:600;font-size:10.5px;letter-spacing:.04em;text-transform:uppercase}}
  tbody tr:last-child td{{border-bottom:0}}
  .prop{{background:var(--panel2);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:10px;padding:14px 18px}}
  .prop h2{{font-size:13px;margin-bottom:8px}} .prop ul{{margin:0;padding-left:18px}} .prop li{{font-size:13.5px;margin:5px 0;max-width:76ch}}
  .note{{font-size:11px;color:var(--muted);border-top:1px solid var(--border);padding-top:14px;margin-top:8px;line-height:1.6}}
</style>
<div class="wrap">
  {_hb_banner}
  {_risk_banner}
  <header>
    <div><div class="eyebrow">StonkMonitor · Paper Eval Loop</div><h1>Daily Check-in — {e(date_label)}</h1></div>
    <div style="text-align:right"><span class="pill {act_cls}">{e(act['tag'])}</span>
      <div class="mut" style="font-family:var(--mono);font-size:11px;margin-top:6px">day {a['days_running']} · paper $50k acct</div></div>
  </header>

  <div class="kpis">
    <div class="kpi"><div class="k">Equity</div><div class="v">${a['equity']:,.0f}</div><div class="n">start ${a['start_equity']:,.0f}</div></div>
    <div class="kpi"><div class="k">Total P&L</div><div class="v {pnl_cls}">{pnl_sign}${a['total_pnl']:,.0f}</div><div class="n {pnl_cls}">{pnl_sign}{a['total_pnl_pct']:.2f}%</div></div>
    <div class="kpi"><div class="k">Closed trades</div><div class="v">{m['closed_trades']}</div><div class="n">{m['wins']}W / {m['losses']}L</div></div>
    <div class="kpi"><div class="k">Win rate</div><div class="v">{(str(round(m['win_rate']))+'%') if m['closed_trades'] else '—'}</div><div class="n">PF {pf_disp}</div></div>
    <div class="kpi"><div class="k">Entries / day</div><div class="v">{m['trades_per_day']:.1f}</div><div class="n">{m['trades_7d']} in 7d</div></div>
    <div class="kpi"><div class="k">Open positions</div><div class="v">{a['open_positions']}</div><div class="n">avg hold {_hold(m['avg_hold_min'])}</div></div>
  </div>

  <div class="card"><h2>Activity read</h2><div style="font-size:14px">{e(act['note'])}</div></div>

  <div class="card">
    <h2>IV/RV edge validation <span class="pill mut" style="font-size:11px">hypothetical short straddle · not yet traded</span></h2>
    <div style="display:flex;gap:26px;flex-wrap:wrap;font-family:var(--mono)">
      <div><div class="mut" style="font-size:10.5px;text-transform:uppercase">Resolved</div><div style="font-size:22px;font-weight:700">{iv['resolved']}</div></div>
      <div><div class="mut" style="font-size:10.5px;text-transform:uppercase">Seller win rate</div><div style="font-size:22px;font-weight:700">{(str(round(iv['win_rate']))+'%') if iv['resolved'] else '—'}</div></div>
      <div><div class="mut" style="font-size:10.5px;text-transform:uppercase">Avg edge</div><div style="font-size:22px;font-weight:700" class="{ 'up' if (iv['avg_edge_pct'] or 0)>=0 else 'down'}">{(f"{iv['avg_edge_pct']:+.1f}%") if iv['resolved'] else '—'}</div></div>
      <div><div class="mut" style="font-size:10.5px;text-transform:uppercase">Open</div><div style="font-size:22px;font-weight:700">{iv['open']}</div></div>
    </div>
    <div class="mut" style="font-size:12px;margin-top:10px">Testing whether IV-rich setups over-price the move: realized &lt; implied ⇒ a premium seller wins. Validating the edge before any execution.</div>
  </div>

  <div class="card">
    <h2>IV condor paper executions <span class="pill mut" style="font-size:11px">actual multi-leg fills only</span></h2>
    <div style="display:flex;gap:26px;flex-wrap:wrap;font-family:var(--mono)">
      <div><div class="mut" style="font-size:10.5px;text-transform:uppercase">Closed</div><div style="font-size:22px;font-weight:700">{condors['closed']}</div></div>
      <div><div class="mut" style="font-size:10.5px;text-transform:uppercase">Win rate</div><div style="font-size:22px;font-weight:700">{(str(round(condors['win_rate']))+'%') if condors['closed'] else '—'}</div></div>
      <div><div class="mut" style="font-size:10.5px;text-transform:uppercase">Condor P&amp;L</div><div class="{ 'up' if (condors['total_pnl'] or 0)>=0 else 'down'}" style="font-size:22px;font-weight:700">{_money(condors['total_pnl'])}</div></div>
      <div><div class="mut" style="font-size:10.5px;text-transform:uppercase">Active / pending</div><div style="font-size:22px;font-weight:700">{condors['open']} / {condors['pending']}</div></div>
    </div>
    <div class="mut" style="font-size:12px;margin-top:10px">This is separate from the legacy single-leg strategy ledger above.</div>
  </div>

  {_edge_html}

  {_variant_card}

  <div class="card">
    <h2>Equity curve</h2>
    <svg id="eq" viewBox="0 0 640 200" role="img" aria-label="Paper account equity over time"></svg>
  </div>

  <div class="card"><h2>By strategy (closed trades)</h2>
    <div class="scroll"><table><thead><tr><th>Setup</th><th>N</th><th>Win%</th><th>P&L</th><th>Avg</th><th>Avg hold</th></tr></thead>
    <tbody>{strat_rows}</tbody></table></div></div>

  <div class="card"><h2>Recent closed trades</h2>
    <div class="scroll"><table><thead><tr><th>Ticker</th><th>Setup</th><th>P&L</th><th>%</th><th>Exit</th><th>Hold</th></tr></thead>
    <tbody>{recent_rows}</tbody></table></div></div>

  <div class="prop"><h2>Proposed changes — your approval (propose &amp; approve)</h2><ul>{proposals}</ul></div>

  <div class="note"><b>Not financial advice.</b> Autonomous PAPER trading on Alpaca ($50k simulated). Metrics are computed from the bot's own fills; proposals are rule-based suggestions the human approves — nothing here changes a parameter or trades real money. Generated {e(d['generated'][:16].replace('T',' '))}Z.</div>
</div>
<script>
  (function(){{
    const pts={curve_json};
    const svg=document.getElementById('eq'); if(!svg) return;
    const W=640,H=200,ml=52,mr=16,mt=12,mb=24;
    const cs=getComputedStyle(document.documentElement),col=n=>cs.getPropertyValue(n).trim();
    if(pts.length<1){{svg.innerHTML=`<text x="${{W/2}}" y="${{H/2}}" text-anchor="middle" fill="${{col('--muted')}}" font-family="monospace" font-size="12">collecting…</text>`;return;}}
    const vals=pts.map(p=>p.equity), lo=Math.min(...vals,pts[0].base), hi=Math.max(...vals,pts[0].base);
    const pad=(hi-lo)*0.15||100, y0=lo-pad, y1=hi+pad;
    const X=i=>ml+(pts.length<2?0.5:i/(pts.length-1))*(W-ml-mr);
    const Y=v=>mt+(1-(v-y0)/(y1-y0))*(H-mt-mb);
    let g='';
    [y0,(y0+y1)/2,y1].forEach(v=>{{const y=Y(v);g+=`<line x1="${{ml}}" y1="${{y}}" x2="${{W-mr}}" y2="${{y}}" stroke="${{col('--border')}}"/>`;
      g+=`<text x="${{ml-6}}" y="${{y+3}}" text-anchor="end" font-size="9" fill="${{col('--muted')}}" font-family="monospace">$${{(v/1000).toFixed(1)}}k</text>`;}});
    const base=pts[0].base;g+=`<line x1="${{ml}}" y1="${{Y(base)}}" x2="${{W-mr}}" y2="${{Y(base)}}" stroke="${{col('--muted')}}" stroke-dasharray="3 3" opacity=".5"/>`;
    const line=pts.map((p,i)=>`${{X(i).toFixed(1)}},${{Y(p.equity).toFixed(1)}}`).join(' ');
    const last=pts[pts.length-1], up=last.equity>=base, c=up?col('--up'):col('--down');
    g+=`<polyline points="${{ml}},${{H-mb}} ${{line}} ${{X(pts.length-1)}},${{H-mb}}" fill="${{c}}" opacity=".12"/>`;
    g+=`<polyline points="${{line}}" fill="none" stroke="${{c}}" stroke-width="2.2" stroke-linejoin="round"/>`;
    g+=`<circle cx="${{X(pts.length-1).toFixed(1)}}" cy="${{Y(last.equity).toFixed(1)}}" r="3.4" fill="${{c}}"/>`;
    svg.innerHTML=g;
  }})();
</script>"""


def _money(v):
    if v is None:
        return "—"
    v = float(v)
    s = "-" if v < 0 else ""
    return f"{s}${abs(v):,.0f}"


def _hold(v):
    if v is None:
        return "—"
    v = float(v)
    if v < 60:
        return f"{v:.0f}m"
    if v < 1440:
        return f"{v/60:.1f}h"
    return f"{v/1440:.1f}d"


def _json_points(curve, base):
    import json as _j
    pts = [{"equity": p["equity"], "base": base} for p in curve]
    return _j.dumps(pts)
