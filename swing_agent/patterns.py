"""Bullish reversal pattern detection: double bottom, inverse head & shoulders, cup and handle.

Each detector scans a DataFrame of OHLC bars (entry timeframe) and returns a list
of pattern dicts. A pattern is only emitted once its neckline has been broken to
the upside, which is the confirmation the source strategies require.

Pattern dict fields:
    type            'double_bottom' | 'inverse_hns' | 'cup_and_handle'
    neckline        float, the breakout level
    stop_basis      float, the swing low the stop sits under (2nd bottom / right shoulder / handle low)
    break_index     int, bar index where close first cleared the neckline
    break_time      timestamp of that bar
    pivots          the raw pivot prices/indices that formed the shape
    target_measured float (cup_and_handle only) — neckline + cup height, measured move target
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


def _vol_avg(df: pd.DataFrame, window: int = 20) -> pd.Series | None:
    """Rolling average volume series. Returns None if volume column missing or all-zero."""
    if "volume" not in df.columns:
        return None
    vol = df["volume"].astype(float)
    if vol.sum() == 0:
        return None
    return vol.rolling(window, min_periods=5).mean()


def _vol_around(df: pd.DataFrame, idx: int, half: int = 2) -> float:
    """Mean volume in a small window centred on idx."""
    lo = max(0, idx - half)
    hi = min(len(df) - 1, idx + half)
    return float(df["volume"].iloc[lo : hi + 1].mean())


def _volume_confirms_double_bottom(
    df: pd.DataFrame,
    l1_idx: int,
    l2_idx: int,
    break_idx: int,
    vol_avg: pd.Series,
) -> bool:
    """Check confirm→contract→confirm volume pattern.

    Passes when:
      - Volume around the 1st bottom is above the rolling average (accumulation).
      - Volume around the 2nd bottom is below that of the 1st (contraction).
      - Volume at the breakout bar is above the rolling average (confirmation).
    Any condition skipped when the average is unavailable at that index.
    """
    avg1 = vol_avg.iloc[l1_idx] if l1_idx < len(vol_avg) else None
    avg2 = vol_avg.iloc[break_idx] if break_idx < len(vol_avg) else None

    v1 = _vol_around(df, l1_idx)
    v2 = _vol_around(df, l2_idx)
    vb = float(df["volume"].iloc[break_idx])

    if avg1 and v1 < avg1:
        return False
    if v2 >= v1:
        return False
    if avg2 and vb < avg2:
        return False
    return True


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
    Volume confirm→contract→confirm pattern is required when volume data is present.
    """
    marked = swing_points(df, left, right)
    lows = pivots(marked, "low")
    highs = pivots(marked, "high")
    vol_avg = _vol_avg(df)
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
            brk = _confirm_break(df, neckline, l2["index"])
            if not brk:
                continue
            # Volume confirm→contract→confirm (skip if volume unavailable)
            if vol_avg is not None:
                if not _volume_confirms_double_bottom(
                    df, l1["index"], l2["index"], brk["break_index"], vol_avg
                ):
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


