"""Trailing realized volatility of an underlying, used as the IV-richness proxy.

CLAUDE_OPTIONS.md Step 2: this repo's Robinhood MCP does not expose a
historical IV series (get_option_historicals returns option price bars only),
so true IV Rank isn't computable yet. Until it is, we gate on current IV vs.
this trailing realized vol instead. Reuses the daily bars the swing agent
already fetches to data/<SYMBOL>_day.json — no extra network calls needed.
"""
from __future__ import annotations

import math
from pathlib import Path

from swing_agent.dataio import load as load_equity_bars


def trailing_realized_vol(symbol: str, data_dir: str | Path = "data", window: int = 20) -> float | None:
    """Annualized realized volatility of daily log returns over the trailing window.

    Returns None if the daily bar file is missing or too short.
    """
    path = Path(data_dir) / f"{symbol}_day.json"
    if not path.exists():
        return None
    df = load_equity_bars(path, symbol)
    if len(df) < window + 1:
        return None
    closes = df["close"].tail(window + 1).reset_index(drop=True)
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / (len(log_returns) - 1)
    daily_stdev = math.sqrt(variance)
    return round(daily_stdev * math.sqrt(252), 4)


def iv_richness_ratio(current_iv: float, symbol: str, data_dir: str | Path = "data", window: int = 20) -> float | None:
    """current_iv / trailing_realized_vol. CLAUDE_OPTIONS.md Step 2 gates entries at >= 1.2."""
    rv = trailing_realized_vol(symbol, data_dir, window)
    if not rv:
        return None
    return round(current_iv / rv, 3)
