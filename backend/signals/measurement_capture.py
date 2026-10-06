"""Reproducible measurement inputs; configuration is deliberately allowlisted."""
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
from math import isfinite
from pathlib import Path

SETUP_FIELDS = ("ticker", "price", "avg_volume", "iv30", "rv30", "iv30_rv30", "ts_slope",
                "expected_move", "vol_ok", "iv_expensive", "ts_inverted", "next_earnings_date",
                "earnings_report_time", "recommendation")
CONFIG_FIELDS = ("alpaca_options_feed", "iv_exec_min_dte", "iv_exec_max_dte", "iv_exec_short_move_mult",
                 "iv_exec_wing_width_pct", "iv_conservative_fills", "iv_fee_per_contract",
                 "iv_setup_max_days_to_earnings", "iv_exec_entry_days_before",
                 "iv_variants_reprice_within_days", "iv_measure_min_market_cap")


@lru_cache(maxsize=1)
def strategy_revision():
    """Hash the running strategy sources, including uncommitted implementation."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for name in ("config.py", "main.py", "signals/earnings_scanner.py", "signals/iv_executor.py",
                 "signals/iv_variants.py", "signals/measurement_capture.py"):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def _clean(value):
    if isinstance(value, float) and not isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_clean(v) for v in value]
    return value


def measurement_snapshot(setup, settings, variants, diagnostics, *, action, lead_days, gate_passed):
    from db import VALIDATED_VARIANT_PRICING_MODEL
    from signals.earnings_scanner import MIN_AVG_VOLUME, MIN_IV_RV_RATIO, MAX_TS_SLOPE
    return _clean({
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "strategy_revision": strategy_revision(),
        "pricing_model": VALIDATED_VARIANT_PRICING_MODEL,
        "action": action, "lead_days": lead_days, "gate_passed": gate_passed,
        "setup": {k: getattr(setup, k, None) for k in SETUP_FIELDS},
        "config": {k: getattr(settings, k, None) for k in CONFIG_FIELDS},
        "scanner_thresholds": {"avg_volume": MIN_AVG_VOLUME, "iv30_rv30": MIN_IV_RV_RATIO,
                               "ts_slope": MAX_TS_SLOPE},
        "variants": variants, "diagnostics": diagnostics,
    })
