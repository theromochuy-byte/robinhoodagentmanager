"""Paper-trading simulator.

Takes confirmed patterns plus the entry-timeframe bars and produces resolved
paper trades. NO live orders are ever placed; this is pure simulation over
historical bars so we can collect P&L on proposed entries and exits.
"""
from __future__ import annotations

import pandas as pd

from .indicators import atr


def _prior_structure_high(df: pd.DataFrame, before_index: int, neckline: float) -> float | None:
    """Highest high to the left of the breakout that sits above the neckline,
    used as the primary (structure) target.
    """
    window = df.loc[: before_index - 1]
    above = window[window["high"] > neckline]
    if len(above) == 0:
        return None
    return float(above["high"].max())


def build_trade(
    df: pd.DataFrame,
    pattern: dict,
    atr_series: pd.Series,
    equity: float,
    risk_pct: float = 0.01,
    atr_mult: float = 1.0,
    atr_override: float | None = None,
) -> dict | None:
    """Construct a proposed long trade from a confirmed pattern.

    Entry = close of the breakout bar (Entry A / breakout style).
    Stop  = stop_basis low minus atr_mult * ATR at the breakout bar.
    Targets: primary = prior structure high; plus 1R and 2R reference levels.
    atr_override: live ATR value from Robinhood API; replaces bar-computed ATR when provided.
    """
    bi = pattern["break_index"]
    entry = float(df.loc[bi, "close"])
    a = atr_override if atr_override is not None else float(atr_series.iloc[bi])
    stop = pattern["stop_basis"] - atr_mult * a
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return None

    r1 = entry + risk_per_share
    r2 = entry + 2 * risk_per_share
    struct = _prior_structure_high(df, bi, pattern["neckline"])
    primary_target = struct if (struct and struct > entry) else r2

    shares = (equity * risk_pct) / risk_per_share

    return {
        "type": pattern["type"],
        "entry_time": str(df.loc[bi, "begins_at"]),
        "entry_index": bi,
        "entry": round(entry, 4),
        "stop": round(stop, 4),
        "neckline": round(pattern["neckline"], 4),
        "risk_per_share": round(risk_per_share, 4),
        "target_primary": round(primary_target, 4),
        "target_1R": round(r1, 4),
        "target_2R": round(r2, 4),
        "shares": round(shares, 4),
        "atr": round(a, 4),
    }