def _volume_confirms_inverse_hns(
    df: pd.DataFrame,
    ls_idx: int,
    head_idx: int,
    rs_idx: int,
    break_idx: int,
    vol_avg: pd.Series,
) -> bool:
    """Volume signature for inverse head and shoulders.

    Passes when ALL of the following hold (any check skipped if volume unavailable):
      1. Head volume >= left-shoulder volume (capitulation / selling climax at head).
      2. Right-shoulder volume < head volume (sellers exhausted, contraction).
      3. Breakout volume > rolling average (buyers confirm the break).
    """
    v_ls  = _vol_around(df, ls_idx)
    v_head = _vol_around(df, head_idx)
    v_rs  = _vol_around(df, rs_idx)
    v_brk = float(df["volume"].iloc[break_idx])
    avg_at_brk = vol_avg.iloc[break_idx] if break_idx < len(vol_avg) else None

    # Head must show at least as much selling pressure as the left shoulder
    if v_head < v_ls:
        return False
    # Right shoulder must be quieter than the head (exhaustion)
    if v_rs >= v_head:
        return False
    # Breakout bar must exceed the rolling average volume
    if avg_at_brk and v_brk < avg_at_brk:
        return False
    return True


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
    Volume confirm: head >= LS (capitulation), RS < head (exhaustion),
    breakout > avg (confirmation). Skipped when volume data unavailable.
    """
    marked = swing_points(df, left, right)
    lows = pivots(marked, "low")
    highs = pivots(marked, "high")
    vol_avg = _vol_avg(df)
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
                # neckline = the two highs between LS-head and head-RS
                peak1 = [h for h in highs if ls["index"] < h["index"] < head["index"]]
                peak2 = [h for h in highs if head["index"] < h["index"] < rs["index"]]
                if not peak1 or not peak2:
                    continue
                neckline = max(max(p["price"] for p in peak1), max(p["price"] for p in peak2))
                brk = _confirm_break(df, neckline, rs["index"])
                if not brk:
                    continue
                # Volume: capitulation at head, exhaustion at RS, expansion at break
                if vol_avg is not None:
                    if not _volume_confirms_inverse_hns(
                        df, ls["index"], head["index"], rs["index"],
                        brk["break_index"], vol_avg,
                    ):
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


def detect_cup_and_handle(
    df: pd.DataFrame,
    left: int = 3,
    right: int = 3,
    rim_tol: float = 0.03,
    min_cup_depth: float = 0.05,
    min_cup_span: int = 15,
    max_cup_span: int = 80,
    max_handle_retrace: float = 0.50,
    min_handle_bars: int = 2,
    max_handle_bars: int = 20,
) -> list[dict]:
    """Two comparable swing highs (cup rims) with a rounded bottom between them,
    followed by a shallow pullback handle, then a close above the neckline.

    rim_tol: max relative difference between the two rim highs.
    min_cup_depth: cup bottom must be >= this fraction below the neckline.
    min/max_cup_span: bar range between rim1 and rim2.
    max_handle_retrace: handle low can't drop below 50% of cup depth from neckline.
    Volume expansion on the breakout bar is required when volume data is present.
    """
    marked = swing_points(df, left, right)
    highs = pivots(marked, "high")
    vol_avg = _vol_avg(df)
    patterns = []

    for i in range(len(highs)):
        for j in range(i + 1, len(highs)):
            rim1, rim2 = highs[i], highs[j]
            span = rim2["index"] - rim1["index"]
            if span < min_cup_span or span > max_cup_span:
                continue

            # Rims must be comparable heights
            if abs(rim2["price"] - rim1["price"]) / rim1["price"] > rim_tol:
                continue

            # Neckline at the lower of the two rims (conservative — requires a real break)
            neckline = min(rim1["price"], rim2["price"])

            # Cup bottom: lowest low strictly between rim1 and rim2
            cup_slice = df.loc[rim1["index"] + 1 : rim2["index"] - 1]
            if len(cup_slice) == 0:
                continue
            cup_bottom_price = float(cup_slice["low"].min())
            cup_depth_abs = neckline - cup_bottom_price
            if cup_depth_abs / neckline < min_cup_depth:
                continue

            # Handle: consolidation after rim2, must pull back but not too deep
            h_start = rim2["index"] + 1
            h_end = min(rim2["index"] + max_handle_bars, len(df) - 1)
            if h_start >= len(df):
                continue

            handle_section = df.loc[h_start:h_end]
            handle_low_price = float(handle_section["low"].min())
            handle_low_idx = int(handle_section["low"].idxmin())

            # Handle must show a real pullback below the neckline
            if handle_low_price >= neckline * 0.995:
                continue

            # Handle low must not retrace more than 50% of cup depth
            max_allowed_low = neckline - max_handle_retrace * cup_depth_abs
            if handle_low_price < max_allowed_low:
                continue

            # At least min_handle_bars must elapse before the handle low
            if handle_low_idx - rim2["index"] < min_handle_bars:
                continue

            # Breakout: first close above neckline after the handle low
            brk = _confirm_break(df, neckline, handle_low_idx)
            if not brk:
                continue

            # Volume: expansion at breakout bar (soft — skip if unavailable)
            if vol_avg is not None:
                avg_at_brk = (
                    vol_avg.iloc[brk["break_index"]]
                    if brk["break_index"] < len(vol_avg)
                    else None
                )
                vb = float(df["volume"].iloc[brk["break_index"]])
                if avg_at_brk and vb < avg_at_brk:
                    continue

            patterns.append(
                {
                    "type": "cup_and_handle",
                    "neckline": neckline,
                    "stop_basis": handle_low_price,
                    "break_index": brk["break_index"],
                    "break_time": brk["break_time"],
                    "cup_depth_abs": round(cup_depth_abs, 4),
                    "target_measured": round(neckline + cup_depth_abs, 4),
                    "pivots": {
                        "rim1": rim1,
                        "rim2": rim2,
                        "cup_bottom": cup_bottom_price,
                        "handle_low": handle_low_price,
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
