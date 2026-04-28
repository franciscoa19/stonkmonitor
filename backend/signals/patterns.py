"""
Pattern Engine — cross-signal correlation detector.

Patterns fire when multiple independent signals converge on the same ticker
within a time window. Each pattern has:
  - name / description
  - conditions (what to look for in each feed table)
  - score (how strong the conviction is)
  - cooldown_hours (won't re-alert same ticker within this window)

All thresholds are tunable here. Add/edit patterns freely.
"""
import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class PatternResult:
    pattern_name: str
    ticker: str
    score: float
    description: str
    evidence: list[str]


@dataclass
class PatternConfig:
    name: str
    description: str
    score: float
    cooldown_hours: int = 24
    # Per-condition thresholds (all configurable)
    min_sweep_premium: float = 100_000
    min_darkpool_premium: float = 500_000
    min_insider_value: float = 50_000
    min_options_premium: float = 50_000
    lookback_days: int = 7


# ── Configurable pattern definitions ────────────────────────────────────
PATTERNS: list[PatternConfig] = [
    PatternConfig(
        name="sweep_plus_darkpool",
        description="Options sweep AND large dark pool print — institutions loading both derivatives and shares",
        score=9.0,
        min_sweep_premium=100_000,
        min_darkpool_premium=500_000,
        lookback_days=3,
        cooldown_hours=12,
    ),
    PatternConfig(
        name="insider_buy_plus_sweep",
        description="CEO/officer open-market buy AND bullish options sweep — insider + smart money aligned",
        score=9.5,
        min_sweep_premium=100_000,
        min_insider_value=50_000,
        lookback_days=14,
        cooldown_hours=24,
    ),
    PatternConfig(
        name="congress_plus_sweep",
        description="Congress member bought AND unusual options sweep on same ticker within 45 days",
        score=8.5,
        min_sweep_premium=50_000,
        lookback_days=45,
        cooldown_hours=48,
    ),
    PatternConfig(
        name="insider_cluster_buy",
        description="3+ insiders open-market buying same stock within 30 days — historically very bullish",
        score=9.0,
        min_insider_value=10_000,
        lookback_days=30,
        cooldown_hours=72,
    ),
    PatternConfig(
        name="congress_plus_darkpool",
        description="Congress buy + institutional dark pool accumulation on same ticker",
        score=8.0,
        min_darkpool_premium=1_000_000,
        lookback_days=45,
        cooldown_hours=48,
    ),
    PatternConfig(
        name="triple_confluence",
        description="Options sweep + dark pool + insider activity all on same ticker — highest conviction",
        score=10.0,
        # Raised 2026-04-27 — old thresholds fired 84 hits/day on a normal day,
        # making this pattern statistical noise rather than conviction signal.
        # Real triple-confluence on quality names (e.g. AMD, JPM, MS) clears these
        # easily; mid-cap noise (FITB, MKC, ASO) does not. Target: <15 hits/day.
        min_sweep_premium=500_000,        # was 100_000
        min_darkpool_premium=2_000_000,   # was 500_000
        min_insider_value=50_000,         # was 25_000
        lookback_days=14,
        cooldown_hours=24,
    ),
    PatternConfig(
        name="golden_sweep_cluster",
        description="2+ golden sweeps on same ticker within 3 days — aggressive repeated institutional bet",
        score=9.0,
        min_sweep_premium=200_000,
        lookback_days=3,
        cooldown_hours=12,
    ),
    PatternConfig(
        name="size_sweep",
        description="Single options sweep > $1M — major institutional directional bet",
        score=8.5,
        min_sweep_premium=1_000_000,
        lookback_days=1,
        cooldown_hours=6,
    ),
    PatternConfig(
        name="size_darkpool",
        description="Single dark pool print > $10M — large block accumulation",
        score=8.0,
        min_darkpool_premium=10_000_000,
        lookback_days=1,
        cooldown_hours=6,
    ),
]


