"""Fetch market data via yfinance (no auth required).

Replaces the Robinhood robin_stocks fetcher for automated CI runs.
Produces identical per-symbol JSON files: data/<SYM>_day.json and
data/<SYM>_4hour.json, in the same bar dict format the rest of the
pipeline expects.

Usage:
  python3 -m swing_agent.fetch_yf --daily
  python3 -m swing_agent.fetch_yf --intraday
  python3 -m swing_agent.fetch_yf --quotes
  python3 -m swing_agent.fetch_yf --all
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DAY_PERIOD  = "2y"    # ~2 years of daily bars
H1_PERIOD   = "60d"   # 60 days of 1-hour bars (resampled to 4-hour); yfinance 1.x limit
BATCH_SIZE  = 20      # yfinance handles multi-symbol downloads well


def _load_universe() -> list[str]:
    return (DATA / "universe.txt").read_text().split()


def _load_open_symbols() -> list[str]:
    ledger = DATA / "paper_trades_live.json"
    if not ledger.exists():
        return []
    trades = json.loads(ledger.read_text())
    return sorted({t["symbol"] for t in trades if t.get("status") == "entered"})


def _df_to_bars(df) -> list[dict]:
    """Convert a yfinance DataFrame to the bar dict list format."""
    bars = []
    for ts, row in df.iterrows():
        # ts is a pandas Timestamp; normalize to UTC ISO string
        if hasattr(ts, "isoformat"):
            if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
                # Timezone-aware: convert to UTC then format
                begins_at = ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
            else:
                begins_at = ts.isoformat()
                if not begins_at.endswith("Z"):
                    begins_at += "Z"
        else:
            begins_at = str(ts) + "Z"

        bars.append({
            "begins_at":   begins_at,
            "open_price":  str(round(float(row["Open"]),  4)),
            "high_price":  str(round(float(row["High"]),  4)),
            "low_price":   str(round(float(row["Low"]),   4)),
            "close_price": str(round(float(row["Close"]), 4)),
            "volume":      int(row["Volume"]) if "Volume" in row else 0,
            "interpolated": False,
        })
    return bars


def _resample_1h_to_4h(bars: list[dict]) -> list[dict]:
    """Aggregate 1-hour bars into 4-hour buckets aligned to RTH (9:30 ET)."""
    from collections import defaultdict
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for bar in bars:
        ts_str = bar.get("begins_at", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError:
            continue
        # Convert UTC to US/Eastern accounting for EST (UTC-5) vs EDT (UTC-4)
        et = ts.astimezone(ET)
        et_hour   = et.hour
        et_minute = et.minute
        mins_since_open = (et_hour - 9) * 60 + (et_minute - 30)
        if mins_since_open < 0 or mins_since_open >= 390:
            continue  # outside RTH
        bucket_idx = mins_since_open // 240
        date_str = ts.strftime("%Y-%m-%d")
        buckets[(date_str, bucket_idx)].append(bar)

    result = []
    for (date_str, _idx), group in sorted(buckets.items()):
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


def fetch_daily(symbols: list[str]) -> dict[str, list[dict]]:
    import yfinance as yf
    results = {}
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i : i + BATCH_SIZE]
        tickers = " ".join(batch)
        try:
            df = yf.download(tickers, period=DAY_PERIOD, interval="1d",
                             group_by="ticker", auto_adjust=True, progress=False,
                             threads=True)
        except Exception as e:
            print(f"  WARNING: batch download failed: {e}", file=sys.stderr)
            continue

        for sym in batch:
            try:
                sym_df = df[sym].dropna() if len(batch) > 1 else df.dropna()
                bars = _df_to_bars(sym_df)
                if bars:
                    results[sym] = bars
            except Exception as e:
                print(f"  WARNING: {sym} daily failed: {e}", file=sys.stderr)

    return results


def fetch_4hour(symbols: list[str]) -> dict[str, list[dict]]:
    """Fetch 1-hour bars (60m interval) per symbol and resample to 4-hour.

    Downloads one symbol at a time to avoid yfinance 1.x multi-ticker column
    structure issues with intraday intervals.
    """
    import yfinance as yf
    import pandas as pd
    results = {}
    for sym in symbols:
        try:
            df = yf.download(sym, period=H1_PERIOD, interval="60m",
                             auto_adjust=True, progress=False)
            if df is None or df.empty:
                continue
            # Flatten MultiIndex columns if present (yfinance 1.x single-ticker)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            bars_1h = _df_to_bars(df.dropna())
            bars_4h = _resample_1h_to_4h(bars_1h)
            if bars_4h:
                results[sym] = bars_4h
        except Exception as e:
            print(f"  WARNING: {sym} 4h failed: {e}", flush=True)

    return results


def fetch_quotes(symbols: list[str]) -> dict[str, float]:
    """Return {symbol: last_price} using yfinance fast_info."""
    import yfinance as yf
    quotes = {}
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            price = t.fast_info.get("lastPrice") or t.fast_info.get("previousClose")
            if price:
                quotes[sym] = round(float(price), 4)
        except Exception as e:
            print(f"  WARNING: {sym} quote failed: {e}", file=sys.stderr)
    return quotes


def save(data: dict[str, list[dict]], suffix: str) -> list[str]:
    saved = []
    for sym, bars in data.items():
        if not bars:
            continue
        out = DATA / f"{sym}{suffix}.json"
        out.write_text(json.dumps(bars))
        saved.append(sym)
    return saved


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if not args or not (args & {"--daily", "--intraday", "--quotes", "--all"}):
        print("Usage: python3 -m swing_agent.fetch_yf [--daily] [--intraday] [--quotes] [--all]")
        sys.exit(1)

    do_daily    = "--all" in args or "--daily"    in args
    do_intraday = "--all" in args or "--intraday" in args
    do_quotes   = "--all" in args or "--quotes"   in args

    if do_daily:
        print("Fetching daily bars for all symbols...")
        syms = _load_universe()
        data = fetch_daily(syms)
        saved = save(data, "_day")
        print(f"  Saved {len(saved)} daily files.")

    if do_intraday:
        print("Fetching 4-hour bars for all symbols...")
        syms = _load_universe()
        data = fetch_4hour(syms)
        saved = save(data, "_4hour")
        print(f"  Saved {len(saved)} 4-hour files.")

    if do_quotes:
        syms = _load_open_symbols()
        if syms:
            print(f"Fetching live quotes for: {syms}")
            quotes = fetch_quotes(syms)
            print(json.dumps(quotes, indent=2))
        else:
            print("No open positions.")
