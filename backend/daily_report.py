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
        "by_strategy": [dict(r) for r in by_strategy],
        "by_hour": [dict(r) for r in by_hour],
        "recent": recent,
        "open_positions": open_pos,
        "proposals": proposals,
    }


def _iso_days_ago(days: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ══════════════════════════════════════════════════════════════════════════
#  HTML render
# ══════════════════════════════════════════════════════════════════════════
def render_html(d: dict) -> str:
    a, m = d["account"], d["metrics"]
    act = d["activity"]
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
