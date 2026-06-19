"""Load Robinhood MCP get_equity_historicals JSON into clean OHLC DataFrames.

The agent (in Claude Code) calls get_equity_historicals, saves the raw JSON to
data/<SYMBOL>_<interval>.json, and the backtest reads it here. This module does
no network I/O, which keeps the analytics testable and offline.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def bars_to_df(raw: dict, symbol: str | None = None) -> pd.DataFrame:
    """Convert one get_equity_historicals payload into a DataFrame.

    Accepts either the full {"data": {"results": [...]}} envelope or a single
    result dict. Drops interpolated gap-fill bars.
    """
    results = raw.get("data", {}).get("results") if "data" in raw else raw.get("results")
    if results is None and "bars" in raw:
        result = raw
    else:
        result = None
        for r in results:
            if symbol is None or r.get("symbol") == symbol:
                result = r
                break
        if result is None:
            result = results[0]

    rows = []
    for bar in result["bars"]:
        if bar.get("interpolated"):
            continue
        rows.append(
            {
                "begins_at": bar["begins_at"],
                "open": float(bar["open_price"]),
                "high": float(bar["high_price"]),
                "low": float(bar["low_price"]),
                "close": float(bar["close_price"]),
                "volume": int(bar["volume"]),
            }
        )
    df = pd.DataFrame(rows).reset_index(drop=True)
    return df


def load(path: str | Path, symbol: str | None = None) -> pd.DataFrame:
    with open(path) as f:
        raw = json.load(f)
    return bars_to_df(raw, symbol)
