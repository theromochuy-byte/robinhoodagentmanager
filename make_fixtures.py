"""Generate validation fixtures in Robinhood MCP get_equity_historicals format.

Builds:
  data/TEST_day.json   : a steady daily uptrend (so the 20 EMA long-bias holds)
  data/TEST_4hour.json : a 4h series containing one clean double bottom and one
                         clean inverse head & shoulders, both breaking their neckline

This exists only to prove the engine end-to-end without pulling large live data
into the planning chat. The live pipeline uses real Robinhood bars.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


def _bar(ts, o, h, l, c, v=1_000_000):
    return {
        "begins_at": ts,
        "open_price": f"{o:.4f}",
        "high_price": f"{h:.4f}",
        "low_price": f"{l:.4f}",
        "close_price": f"{c:.4f}",
        "volume": v,
        "session": "reg",
    }


def _envelope(symbol, interval, bars):
    return {"data": {"results": [{"symbol": symbol, "interval": interval, "bounds": "regular", "bars": bars}]}}


def make_daily():
    """60 business days rising from 100 to ~118; lows stay above the 20 EMA."""
    bars = []
    start = datetime(2026, 1, 2)
    price = 100.0
    d = start
    made = 0
    while made < 60:
        if d.weekday() < 5:
            o = price
            c = price + 0.30
            h = c + 0.25
            l = o - 0.15  # shallow lows keep price above a rising EMA
            bars.append(_bar(d.strftime("%Y-%m-%dT00:00:00Z"), o, h, l, c))
            price = c
            made += 1
        d += timedelta(days=1)
    return _envelope("TEST", "day", bars)


def _series_from_pivots(pivot_values, bars_between=5):
    """Interpolate closes through pivot values. Returns (closes, pivot_index_to_kind)
    where kind is 'low' or 'high' for the bars that sit exactly on a pivot.
    """
    closes = []
    pivot_kind = {}
    for i in range(len(pivot_values) - 1):
        a, b = pivot_values[i], pivot_values[i + 1]
        idx = len(closes)
        if 0 < i < len(pivot_values):
            prev = pivot_values[i - 1]
            if a < prev and a < b:
                pivot_kind[idx] = "low"
            elif a > prev and a > b:
                pivot_kind[idx] = "high"
        for j in range(bars_between):
            closes.append(a + (b - a) * (j / bars_between))
    closes.append(pivot_values[-1])
    return closes, pivot_kind


def make_4h():
    # Pivot path: double bottom, then inverse H&S, on a 4h grid.
    # double bottom: peak100 -> b1=90 -> mid=96 -> b2=90.3 -> breakout 100 -> 103
    # inverse H&S:   -> LS=95 -> p1=98.5 -> head=90 -> p2=99 -> RS=95.6 -> breakout 105
    pivots = [100, 90, 96, 90.3, 100, 103, 95, 98.5, 90, 99, 95.6, 105]
    closes, pivot_kind = _series_from_pivots(pivots, bars_between=5)

    bars = []
    start = datetime(2026, 2, 2, 14, 0)  # well after EMA warms, within uptrend
    t = start
    i = 0
    while i < len(closes):
        if t.weekday() < 5:
            c = closes[i]
            o = closes[i - 1] if i > 0 else c
            h = max(o, c) + 0.3
            l = min(o, c) - 0.3
            kind = pivot_kind.get(i)
            if kind == "low":
                l = c - 1.5  # strict spike low so the pivot is unambiguous
            elif kind == "high":
                h = c + 1.5  # strict spike high
            bars.append(_bar(t.strftime("%Y-%m-%dT%H:00:00Z"), o, h, l, c))
            i += 1
            t = t.replace(hour=18) if t.hour == 14 else (t + timedelta(days=1)).replace(hour=14)
        else:
            t = (t + timedelta(days=1)).replace(hour=14)
    return _envelope("TEST", "4hour", bars)


if __name__ == "__main__":
    DATA.mkdir(exist_ok=True)
    with open(DATA / "TEST_day.json", "w") as f:
        json.dump(make_daily(), f)
    with open(DATA / "TEST_4hour.json", "w") as f:
        json.dump(make_4h(), f)
    print("wrote TEST_day.json and TEST_4hour.json")
