"""Step 1 trend confirmation: is the stock above its 20- and 50-day SMA?

A second wheel-strategy source (same supplier as the Step 1 quality gate)
adds a technical trend filter -- price above both the 20-period and 50-period
moving average -- alongside the fundamental screen. Combined with the
existing "price <= 90% of 52-week high" check in quality_screen.py, this
reproduces both sources' actual intent: an established uptrend that has
pulled back, not a stock in a downtrend or one still making new highs.

Computed locally from data/<SYMBOL>_day.json (already fetched daily by the
swing agent) rather than calling get_equity_technical_indicators, to stay
consistent with this package's no-network-I/O convention -- same reasoning
as realized_vol.py.
"""
from __future__ import annotations

from pathlib import Path

from swing_agent.dataio import load as load_equity_bars


def above_moving_averages(
    symbol: str,
    data_dir: str | Path = "data",
    periods: tuple[int, int] = (20, 50),
) -> dict:
    """Returns {"pass": bool, "reasons": [...], "price": ..., "sma20": ..., "sma50": ...}.

    pass is False (with an explanatory reason) if the daily bar file is
    missing, too short for the longer SMA period, or price is below either
    average.
    """
    short_p, long_p = periods
    path = Path(data_dir) / f"{symbol}_day.json"
    if not path.exists():
        return {"pass": False, "reasons": [f"no daily bar file for {symbol}"]}

    df = load_equity_bars(path, symbol)
    if len(df) < long_p:
        return {"pass": False, "reasons": [f"only {len(df)} daily bars, need >= {long_p}"]}

    price = float(df["close"].iloc[-1])
    sma_short = float(df["close"].tail(short_p).mean())
    sma_long = float(df["close"].tail(long_p).mean())

    reasons = []
    if price < sma_short:
        reasons.append(f"price {price:.2f} < {short_p}-period SMA {sma_short:.2f}")
    if price < sma_long:
        reasons.append(f"price {price:.2f} < {long_p}-period SMA {sma_long:.2f}")

    return {
        "pass": len(reasons) == 0,
        "reasons": reasons,
        "price": price,
        f"sma{short_p}": round(sma_short, 2),
        f"sma{long_p}": round(sma_long, 2),
    }
