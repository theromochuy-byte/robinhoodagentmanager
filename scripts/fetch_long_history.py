#!/usr/bin/env python3
"""Download long daily history (research only) for the backtest universe plus SPY.

Saves split/dividend-adjusted daily bars to data/hist_long/<SYM>_day.json in the same bar
format as data/hist_cache. Research data only: live decisions must use Robinhood prices.

Usage: python scripts/fetch_long_history.py [--start 2014-01-01]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yfinance as yf

from swing_agent.fetch_yf import _df_to_bars

OUT = ROOT / "data" / "hist_long"
BATCH = 20


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--symbols", nargs="*", help="override the universe file")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    universe = ROOT / "data" / "universe.txt"
    symbols = args.symbols or (sorted({l.strip() for l in universe.read_text().splitlines()
                                       if l.strip() and not l.startswith("#")}) + ["SPY"])
    missing = []
    for i in range(0, len(symbols), BATCH):
        batch = symbols[i:i + BATCH]
        df = yf.download(batch, start=args.start, interval="1d", auto_adjust=True,
                         group_by="ticker", progress=False, threads=True)
        for sym in batch:
            try:
                sub = df[sym].dropna(subset=["Close"])
            except KeyError:
                missing.append(sym)
                continue
            if sub.empty:
                missing.append(sym)
                continue
            (out / f"{sym}_day.json").write_text(json.dumps(_df_to_bars(sub)))
        print(f"  {min(i + BATCH, len(symbols))}/{len(symbols)}")
    print(f"Saved {len(symbols) - len(missing)} symbols to {out}; missing: {missing}")


if __name__ == "__main__":
    main()
