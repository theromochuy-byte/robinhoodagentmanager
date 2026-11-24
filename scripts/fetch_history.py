#!/usr/bin/env python3
"""Fetch historical OHLC data from Robinhood MCP and cache locally.

Run this from a Claude Code session that has Robinhood MCP access.
The session calls get_equity_historicals, pipes the JSON response here,
and this script saves it to data/hist_cache/{sym}_{interval}.json.

Usage (from within Claude session via Bash):
    python scripts/fetch_history.py --write <symbol> <interval> <json_file>
    python scripts/fetch_history.py --status          # show what's cached

The backtest script reads from data/hist_cache/ automatically.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "hist_cache"
CACHE.mkdir(parents=True, exist_ok=True)


def write_bars(symbol: str, interval: str, raw_json: dict | list) -> Path:
    """Save raw MCP response to cache. raw_json is the full MCP result or bar list."""
    out = CACHE / f"{symbol}_{interval}.json"

    # Normalise: extract bar list regardless of envelope shape
    if isinstance(raw_json, list):
        bars = raw_json
    elif "bars" in raw_json:
        bars = raw_json["bars"]
    elif "data" in raw_json and "results" in raw_json["data"]:
        for r in raw_json["data"]["results"]:
            if r.get("symbol") == symbol:
                bars = r["bars"]
                break
        else:
            bars = raw_json["data"]["results"][0]["bars"]
    elif "results" in raw_json:
        for r in raw_json["results"]:
            if r.get("symbol") == symbol:
                bars = r["bars"]
                break
        else:
            bars = raw_json["results"][0]["bars"]
    else:
        raise ValueError(f"Cannot find bars in payload for {symbol}")

    # Drop interpolated gap-fills, keep real bars only
    real = [b for b in bars if not b.get("interpolated")]
    payload = {
        "symbol": symbol,
        "interval": interval,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "bar_count": len(real),
        "bars": real,
    }
    with open(out, "w") as f:
        json.dump(payload, f)
    return out


def status() -> None:
    files = sorted(CACHE.glob("*.json"))
    if not files:
        print("No cached data yet.")
        return
    print(f"{'Symbol':10} {'Interval':8} {'Bars':6}  {'From':12} {'To':12}  Age")
    print("-" * 65)
    now = datetime.now(timezone.utc)
    for fp in files:
        try:
            with open(fp) as f:
                d = json.load(f)
            sym = d.get("symbol", fp.stem.split("_")[0])
            iv  = d.get("interval", "?")
            bars = d.get("bars", [])
            n   = len(bars)
            frm = bars[0]["begins_at"][:10] if bars else "?"
            to  = bars[-1]["begins_at"][:10] if bars else "?"
            fetched = datetime.fromisoformat(d.get("fetched_at", now.isoformat()).replace("Z", "+00:00"))
            age_h = round((now - fetched).total_seconds() / 3600, 1)
            print(f"{sym:10} {iv:8} {n:6}  {frm:12} {to:12}  {age_h}h ago")
        except Exception as e:
            print(f"  {fp.name}: ERROR {e}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="show cached files")
    ap.add_argument("--write", nargs=3, metavar=("SYMBOL", "INTERVAL", "JSON_FILE"),
                    help="write MCP response to cache")
    args = ap.parse_args()

    if args.status:
        status()
        return

    if args.write:
        sym, iv, jpath = args.write
        with open(jpath) as f:
            raw = json.load(f)
        out = write_bars(sym, iv, raw)
        print(f"Saved {out}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
