"""Live forward scanner — paper trading only, no real orders.

Scans all universe symbols for active retest setups and logs proposed
paper trades to data/paper_trades_live.json. Run once per day after close.

A setup is "live" when:
  1. A qualifying pattern broke its neckline within the last 12 4h bars.
  2. Daily bias is long at the time of the break.
  3. Pattern depth >= 3%.
  4. The retest has NOT yet triggered (still watching) OR just triggered today.

Outputs:
  data/paper_trades_live.json  — all open paper positions + today's new entries
  reports/scan_<date>.json     — today's watchlist + triggered entries
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .backtest import daily_bias_series, bias_asof
from .dataio import load
from .indicators import atr
from .patterns import detect_double_bottom, detect_inverse_hns
from .simulator import build_trade

ROOT        = Path(__file__).resolve().parent.parent
DATA        = ROOT / "data"
REPORTS     = ROOT / "reports"
LIVE_LEDGER = DATA / "paper_trades_live.json"
EQUITY_FILE = DATA / "equity.json"

STARTING_EQUITY = 1500.0


def _load_equity() -> dict:
    """Load equity state, creating it from scratch if missing."""
    if EQUITY_FILE.exists():
        return json.loads(EQUITY_FILE.read_text())
    return {"starting_equity": STARTING_EQUITY, "capital_in_use": 0.0}


def _save_equity(state: dict) -> None:
    EQUITY_FILE.write_text(json.dumps(state, indent=2))


def _recompute_equity() -> dict:
    """Recompute capital_in_use from the live ledger and save."""
    ledger = _load_live_ledger()
    in_use = sum(
        t["entry"] * t.get("shares", 0)
        for t in ledger
        if t.get("status") == "entered"
    )
    state = _load_equity()
    state["capital_in_use"] = round(in_use, 2)
    state["available_equity"] = round(state["starting_equity"] - in_use, 2)
    _save_equity(state)
    return state


def _load_live_ledger() -> list[dict]:
    if LIVE_LEDGER.exists():
        return json.loads(LIVE_LEDGER.read_text())
    return []


def _save_live_ledger(trades: list[dict]) -> None:
    LIVE_LEDGER.write_text(json.dumps(trades, indent=2))


def scan_symbol(symbol: str, equity: float, risk_pct: float = 0.02) -> dict:
    """Scan one symbol. Returns dict with 'watching' and 'triggered' lists."""
    daily_path = DATA / f"{symbol}_day.json"
    h4_path    = DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not h4_path.exists():
        return {"watching": [], "triggered": []}

    daily = load(daily_path, symbol)
    h4    = load(h4_path, symbol)
    if len(daily) < 30 or len(h4) < 30:
        return {"watching": [], "triggered": []}

    bias       = daily_bias_series(daily)
    atr_series = atr(h4, 14)
    patterns   = detect_double_bottom(h4) + detect_inverse_hns(h4)
    patterns.sort(key=lambda p: p["break_index"])

    last_bar   = len(h4) - 1
    neckline   = None
    watching   = []
    triggered  = []

    for p in patterns:
        bi = p["break_index"]
        # only patterns whose break is within the last 12 bars
        if bi < last_bar - 11:
            continue
        if not bias_asof(bias, p["break_time"]):
            continue
        if (p["neckline"] - p["stop_basis"]) / p["neckline"] < 0.03:
            continue

        trade = build_trade(h4, p, atr_series, equity, risk_pct)
        if trade is None:
            continue

        neckline   = p["neckline"]
        bars_since = last_bar - bi
        last_close = float(h4.loc[last_bar, "close"])
        last_low   = float(h4.loc[last_bar, "low"])
        last_open  = float(h4.loc[last_bar, "open"])

        # check if we're in a pullback zone already
        in_pullback = any(
            float(h4.loc[i, "low"]) <= neckline * 1.005
            for i in range(bi + 1, last_bar + 1)
        )

        # check if today's bar IS the retest trigger
        triggered_today = (
            in_pullback
            and last_close >= neckline
            and last_close > last_open
        )

        setup = {
            "symbol":        symbol,
            "type":          p["type"],
            "neckline":      round(neckline, 4),
            "stop":          round(trade["stop"], 4),
            "target_2R":     round(trade["target_2R"], 4),
            "risk_per_share": round(trade["risk_per_share"], 4),
            "shares":        round(trade["shares"], 4),
            "break_time":    str(p["break_time"]),
            "bars_since_break": bars_since,
            "in_pullback":   in_pullback,
            "last_close":    round(last_close, 4),
            "last_bar_time": str(h4.loc[last_bar, "begins_at"]),
            "scanned_at":    datetime.now(timezone.utc).isoformat(),
        }

        if triggered_today:
            setup["entry"]       = round(last_close, 4)
            setup["entry_time"]  = str(h4.loc[last_bar, "begins_at"])
            setup["status"]      = "entered"
            triggered.append(setup)
        else:
            setup["status"] = "watching"
            watching.append(setup)

    return {"watching": watching, "triggered": triggered}


def run_scan(symbols: list[str], risk_pct: float = 0.02) -> dict:
    REPORTS.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Recompute equity state from ledger before scanning
    equity_state = _recompute_equity()
    available    = equity_state["available_equity"]
    starting     = equity_state["starting_equity"]
    print(f"  Equity: ${starting:.2f} starting  |  "
          f"${equity_state['capital_in_use']:.2f} in use  |  "
          f"${available:.2f} available")

    all_watching  = []
    all_triggered = []

    for sym in symbols:
        # Use full starting equity for position sizing (risk % of starting capital)
        # but gate entry on available equity
        result = scan_symbol(sym, starting, risk_pct)
        all_watching.extend(result["watching"])
        all_triggered.extend(result["triggered"])

    # Merge triggered entries — only add if we have enough capital
    ledger = _load_live_ledger()
    existing_keys = {(t["symbol"], t.get("entry_time")) for t in ledger}
    new_entries   = []
    skipped       = []

    for t in all_triggered:
        if (t["symbol"], t.get("entry_time")) in existing_keys:
            continue
        cost = round(t["entry"] * t.get("shares", 0), 2)
        if cost > available:
            skipped.append({"symbol": t["symbol"], "type": t["type"],
                            "cost": cost, "available": round(available, 2)})
            continue
        new_entries.append(t)
        available = round(available - cost, 2)  # reserve capital as we add

    ledger.extend(new_entries)
    _save_live_ledger(ledger)

    # Recompute and save final equity state
    final_state = _recompute_equity()

    report = {
        "scan_date":       today,
        "watching":        sorted(all_watching, key=lambda x: x["bars_since_break"]),
        "triggered_today": all_triggered,
        "new_entries":     len(new_entries),
        "skipped_no_capital": skipped,
        "total_open":      len([t for t in ledger if t.get("status") == "entered"]),
        "equity": {
            "starting":   final_state["starting_equity"],
            "in_use":     final_state["capital_in_use"],
            "available":  final_state["available_equity"],
        },
    }
    report_path = REPORTS / f"scan_{today}.json"
    report_path.write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    from swing_agent.fetch_yf import _load_universe, fetch_daily, fetch_4hour, save as yf_save

    syms = _load_universe()
    print("=== DATA REFRESH ===")
    daily_data = fetch_daily(syms)
    yf_save(daily_data, "_day")
    h4_data = fetch_4hour(syms)
    yf_save(h4_data, "_4hour")
    print(f"  Refreshed {len(syms)} symbols\n")

    result = run_scan(syms)

    print(f"\n{'='*60}")
    print(f"SCAN: {result['scan_date']}  |  {len(syms)} symbols")
    print(f"{'='*60}")
    print(f"Watching (retest pending):  {len(result['watching'])}")
    print(f"Triggered today (entered):  {len(result['triggered_today'])}")
    print(f"Total open paper positions: {result['total_open']}")

    if result["watching"]:
        print(f"\n--- WATCHLIST (neckline retest pending) ---")
        for s in result["watching"]:
            pullback = "IN ZONE" if s["in_pullback"] else "waiting"
            print(f"  {s['symbol']:<6} {s['type']:<14} "
                  f"neckline={s['neckline']} stop={s['stop']} 2R={s['target_2R']} "
                  f"bars_since_break={s['bars_since_break']} [{pullback}]")

    if result["triggered_today"]:
        print(f"\n--- ENTRIES TODAY ---")
        for s in result["triggered_today"]:
            print(f"  {s['symbol']:<6} {s['type']:<14} "
                  f"entry={s['entry']} stop={s['stop']} 2R={s['target_2R']} "
                  f"shares={s['shares']}")
    else:
        print(f"\n  No entries triggered today.")
