"""Bullish reversal pattern detection: double bottom and inverse head & shoulders.

Each detector scans a DataFrame of OHLC bars (entry timeframe) and returns a list
of pattern dicts. A pattern is only emitted once its neckline has been broken to
the upside, which is the confirmation the source strategies require.

Pattern dict fields:
    type            'double_bottom' | 'inverse_hns'
    neckline        float, the breakout level
    stop_basis      float, the swing low the stop sits under (2nd bottom / right shoulder)
    break_index     int, bar index where close first cleared the neckline
    break_time      timestamp of that bar
    pivots          the raw pivot prices/indices that formed the shape
"""
from __future__ import annotations

import pandas as pd

from .indicators import pivots, swing_points


def _confirm_break(df: pd.DataFrame, neckline: float, after_index: int) -> dict | None:
    """Find the first bar after `after_index` whose close clears the neckline."""
    for i in range(after_index + 1, len(df)):
        if df.loc[i, "close"] > neckline:
            return {"break_index": int(i), "break_time": df.loc[i, "begins_at"]}
    return None


def detect_double_bottom(
    df: pd.DataFrame,
    left: int = 3,
    right: int = 3,
    tol: float = 0.02,
    min_sep: int = 8,
    max_sep: int = 60,
) -> list[dict]:
    """Two comparable lows separated by a middle high (the neckline).

    tol: max relative difference between the two bottoms.
    min_sep / max_sep: allowed bar distance between the two bottoms.
    """
    marked = swing_points(df, left, right)
    lows = pivots(marked, "low")
    highs = pivots(marked, "high")
    patterns = []

    for a in range(len(lows)):
        for b in range(a + 1, len(lows)):
            l1, l2 = lows[a], lows[b]
            sep = l2["index"] - l1["index"]
            if sep < min_sep or sep > max_sep:
                continue
            if abs(l2["price"] - l1["price"]) / l1["price"] > tol:
                continue
            # 2nd bottom must be a higher or equal low (classic double-bottom structure)
            if l2["price"] < l1["price"]:
                continue
            # middle high between the two bottoms = neckline
            mids = [h for h in highs if l1["index"] < h["index"] < l2["index"]]
            if not mids:
                continue
            neckline = max(m["price"] for m in mids)
            # second bottom should hold above the first by structure (higher or equal low)
            brk = _confirm_break(df, neckline, l2["index"])
            if not brk:
                continue
            patterns.append(
                {
                    "type": "double_bottom",
                    "neckline": neckline,
                    "stop_basis": min(l1["price"], l2["price"]),
                    "break_index": brk["break_index"],
                    "break_time": brk["break_time"],
                    "pivots": {"bottom1": l1, "bottom2": l2, "neckline_price": neckline},
                }
            )
    return _dedupe(patterns)


def detect_inverse_hns(
    df: pd.DataFrame,
    left: int = 3,
    right: int = 3,
    shoulder_tol: float = 0.06,
    min_sep: int = 3,
    max_span: int = 90,
) -> list[dict]:
    """Left shoulder, lower head, higher-low right shoulder; neckline across the
    two intervening highs. Imperfect shoulders allowed within shoulder_tol.
    """
    marked = swing_points(df, left, right)
    lows = pivots(marked, "low")
    highs = pivots(marked, "high")
    patterns = []

    for i in range(len(lows)):
        for j in range(i + 1, len(lows)):
            for k in range(j + 1, len(lows)):
                ls, head, rs = lows[i], lows[j], lows[k]
                if rs["index"] - ls["index"] > max_span:
                    continue
                if (head["index"] - ls["index"]) < min_sep or (rs["index"] - head["index"]) < min_sep:
                    continue
                # head must be the lowest
                if not (head["price"] < ls["price"] and head["price"] < rs["price"]):
                    continue
                # shoulders roughly comparable
                if abs(rs["price"] - ls["price"]) / ls["price"] > shoulder_tol:
                    continue
                # right shoulder is a higher low than the head (already true) -> ok
                # neckline = the two highs between LS-head and head-RS
                peak1 = [h for h in highs if ls["index"] < h["index"] < head["index"]]
                peak2 = [h for h in highs if head["index"] < h["index"] < rs["index"]]
                if not peak1 or not peak2:
                    continue
                neckline = max(max(p["price"] for p in peak1), max(p["price"] for p in peak2))
                brk = _confirm_break(df, neckline, rs["index"])
                if not brk:
                    continue
                patterns.append(
                    {
                        "type": "inverse_hns",
                        "neckline": neckline,
                        "stop_basis": rs["price"],
                        "break_index": brk["break_index"],
                        "break_time": brk["break_time"],
                        "pivots": {
                            "left_shoulder": ls,
                            "head": head,
                            "right_shoulder": rs,
                            "neckline_price": neckline,
                        },
                    }
                )
    return _dedupe(patterns)


def _dedupe(patterns: list[dict]) -> list[dict]:
    """Keep one pattern per break bar (the tightest stop), avoid duplicate signals."""
    by_break: dict[int, dict] = {}
    for p in patterns:
        bi = p["break_index"]
        if bi not in by_break or p["stop_basis"] > by_break[bi]["stop_basis"]:
            by_break[bi] = p
    return [by_break[k] for k in sorted(by_break)]


def daily_bias_long(df: pd.DataFrame, ema_period: int = 20) -> bool:
    """Long bias when the most recent daily bar sits fully above the 20 EMA
    (no touch) and the recent swing-low structure has not broken.
    """
    from .indicators import ema

    e = ema(df["close"], ema_period)
    last = len(df) - 1
    if df.loc[last, "low"] <= e.iloc[last]:
        return False
    # structure: last confirmed swing low should still hold (price above it)
    marked = swing_points(df)
    lows = pivots(marked, "low")
    if lows:
        last_swing_low = lows[-1]["price"]
        if df.loc[last, "close"] < last_swing_low:
            return False
    return True