class PatternEngine:
    def __init__(self, settings=None, notify_threshold: float = 8.0):
        self.notify_threshold = notify_threshold
        self._notifiers: list = []   # populated by main.py

    def set_notifiers(self, discord, pushover):
        self._notifiers = [discord, pushover]

    async def _notify(self, result: PatternResult):
        """Send pattern hit notification."""
        title = f"🎯 {result.ticker} — {result.pattern_name.replace('_', ' ').upper()}"
        body  = f"{result.description}\n" + "\n".join(f"  • {e}" for e in result.evidence)

        for notifier in self._notifiers:
            try:
                if hasattr(notifier, 'send_alert'):
                    await notifier.send_alert(title=title, message=body, priority=1)
                elif hasattr(notifier, 'send_raw'):
                    await notifier.send_raw(f"**{title}**\n{body}\nScore: {result.score}/10")
            except Exception as e:
                logger.warning(f"Pattern notify error: {e}")

    # ── Individual pattern checkers ──────────────────────────────────────

    async def _check_sweep_plus_darkpool(self, ticker: str, db, cfg: PatternConfig) -> Optional[PatternResult]:
        sweeps = await db.get_options_flow(
            ticker=ticker, min_premium=cfg.min_sweep_premium,
            has_sweep=True, limit=5
        )
        # Also check golden sweeps (alert_rule contains "Golden")
        if not sweeps:
            sweeps = await db.get_options_flow(
                ticker=ticker, min_premium=cfg.min_sweep_premium,
                alert_rule="Golden", limit=5
            )
        if not sweeps:
            return None

        dp = await db.get_dark_pool(
            ticker=ticker, min_premium=cfg.min_darkpool_premium, limit=3
        )
        if not dp:
            return None

        best_sweep = max(sweeps, key=lambda x: x["premium"])
        best_dp    = max(dp, key=lambda x: x["premium"])

        return PatternResult(
            pattern_name=cfg.name,
            ticker=ticker,
            score=cfg.score,
            description=cfg.description,
            evidence=[
                f"Sweep: ${best_sweep['premium']:,.0f} {best_sweep.get('opt_type','').upper()} (rule: {best_sweep.get('alert_rule','')})",
                f"Dark Pool: ${best_dp['premium']:,.0f} ({best_dp.get('size',0):,.0f} shares @ ${best_dp.get('price',0):.2f})",
            ],
        )

    async def _check_insider_buy_plus_sweep(self, ticker: str, db, cfg: PatternConfig) -> Optional[PatternResult]:
        buys = await db.get_insider_trades(
            ticker=ticker, code="P", min_value=cfg.min_insider_value, limit=5
        )
        if not buys:
            return None

        sweeps = await db.get_options_flow(
            ticker=ticker, min_premium=cfg.min_sweep_premium,
            has_sweep=True, limit=5
        )
        if not sweeps:
            return None

        best_buy   = max(buys, key=lambda x: x["dollar_value"])
        best_sweep = max(sweeps, key=lambda x: x["premium"])
        side       = best_sweep.get("opt_type", "").upper()

        return PatternResult(
            pattern_name=cfg.name,
            ticker=ticker,
            score=cfg.score,
            description=cfg.description,
            evidence=[
                f"Insider buy: {best_buy.get('owner_name','')} ({best_buy.get('officer_title','')}) — ${best_buy['dollar_value']:,.0f}",
                f"Sweep: ${best_sweep['premium']:,.0f} {side} (rule: {best_sweep.get('alert_rule','')})",
            ],
        )

    async def _check_congress_plus_sweep(self, ticker: str, db, cfg: PatternConfig) -> Optional[PatternResult]:
        congress = await db.get_congress_trades(ticker=ticker, txn_type="Buy", limit=5)
        if not congress:
            return None

        sweeps = await db.get_options_flow(
            ticker=ticker, min_premium=cfg.min_sweep_premium, limit=10
        )
        if not sweeps:
            return None

        best_ct    = congress[0]
        best_sweep = max(sweeps, key=lambda x: x["premium"])

        return PatternResult(
            pattern_name=cfg.name,
            ticker=ticker,
            score=cfg.score,
            description=cfg.description,
            evidence=[
                f"Congress buy: {best_ct.get('member_name','')} ({best_ct.get('chamber','').title()}) — {best_ct.get('amounts','')}",
                f"Options flow: ${best_sweep['premium']:,.0f} {best_sweep.get('opt_type','').upper()} (rule: {best_sweep.get('alert_rule','')})",
            ],
        )

    async def _check_insider_cluster(self, ticker: str, db, cfg: PatternConfig) -> Optional[PatternResult]:
        buys = await db.get_insider_trades(
            ticker=ticker, code="P", min_value=cfg.min_insider_value, limit=20
        )
        if len(buys) < 3:
            return None

        total_val = sum(b["dollar_value"] for b in buys)
        names     = list({b["owner_name"] for b in buys})[:5]

        return PatternResult(
            pattern_name=cfg.name,
            ticker=ticker,
            score=cfg.score,
            description=cfg.description,
            evidence=[
                f"{len(buys)} insider buys totaling ${total_val:,.0f}",
                f"Buyers: {', '.join(names)}",
            ],
        )

    async def _check_congress_plus_darkpool(self, ticker: str, db, cfg: PatternConfig) -> Optional[PatternResult]:
        congress = await db.get_congress_trades(ticker=ticker, txn_type="Buy", limit=5)
        if not congress:
            return None

        dp = await db.get_dark_pool(ticker=ticker, min_premium=cfg.min_darkpool_premium, limit=3)
        if not dp:
            return None

        best_ct = congress[0]
        best_dp = max(dp, key=lambda x: x["premium"])

        return PatternResult(
            pattern_name=cfg.name,
            ticker=ticker,
            score=cfg.score,
            description=cfg.description,
            evidence=[
                f"Congress buy: {best_ct.get('member_name','')} — {best_ct.get('amounts','')}",
                f"Dark pool: ${best_dp['premium']:,.0f} ({best_dp.get('size',0):,.0f} shares)",
            ],
        )

    async def _check_triple_confluence(self, ticker: str, db, cfg: PatternConfig) -> Optional[PatternResult]:
        sweeps  = await db.get_options_flow(ticker=ticker, min_premium=cfg.min_sweep_premium, limit=5)
        dp      = await db.get_dark_pool(ticker=ticker, min_premium=cfg.min_darkpool_premium, limit=3)
        insiders = await db.get_insider_trades(ticker=ticker, min_value=cfg.min_insider_value, limit=5)

        if not (sweeps and dp and insiders):
            return None

        return PatternResult(
            pattern_name=cfg.name,
            ticker=ticker,
            score=cfg.score,
            description=cfg.description,
            evidence=[
                f"Sweeps: {len(sweeps)} (best ${max(s['premium'] for s in sweeps):,.0f})",
                f"Dark pool: {len(dp)} prints (best ${max(d['premium'] for d in dp):,.0f})",
                f"Insiders: {len(insiders)} trades",
            ],
        )

    async def _check_golden_sweep_cluster(self, ticker: str, db, cfg: PatternConfig) -> Optional[PatternResult]:
        golden = await db.get_options_flow(
            ticker=ticker, min_premium=cfg.min_sweep_premium,
            alert_rule="Golden", limit=10
        )
        if len(golden) < 2:
            return None

        total = sum(g["premium"] for g in golden)
        calls = sum(1 for g in golden if g.get("opt_type") == "call")
        puts  = len(golden) - calls

        return PatternResult(
            pattern_name=cfg.name,
            ticker=ticker,
            score=cfg.score,
            description=cfg.description,
            evidence=[
                f"{len(golden)} golden sweeps totaling ${total:,.0f}",
                f"{calls} calls / {puts} puts",
            ],
        )

    async def _check_size_sweep(self, ticker: str, db, cfg: PatternConfig) -> Optional[PatternResult]:
        sweeps = await db.get_options_flow(ticker=ticker, min_premium=cfg.min_sweep_premium, limit=3)
        if not sweeps:
            return None
        best = max(sweeps, key=lambda x: x["premium"])
        return PatternResult(
            pattern_name=cfg.name,
            ticker=ticker,
            score=cfg.score,
            description=cfg.description,
            evidence=[
                f"${best['premium']:,.0f} {best.get('opt_type','').upper()} sweep",
                f"Rule: {best.get('alert_rule','')} | Strike: ${best.get('strike',0)} | Exp: {best.get('expiry','')}",
            ],
        )

    async def _check_size_darkpool(self, ticker: str, db, cfg: PatternConfig) -> Optional[PatternResult]:
        dp = await db.get_dark_pool(ticker=ticker, min_premium=cfg.min_darkpool_premium, limit=3)
        if not dp:
            return None
        best = max(dp, key=lambda x: x["premium"])
        return PatternResult(
            pattern_name=cfg.name,
            ticker=ticker,
            score=cfg.score,
            description=cfg.description,
            evidence=[
                f"${best['premium']:,.0f} dark pool print",
                f"{best.get('size',0):,.0f} shares @ ${best.get('price',0):.2f}",
            ],
        )

    # ── Master evaluate ──────────────────────────────────────────────────

    CHECKERS = {
        "sweep_plus_darkpool":    "_check_sweep_plus_darkpool",
        "insider_buy_plus_sweep": "_check_insider_buy_plus_sweep",
        "congress_plus_sweep":    "_check_congress_plus_sweep",
        "insider_cluster_buy":    "_check_insider_cluster",
        "congress_plus_darkpool": "_check_congress_plus_darkpool",
        "triple_confluence":      "_check_triple_confluence",
        "golden_sweep_cluster":   "_check_golden_sweep_cluster",
        "size_sweep":             "_check_size_sweep",
        "size_darkpool":          "_check_size_darkpool",
    }

    async def evaluate(self, ticker: str, trigger_channel: str, db) -> list[PatternResult]:
        """
        Run all relevant patterns for a ticker after a new event arrives.
        Only checks patterns that could be triggered by this feed type.
        """
        if not ticker:
            return []

        # Which patterns are relevant for each trigger channel
        channel_patterns = {
            "options-flow":   ["sweep_plus_darkpool", "insider_buy_plus_sweep",
                               "congress_plus_sweep", "triple_confluence",
                               "golden_sweep_cluster", "size_sweep"],
            "darkpool":       ["sweep_plus_darkpool", "congress_plus_darkpool",
                               "triple_confluence", "size_darkpool"],
            "insider-trades": ["insider_buy_plus_sweep", "insider_cluster_buy",
                               "triple_confluence"],
            "congress-trades":["congress_plus_sweep", "congress_plus_darkpool",
                               "triple_confluence"],
        }
        relevant = channel_patterns.get(trigger_channel, [])

        fired: list[PatternResult] = []

        for cfg in PATTERNS:
            if cfg.name not in relevant:
                continue

            checker_name = self.CHECKERS.get(cfg.name)
            if not checker_name:
                continue

            try:
                result = await getattr(self, checker_name)(ticker, db, cfg)
                if result is None:
                    continue

                # Cooldown check
                if await db.was_pattern_recently_hit(cfg.name, ticker, cfg.cooldown_hours):
                    continue

                # Persist
                await db.save_pattern_hit(
                    cfg.name, ticker, result.score,
                    result.description, result.evidence
                )

                # Notify if above threshold
                if result.score >= self.notify_threshold:
                    await self._notify(result)

                # Broadcast to frontend
                from api.websocket import manager
                await manager.broadcast({
                    "type": "pattern",
                    "data": {
                        "pattern": result.pattern_name,
                        "ticker": result.ticker,
                        "score": result.score,
                        "description": result.description,
                        "evidence": result.evidence,
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                })

                logger.info(f"PATTERN HIT: {result.pattern_name} on {ticker} | Score {result.score}")
                fired.append(result)

            except Exception as e:
                logger.warning(f"Pattern check error ({cfg.name} / {ticker}): {e}")

        return fired
