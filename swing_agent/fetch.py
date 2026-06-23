"""Fetch market data from Robinhood and write per-symbol data files.

Replaces the manual MCP call workflow for automated (GitHub Actions) runs.
Uses robin_stocks to call the same Robinhood API endpoints.

Usage:
  python3 -m swing_agent.fetch --daily      # refresh all daily bars
  python3 -m swing_agent.fetch --intraday   # refresh all 4-hour bars
  python3 -m swing_agent.fetch --quotes     # fetch quotes for open positions
  python3 -m swing_agent.fetch --all        # daily + intraday + quotes
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DAY_START   = "2025-06-20"   # ~12 months of daily bars
H4_SPAN     = 90             # days of 4-hour bars (~3 months)

BATCH_SIZE  = 10
RATE_DELAY  = 0.5            # seconds between batches


def _login():
    """Authenticate with Robinhood. Reads credentials from env vars."""
    try:
        import robin_stocks.robinhood as rh
    except ImportError:
        print("robin_stocks not installed. Run: pip install robin-stocks", file=sys.stderr)
        sys.exit(1)

    username = os.environ.get("ROBINHOOD_USERNAME")
    password = os.environ.get("ROBINHOOD_PASSWORD")
    mfa_code = os.environ.get("ROBINHOOD_MFA_CODE")   # optional: TOTP code
    totp_key = os.environ.get("ROBINHOOD_TOTP_KEY")   # optional: base32 TOTP secret

    if not username or not password:
        print("Set ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD env vars.", file=sys.stderr)
        sys.exit(1)

    # If a TOTP key is provided, generate a fresh code automatically
    if totp_key and not mfa_code:
        import pyotp
        mfa_code = pyotp.TOTP(totp_key).now()

    kwargs = dict(username=username, password=password, store_session=False)
    if mfa_code:
        kwargs["mfa_code"] = mfa_code

    rh.login(**kwargs)
    return rh


def _load_universe() -> list[str]:
    return (DATA / "universe.txt").read_text().split()


def _load_open_symbols() -> list[str]:
    ledger = DATA / "paper_trades_live.json"
    if not ledger.exists():
        return []
    trades = json.loads(ledger.read_text())
    return sorted({t["symbol"] for t in trades if t.get("status") == "entered"})


def _bar_to_dict(bar: dict) -> dict:
    """Normalise a robin_stocks bar into the same shape dataio.py expects."""
    return {
        "begins_at":    bar.get("begins_at", bar.get("begins_at_date", "")),
        "open_price":   str(bar["open_price"]),
        "high_price":   str(bar["high_price"]),
        "low_price":    str(bar["low_price"]),
        "close_price":  str(bar["close_price"]),
        "volume":       int(bar.get("volume", 0)),
        "interpolated": bar.get("interpolated", False),
    }


def fetch_historicals(rh, symbols: list[str], interval: str, span: str) -> dict[str, list]:
    """Fetch OHLC bars for a list of symbols. Returns {symbol: [bar, ...]}."""
    import robin_stocks.robinhood as rh_module
    results = {}
    for i in range(0, len(symbols), BATCH_SIZE):
        batch = symbols[i : i + BATCH_SIZE]
        for sym in batch:
            try:
                bars = rh_module.stocks.get_stock_historicals(
                    sym, interval=interval, span=span, bounds="regular"
                )
                if bars:
                    results[sym] = [_bar_to_dict(b) for b in bars]
            except Exception as e:
                print(f"  WARNING: {sym} failed: {e}", file=sys.stderr)
        if i + BATCH_SIZE < len(symbols):
            time.sleep(RATE_DELAY)
    return results


def save_historicals(data: dict[str, list], suffix: str) -> list[str]:
    saved = []
    for sym, bars in data.items():
        if not bars:
            continue
        out = DATA / f"{sym}{suffix}.json"
        out.write_text(json.dumps(bars))
        saved.append(sym)
    return saved


def fetch_and_save_daily(rh) -> list[str]:
    print("Fetching daily bars for all 188 symbols...")
    syms = _load_universe()
    data = fetch_historicals(rh, syms, interval="day", span="year")
    saved = save_historicals(data, "_day")
    print(f"  Saved {len(saved)} daily files.")
    return saved


def fetch_and_save_4hour(rh) -> list[str]:
    print("Fetching 4-hour bars for all 188 symbols...")
    syms = _load_universe()
    data = fetch_historicals(rh, syms, interval="10minute", span="3month")
    # robin_stocks doesn't expose a 4hour interval directly;
    # resample 10-minute bars to 4-hour in place
    from swing_agent.resample import resample_to_4hour
    resampled = {sym: resample_to_4hour(bars) for sym, bars in data.items()}
    saved = save_historicals(resampled, "_4hour")
    print(f"  Saved {len(saved)} 4-hour files.")
    return saved


def fetch_quotes(rh, symbols: list[str]) -> dict[str, float]:
    """Return {symbol: last_price} for the given symbols."""
    import robin_stocks.robinhood as rh_module
    quotes = rh_module.stocks.get_latest_price(symbols)
    result = {}
    for sym, price in zip(symbols, quotes):
        try:
            result[sym] = float(price)
        except (TypeError, ValueError):
            pass
    return result


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if not args or not (args & {"--daily", "--intraday", "--quotes", "--all"}):
        print("Usage: python3 -m swing_agent.fetch [--daily] [--intraday] [--quotes] [--all]")
        sys.exit(1)

    do_daily    = "--all" in args or "--daily" in args
    do_intraday = "--all" in args or "--intraday" in args
    do_quotes   = "--all" in args or "--quotes" in args

    rh = _login()

    if do_daily:
        fetch_and_save_daily(rh)

    if do_intraday:
        fetch_and_save_4hour(rh)

    if do_quotes:
        syms = _load_open_symbols()
        if syms:
            print(f"Fetching live quotes for: {syms}")
            quotes = fetch_quotes(rh, syms)
            print(json.dumps(quotes, indent=2))
        else:
            print("No open positions.")
