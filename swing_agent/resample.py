"""Resample fine-grained bars (e.g. 10-minute) to 4-hour OHLCV bars.

Used by fetch.py when the broker API doesn't expose a native 4-hour interval.
"""
from __future__ import annotations

from datetime import datetime, timezone


def resample_to_4hour(bars: list[dict]) -> list[dict]:
    """Aggregate a list of bar dicts into 4-hour buckets (RTH-aware).

    Each 4-hour bucket boundary is at HH:MM = 09:30, 13:30 ET (regular hours).
    We bucket by (date, floor(minutes_since_930 / 240)).
    """
    if not bars:
        return []

    from collections import defaultdict

    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for bar in bars:
        ts_str = bar.get("begins_at", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        # Convert to ET offset (UTC-4 summer / UTC-5 winter — approximate with UTC-4)
        et_hour   = (ts.hour - 4) % 24
        et_minute = ts.minute
        mins_since_open = (et_hour - 9) * 60 + (et_minute - 30)
        if mins_since_open < 0 or mins_since_open >= 390:
            continue  # outside regular trading hours
        bucket_idx = mins_since_open // 240
        date_str = ts.strftime("%Y-%m-%d")
        buckets[(date_str, bucket_idx)].append(bar)

    result = []
    for (date_str, bucket_idx), group in sorted(buckets.items()):
        opens  = [float(b["open_price"])  for b in group]
        highs  = [float(b["high_price"])  for b in group]
        lows   = [float(b["low_price"])   for b in group]
        closes = [float(b["close_price"]) for b in group]
        vols   = [int(b.get("volume", 0)) for b in group]
        result.append({
            "begins_at":   group[0]["begins_at"],
            "open_price":  str(opens[0]),
            "high_price":  str(max(highs)),
            "low_price":   str(min(lows)),
            "close_price": str(closes[-1]),
            "volume":      sum(vols),
            "interpolated": False,
        })

    return result
