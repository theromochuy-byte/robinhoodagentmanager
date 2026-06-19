"""Technical indicators and swing-pivot detection.

All functions operate on a pandas DataFrame with columns:
    begins_at, open, high, low, close, volume
indexed 0..n-1 in chronological order.
"""
from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False).mean()


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range (Wilder smoothing)."""
    high = df["high"]
    low = df["low"]
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False).mean()


def swing_points(df: pd.DataFrame, left: int = 3, right: int = 3) -> pd.DataFrame:
    """Mark fractal swing highs and lows.

    A bar is a swing high if its high is the strict max over a window of
    `left` bars before and `right` bars after. Swing low is the mirror.
    Adds boolean columns 'swing_high' and 'swing_low'.
    """
    n = len(df)
    is_high = [False] * n
    is_low = [False] * n
    highs = df["high"].values
    lows = df["low"].values

    for i in range(left, n - right):
        window_high = highs[i - left : i + right + 1]
        window_low = lows[i - left : i + right + 1]
        if highs[i] == window_high.max() and (window_high == highs[i]).sum() == 1:
            is_high[i] = True
        if lows[i] == window_low.min() and (window_low == lows[i]).sum() == 1:
            is_low[i] = True

    out = df.copy()
    out["swing_high"] = is_high
    out["swing_low"] = is_low
    return out


def pivots(df: pd.DataFrame, kind: str) -> list[dict]:
    """Return swing pivots of a given kind as a list of {index, price, time}."""
    assert kind in ("high", "low")
    col = "swing_high" if kind == "high" else "swing_low"
    price_col = "high" if kind == "high" else "low"
    rows = []
    for i, row in df.iterrows():
        if row[col]:
            rows.append(
                {"index": int(i), "price": float(row[price_col]), "time": row["begins_at"]}
            )
    return rows
