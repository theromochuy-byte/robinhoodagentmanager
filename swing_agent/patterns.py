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

from .candlesticks import is_bullish_engulfing, is_hammer
from .indicators import ema, pivots, swing_points


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


def daily_ma_crossover_bias(daily_df: pd.DataFrame, fast: int = 20, slow: int = 50) -> dict:
    """EMA(fast) > EMA(slow) on the most recent daily bar = confirmed new uptrend,
    per PROPOSAL_SWING_MA_CROSSOVER.md. Distinct from daily_bias_long (the
    existing 20-EMA-no-touch rule) -- this is a separate trend-confirmation
    mechanism for the ma_crossover_pullback trigger only, not a replacement.

    Also reports the SMA(fast)/SMA(slow) crossover state for comparison
    logging. It is NOT used as a gate: requiring EMA and SMA to agree wasn't
    something the source specified, and adding that requirement would be an
    invented rule, not one taken from it. Logging it lets us compare later
    with real data instead of guessing now.
    """
    if len(daily_df) < slow + 1:
        return {"pass": False, "reasons": [f"only {len(daily_df)} daily bars, need >= {slow + 1}"]}

    ema_fast = ema(daily_df["close"], fast)
    ema_slow = ema(daily_df["close"], slow)
    sma_fast = daily_df["close"].rolling(fast).mean()
    sma_slow = daily_df["close"].rolling(slow).mean()

    last = len(daily_df) - 1
    ema_bias = bool(ema_fast.iloc[last] > ema_slow.iloc[last])
    sma_bias = bool(sma_fast.iloc[last] > sma_slow.iloc[last]) if pd.notna(sma_fast.iloc[last]) else None

    # Most recent crossing-up event (False -> True) visible in the window.
    crossover_index = None
    prev_above = None
    for i in range(slow, last + 1):
        above = bool(ema_fast.iloc[i] > ema_slow.iloc[i])
        if prev_above is False and above:
            crossover_index = i
        prev_above = above
    if crossover_index is None:
        # EMA was already above for the whole visible window -- the actual
        # crossover happened before our data starts. Use the earliest
        # computable bar as an "at least this far back" approximation.
        crossover_index = slow

    return {
        "pass": ema_bias,
        "reasons": [] if ema_bias else ["ema_fast <= ema_slow on the most recent daily bar"],
        "ema_bias": ema_bias,
        "sma_bias": sma_bias,
        "ema_agrees_with_sma": (ema_bias == sma_bias) if sma_bias is not None else None,
        "crossover_index": crossover_index,
        "crossover_time": daily_df.loc[crossover_index, "begins_at"],
    }


def detect_ma_crossover_pullback(
    daily_df: pd.DataFrame,
    entry_df: pd.DataFrame,
    fast: int = 20,
    slow: int = 50,
    support_tolerance: float = 0.02,
) -> list[dict]:
    """Continuation entry per PROPOSAL_SWING_MA_CROSSOVER.md: daily MA
    crossover (Step 1) confirms a new uptrend -> the entry timeframe must
    print a higher swing high since the crossover (Step 2) -> the first
    pullback after that higher high (Step 3) -> the pullback must be holding
    support, either at the entry timeframe's fast EMA or at a prior swing
    high now acting as support ("old resistance becomes new floor") (Step 4)
    -> a hammer or bullish-engulfing candle confirms the reversal (Step 5).

    Emits at most one pattern: the first qualifying pullback since the most
    recent crossover, matching the source's "buy the first pullback, not a
    later one" rule. Shaped like detect_double_bottom / detect_inverse_hns
    (type/neckline/stop_basis/break_index/break_time/pivots) so it plugs
    into simulator.build_trade unchanged: neckline = the higher swing high,
    stop_basis = the pullback low (paired with the existing swing-low minus
    1xATR stop rule).
    """
    bias = daily_ma_crossover_bias(daily_df, fast, slow)
    if not bias["pass"]:
        return []

    marked = swing_points(entry_df)
    highs = pivots(marked, "high")
    lows = pivots(marked, "low")
    if len(highs) < 2 or not lows:
        return []

    crossover_time = bias["crossover_time"]
    post_highs = [h for h in highs if h["time"] >= crossover_time]
    pre_highs = [h for h in highs if h["time"] < crossover_time]
    if not post_highs:
        return []

    prior_high_price = pre_highs[-1]["price"] if pre_highs else 0.0
    higher_high = next((h for h in post_highs if h["price"] > prior_high_price), None)
    if higher_high is None:
        return []

    pullback = next((l for l in lows if l["index"] > higher_high["index"]), None)
    if pullback is None:
        return []

    entry_fast_ema = ema(entry_df["close"], fast)
    pullback_low = pullback["price"]
    ma_at_pullback = float(entry_fast_ema.iloc[pullback["index"]])
    near_ma_support = pullback_low >= ma_at_pullback * (1 - support_tolerance)
    old_resistance_support = any(
        h["price"] * (1 - support_tolerance) <= pullback_low <= h["price"] * 1.10
        and h["index"] < higher_high["index"]
        for h in pre_highs
    )
    if not (near_ma_support or old_resistance_support):
        return []

    for i in range(pullback["index"], min(pullback["index"] + 4, len(entry_df))):
        hammer = is_hammer(entry_df, i)
        engulfing = is_bullish_engulfing(entry_df, i)
        if hammer or engulfing:
            return [
                {
                    "type": "ma_crossover_pullback",
                    "neckline": round(higher_high["price"], 4),
                    "stop_basis": round(pullback_low, 4),
                    "break_index": i,
                    "break_time": entry_df.loc[i, "begins_at"],
                    "pivots": {
                        "higher_high": higher_high,
                        "pullback_low": pullback,
                        "daily_bias": bias,
                        "candle_pattern": "hammer" if hammer else "bullish_engulfing",
                        "support_type": "fast_ma" if near_ma_support else "old_resistance",
                    },
                }
            ]
    return []


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
