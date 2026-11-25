"""Pattern detection: bullish reversals (double bottom, inverse HnS, cup & handle, ascending
triangle) and bearish reversals (head & shoulders).

Each detector scans a DataFrame of OHLC bars (entry timeframe) and returns a list
of pattern dicts. A pattern is only emitted once its neckline has been broken in the
pattern's direction (upside for bullish, downside for bearish).

Pattern dict fields (all patterns):
    type            str — pattern identifier
    neckline        float — the breakout/breakdown level
    stop_basis      float — the pivot the stop sits beyond
                      bullish: swing low to place stop below
                      bearish: swing high to place stop above
    break_index     int — bar index where close first cleared the neckline
    break_time      timestamp of that bar
    pivots          dict of raw pivot prices/indices

Additional fields (bearish patterns):
    direction       'short' — signals short entry
    peak1_max       float — highest point in the LS→Head trough (neckline slope reference)
    peak2_max       float — highest point in the Head→RS trough
    ls_to_head_span int — bar distance LS→Head (time symmetry reference)
    head_to_rs_span int — bar distance Head→RS
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
                peak1_max = max(p["price"] for p in peak1)
                peak2_max = max(p["price"] for p in peak2)
                ls_to_head_span = head["index"] - ls["index"]
                head_to_rs_span = rs["index"] - head["index"]
                patterns.append(
                    {
                        "type": "inverse_hns",
                        "neckline": neckline,
                        "stop_basis": rs["price"],
                        "break_index": brk["break_index"],
                        "break_time": brk["break_time"],
                        # Neckline slope: peak2_max >= peak1_max = upward slope (bullish)
                        "peak1_max": peak1_max,
                        "peak2_max": peak2_max,
                        # Time symmetry: head_to_rs_span / ls_to_head_span ideally ~1.0
                        "ls_to_head_span": ls_to_head_span,
                        "head_to_rs_span": head_to_rs_span,
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


def detect_ascending_triangle(
    df: pd.DataFrame,
    left: int = 3,
    right: int = 3,
    flat_tol: float = 0.015,
    min_highs: int = 2,
    min_lows: int = 2,
    min_span: int = 10,
    max_span: int = 100,
) -> list[dict]:
    """Flat upper resistance touched 2+ times with a series of ascending lows.

    flat_tol: max relative spread between resistance highs (1.5% = "flat").
    min_highs: minimum touches at the resistance level (default 2).
    min_lows: minimum higher-low pivots in the pattern span (default 2).
    min_span / max_span: bar range from first resistance touch to break.
    Volume: breakout bar must exceed rolling average when data available.

    stop_basis = the last higher low before the break.
    neckline   = midpoint of the two resistance highs.
    """
    marked = swing_points(df, left, right)
    highs  = pivots(marked, "high")
    lows   = pivots(marked, "low")
    vol_avg = _vol_avg(df)
    patterns = []

    for i in range(len(highs)):
        for j in range(i + 1, len(highs)):
            h1, h2 = highs[i], highs[j]
            span = h2["index"] - h1["index"]
            if span < min_span or span > max_span:
                continue

            resistance = (h1["price"] + h2["price"]) / 2
            if abs(h2["price"] - h1["price"]) / resistance > flat_tol:
                continue

            # At least one low must sit between the two resistance touches
            between_lows = [
                l for l in lows
                if h1["index"] < l["index"] < h2["index"] and l["price"] < resistance
            ]
            if not between_lows:
                continue

            # Confirm breakout above resistance after the second high
            brk = _confirm_break(df, resistance, h2["index"])
            if not brk:
                continue

            # Collect all lows in the full span, below resistance
            span_lows = [
                l for l in lows
                if h1["index"] <= l["index"] < brk["break_index"]
                and l["price"] < resistance
            ]
            if len(span_lows) < min_lows:
                continue

            # Lows must be ascending: last low strictly higher than first
            if span_lows[-1]["price"] <= span_lows[0]["price"]:
                continue

            # Volume: breakout bar above rolling average
            if vol_avg is not None:
                bi = brk["break_index"]
                avg_at_brk = vol_avg.iloc[bi] if bi < len(vol_avg) else None
                vb = float(df["volume"].iloc[bi])
                if avg_at_brk and vb < avg_at_brk:
                    continue

            patterns.append(
                {
                    "type": "ascending_triangle",
                    "neckline": resistance,
                    "stop_basis": span_lows[-1]["price"],
                    "break_index": brk["break_index"],
                    "break_time": brk["break_time"],
                    "pivots": {
                        "resistance_high1": h1,
                        "resistance_high2": h2,
                        "last_higher_low": span_lows[-1],
                        "neckline_price": resistance,
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


def _dedupe_short(patterns: list[dict]) -> list[dict]:
    """Like _dedupe but for short patterns: keep the lowest stop_basis (tightest stop above)."""
    by_break: dict[int, dict] = {}
    for p in patterns:
        bi = p["break_index"]
        if bi not in by_break or p["stop_basis"] < by_break[bi]["stop_basis"]:
            by_break[bi] = p
    return [by_break[k] for k in sorted(by_break)]


def _confirm_breakdown(df: pd.DataFrame, neckline: float, after_index: int) -> dict | None:
    """Find the first bar after `after_index` whose close falls below the neckline."""
    for i in range(after_index + 1, len(df)):
        if df.loc[i, "close"] < neckline:
            return {"break_index": int(i), "break_time": df.loc[i, "begins_at"]}
    return None


def _volume_confirms_hns(
    df: pd.DataFrame,
    ls_idx: int,
    head_idx: int,
    rs_idx: int,
    break_idx: int,
    vol_avg: pd.Series,
) -> bool:
    """Volume signature for bearish head & shoulders.

    Passes when ALL of the following hold (any check skipped if volume unavailable):
      1. Head volume >= left-shoulder volume (buying climax / exhaustion at head).
      2. Right-shoulder volume < head volume (buyers diminishing, distribution).
      3. Breakdown volume > rolling average (sellers confirm the break).
    """
    v_ls   = _vol_around(df, ls_idx)
    v_head = _vol_around(df, head_idx)
    v_rs   = _vol_around(df, rs_idx)
    v_brk  = float(df["volume"].iloc[break_idx])
    avg_at_brk = vol_avg.iloc[break_idx] if break_idx < len(vol_avg) else None

    if v_head < v_ls:
        return False
    if v_rs >= v_head:
        return False
    if avg_at_brk and v_brk < avg_at_brk:
        return False
    return True


def detect_hns(
    df: pd.DataFrame,
    left: int = 3,
    right: int = 3,
    shoulder_tol: float = 0.06,
    min_sep: int = 3,
    max_span: int = 90,
) -> list[dict]:
    """Bearish head & shoulders: left shoulder, higher head, lower right shoulder.

    Neckline connects the two troughs between the shoulders (trough1: LS→Head,
    trough2: Head→RS). Confirmation is a close below the neckline.

    shoulder_tol: max relative difference between the two shoulder highs.
    min_sep: minimum bars between each pivot pair.
    max_span: max bars from left shoulder to right shoulder.

    stop_basis = right shoulder high (stop placed above it + 1×ATR buffer).
    neckline   = minimum of the two intervening trough lows.
    direction  = 'short'

    Volume signature: head volume >= LS (buying climax), RS volume < head
    (distribution), breakdown volume > rolling average (confirmation).
    """
    marked  = swing_points(df, left, right)
    highs   = pivots(marked, "high")
    lows    = pivots(marked, "low")
    vol_avg = _vol_avg(df)
    patterns = []

    for i in range(len(highs)):
        for j in range(i + 1, len(highs)):
            for k in range(j + 1, len(highs)):
                ls, head, rs = highs[i], highs[j], highs[k]

                if rs["index"] - ls["index"] > max_span:
                    continue
                if (head["index"] - ls["index"]) < min_sep:
                    continue
                if (rs["index"] - head["index"]) < min_sep:
                    continue

                # Head must be the highest peak
                if not (head["price"] > ls["price"] and head["price"] > rs["price"]):
                    continue

                # Shoulders roughly comparable in height
                if abs(rs["price"] - ls["price"]) / ls["price"] > shoulder_tol:
                    continue

                # Neckline = the two troughs: trough1 between LS and Head, trough2 between Head and RS
                trough1 = [l for l in lows if ls["index"] < l["index"] < head["index"]]
                trough2 = [l for l in lows if head["index"] < l["index"] < rs["index"]]
                if not trough1 or not trough2:
                    continue

                # Neckline at the higher of the two troughs (conservative — requires a real break)
                neckline = max(
                    min(t["price"] for t in trough1),
                    min(t["price"] for t in trough2),
                )

                # Right shoulder must be strictly above the neckline (stop has to be above entry)
                if rs["price"] <= neckline:
                    continue

                # Confirm breakdown: first close below neckline after right shoulder
                brk = _confirm_breakdown(df, neckline, rs["index"])
                if not brk:
                    continue

                # Volume: climax at head, exhaustion at RS, expansion on breakdown
                if vol_avg is not None:
                    if not _volume_confirms_hns(
                        df, ls["index"], head["index"], rs["index"],
                        brk["break_index"], vol_avg,
                    ):
                        continue

                trough1_min = min(t["price"] for t in trough1)
                trough2_min = min(t["price"] for t in trough2)
                ls_to_head_span = head["index"] - ls["index"]
                head_to_rs_span = rs["index"] - head["index"]

                patterns.append({
                    "type":            "hns",
                    "direction":       "short",
                    "neckline":        neckline,
                    "stop_basis":      rs["price"],   # stop goes ABOVE this
                    "break_index":     brk["break_index"],
                    "break_time":      brk["break_time"],
                    "trough1_min":     trough1_min,
                    "trough2_min":     trough2_min,
                    "ls_to_head_span": ls_to_head_span,
                    "head_to_rs_span": head_to_rs_span,
                    "pivots": {
                        "left_shoulder":  ls,
                        "head":           head,
                        "right_shoulder": rs,
                        "neckline_price": neckline,
                    },
                })

    return _dedupe_short(patterns)


def daily_bias_short(df: pd.DataFrame, ema_period: int = 20) -> bool:
    """Short bias when the most recent daily bar sits fully below the 20 EMA
    (no touch) and the recent swing-high structure has broken (lower highs confirmed).
    Mirror of daily_bias_long for bearish setups.
    """
    from .indicators import ema

    e = ema(df["close"], ema_period)
    last = len(df) - 1
    if df.loc[last, "high"] >= e.iloc[last]:
        return False
    # structure: last confirmed swing high should be lower than the one before it
    marked = swing_points(df)
    highs = pivots(marked, "high")
    if len(highs) >= 2:
        if highs[-1]["price"] >= highs[-2]["price"]:
            return False
    return True


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
