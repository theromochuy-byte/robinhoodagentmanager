"""
test_filters.py — Backtest filter comparison script.

Runs 5 independent filter variants against the baseline and prints a summary table.
Does NOT modify any source files. Uses monkey-patching to apply each filter inline.
Places NO live orders.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Bootstrap path
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from swing_agent import backtest as bt_module
from swing_agent import indicators as ind_module
from swing_agent.dataio import load
from swing_agent.indicators import atr, ema
from swing_agent.patterns import detect_double_bottom, detect_inverse_hns
from swing_agent.simulator import (
    build_retest_trade,
    build_trade,
    compound_ledger,
    resolve_trade,
    summarize,
)

# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
UNIVERSE_PATH = ROOT / "data" / "universe.txt"
SYMBOLS = [s.strip() for s in UNIVERSE_PATH.read_text().splitlines() if s.strip()]

EQUITY = 1500.0
RISK_PCT = 0.02

DATA = ROOT / "data"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_all(symbols, run_symbol_fn):
    """Collect trades across symbols, compound, and summarize."""
    all_trades = []
    for sym in symbols:
        all_trades.extend(run_symbol_fn(sym, EQUITY, RISK_PCT))

    all_trades.sort(key=lambda t: t["entry_time"])
    all_trades = compound_ledger(all_trades, EQUITY, RISK_PCT)

    summary = summarize(all_trades)
    closed = [t for t in all_trades if t["outcome"] != "open"]
    final_equity = round(EQUITY + sum(t["pnl_dollars"] for t in closed), 2)
    gain_pct = round((final_equity - EQUITY) / EQUITY * 100, 2)
    return {
        "trades": summary["trades"],
        "win_rate": round(summary["win_rate"] * 100, 1),
        "expectancy_R": round(summary["expectancy_R"], 3),
        "total_R": round(summary["total_R"], 2),
        "final_equity": final_equity,
        "gain_pct": gain_pct,
        "open_trades": summary["open_trades"],
    }


def _load_intraday(symbol):
    p = DATA / f"{symbol}_4hour.json"
    return load(p, symbol) if p.exists() else None


def _load_daily(symbol):
    p = DATA / f"{symbol}_day.json"
    return load(p, symbol) if p.exists() else None


# ---------------------------------------------------------------------------
# BASELINE — original run_symbol
# ---------------------------------------------------------------------------

def baseline_run_symbol(symbol, equity, risk_pct):
    return bt_module.run_symbol(symbol, equity, risk_pct)


# ---------------------------------------------------------------------------
# FILTER 1 — Retest-only entries (drop entry_a / breakout trades)
# ---------------------------------------------------------------------------

def filter1_run_symbol(symbol, equity, risk_pct):
    daily_path = DATA / f"{symbol}_day.json"
    intraday_path = DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not intraday_path.exists():
        return []

    daily = load(daily_path, symbol)
    intraday = load(intraday_path, symbol)
    if len(daily) < 30 or len(intraday) < 30:
        return []

    bias = bt_module.daily_bias_series(daily)
    atr_series = atr(intraday, 14)

    patterns = detect_double_bottom(intraday) + detect_inverse_hns(intraday)
    patterns.sort(key=lambda p: p["break_index"])

    trades = []
    open_until: dict[str, int] = {}
    for p in patterns:
        if not bt_module.bias_asof(bias, p["break_time"]):
            continue
        ptype = p["type"]
        if p["break_index"] <= open_until.get(ptype, -1):
            continue
        trade = build_trade(intraday, p, atr_series, equity, risk_pct)
        if trade is None:
            continue
        # track slot using a dummy resolved_a (not appended)
        entry_a_dummy = dict(trade)
        entry_a_dummy["entry_style"] = "breakout"
        resolved_a = resolve_trade(intraday, entry_a_dummy, target_key="target_2R")
        if resolved_a["outcome"] == "open":
            open_until[ptype] = len(intraday)
        else:
            idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
            open_until[ptype] = idx[0] if idx else len(intraday)

        # Only append retest (Entry B)
        trade_b = build_retest_trade(intraday, trade, atr_series)
        if trade_b is not None:
            resolved_b = resolve_trade(intraday, trade_b, target_key="target_2R")
            resolved_b["symbol"] = symbol
            trades.append(resolved_b)

    return trades


# ---------------------------------------------------------------------------
# FILTER 2 — Rising EMA slope (EMA now > EMA 5 bars ago)
# ---------------------------------------------------------------------------

def _daily_bias_rising_ema(daily, ema_period=20):
    e = ema(daily["close"], ema_period)
    slope_ok = e > e.shift(5)
    bias = (daily["close"] > e) & (daily["low"] > e) & slope_ok
    return pd.Series(bias.values, index=pd.to_datetime(daily["begins_at"]))


def filter2_run_symbol(symbol, equity, risk_pct):
    daily_path = DATA / f"{symbol}_day.json"
    intraday_path = DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not intraday_path.exists():
        return []

    daily = load(daily_path, symbol)
    intraday = load(intraday_path, symbol)
    if len(daily) < 30 or len(intraday) < 30:
        return []

    bias = _daily_bias_rising_ema(daily)
    atr_series = atr(intraday, 14)

    patterns = detect_double_bottom(intraday) + detect_inverse_hns(intraday)
    patterns.sort(key=lambda p: p["break_index"])

    trades = []
    open_until: dict[str, int] = {}
    for p in patterns:
        if not bt_module.bias_asof(bias, p["break_time"]):
            continue
        ptype = p["type"]
        if p["break_index"] <= open_until.get(ptype, -1):
            continue
        trade = build_trade(intraday, p, atr_series, equity, risk_pct)
        if trade is None:
            continue
        entry_a = dict(trade)
        entry_a["entry_style"] = "breakout"
        resolved_a = resolve_trade(intraday, entry_a, target_key="target_2R")
        resolved_a["symbol"] = symbol
        trades.append(resolved_a)
        if resolved_a["outcome"] == "open":
            open_until[ptype] = len(intraday)
        else:
            idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
            open_until[ptype] = idx[0] if idx else len(intraday)
        trade_b = build_retest_trade(intraday, trade, atr_series)
        if trade_b is not None:
            resolved_b = resolve_trade(intraday, trade_b, target_key="target_2R")
            resolved_b["symbol"] = symbol
            trades.append(resolved_b)

    return trades


# ---------------------------------------------------------------------------
# FILTER 3 — Volume confirmation: break_index bar volume >= 10-bar avg volume
# ---------------------------------------------------------------------------

def filter3_run_symbol(symbol, equity, risk_pct):
    daily_path = DATA / f"{symbol}_day.json"
    intraday_path = DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not intraday_path.exists():
        return []

    daily = load(daily_path, symbol)
    intraday = load(intraday_path, symbol)
    if len(daily) < 30 or len(intraday) < 30:
        return []

    bias = bt_module.daily_bias_series(daily)
    atr_series = atr(intraday, 14)
    vol_avg = intraday["volume"].rolling(10, min_periods=1).mean()

    patterns = detect_double_bottom(intraday) + detect_inverse_hns(intraday)
    patterns.sort(key=lambda p: p["break_index"])

    trades = []
    open_until: dict[str, int] = {}
    for p in patterns:
        if not bt_module.bias_asof(bias, p["break_time"]):
            continue
        # Volume filter: break bar volume must be >= 10-bar avg
        bi = p["break_index"]
        if bi < len(intraday):
            bar_vol = intraday.loc[bi, "volume"]
            avg_vol = vol_avg.loc[bi]
            if bar_vol < avg_vol:
                continue
        ptype = p["type"]
        if p["break_index"] <= open_until.get(ptype, -1):
            continue
        trade = build_trade(intraday, p, atr_series, equity, risk_pct)
        if trade is None:
            continue
        entry_a = dict(trade)
        entry_a["entry_style"] = "breakout"
        resolved_a = resolve_trade(intraday, entry_a, target_key="target_2R")
        resolved_a["symbol"] = symbol
        trades.append(resolved_a)
        if resolved_a["outcome"] == "open":
            open_until[ptype] = len(intraday)
        else:
            idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
            open_until[ptype] = idx[0] if idx else len(intraday)
        trade_b = build_retest_trade(intraday, trade, atr_series)
        if trade_b is not None:
            resolved_b = resolve_trade(intraday, trade_b, target_key="target_2R")
            resolved_b["symbol"] = symbol
            trades.append(resolved_b)

    return trades


# ---------------------------------------------------------------------------
# FILTER 4 — Minimum pattern depth 3%: (neckline - stop_basis) / neckline >= 0.03
# ---------------------------------------------------------------------------

def filter4_run_symbol(symbol, equity, risk_pct):
    daily_path = DATA / f"{symbol}_day.json"
    intraday_path = DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not intraday_path.exists():
        return []

    daily = load(daily_path, symbol)
    intraday = load(intraday_path, symbol)
    if len(daily) < 30 or len(intraday) < 30:
        return []

    bias = bt_module.daily_bias_series(daily)
    atr_series = atr(intraday, 14)

    patterns = detect_double_bottom(intraday) + detect_inverse_hns(intraday)
    patterns.sort(key=lambda p: p["break_index"])

    trades = []
    open_until: dict[str, int] = {}
    for p in patterns:
        if not bt_module.bias_asof(bias, p["break_time"]):
            continue
        # Depth filter
        neckline = p["neckline"]
        stop_basis = p["stop_basis"]
        if neckline <= 0 or (neckline - stop_basis) / neckline < 0.03:
            continue
        ptype = p["type"]
        if p["break_index"] <= open_until.get(ptype, -1):
            continue
        trade = build_trade(intraday, p, atr_series, equity, risk_pct)
        if trade is None:
            continue
        entry_a = dict(trade)
        entry_a["entry_style"] = "breakout"
        resolved_a = resolve_trade(intraday, entry_a, target_key="target_2R")
        resolved_a["symbol"] = symbol
        trades.append(resolved_a)
        if resolved_a["outcome"] == "open":
            open_until[ptype] = len(intraday)
        else:
            idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
            open_until[ptype] = idx[0] if idx else len(intraday)
        trade_b = build_retest_trade(intraday, trade, atr_series)
        if trade_b is not None:
            resolved_b = resolve_trade(intraday, trade_b, target_key="target_2R")
            resolved_b["symbol"] = symbol
            trades.append(resolved_b)

    return trades


# ---------------------------------------------------------------------------
# FILTER 5 — Double-bottom only
# ---------------------------------------------------------------------------

def filter5_run_symbol(symbol, equity, risk_pct):
    daily_path = DATA / f"{symbol}_day.json"
    intraday_path = DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not intraday_path.exists():
        return []

    daily = load(daily_path, symbol)
    intraday = load(intraday_path, symbol)
    if len(daily) < 30 or len(intraday) < 30:
        return []

    bias = bt_module.daily_bias_series(daily)
    atr_series = atr(intraday, 14)

    patterns = detect_double_bottom(intraday) + detect_inverse_hns(intraday)
    patterns.sort(key=lambda p: p["break_index"])

    trades = []
    open_until: dict[str, int] = {}
    for p in patterns:
        if p["type"] != "double_bottom":
            continue  # filter: double_bottom only
        if not bt_module.bias_asof(bias, p["break_time"]):
            continue
        ptype = p["type"]
        if p["break_index"] <= open_until.get(ptype, -1):
            continue
        trade = build_trade(intraday, p, atr_series, equity, risk_pct)
        if trade is None:
            continue
        entry_a = dict(trade)
        entry_a["entry_style"] = "breakout"
        resolved_a = resolve_trade(intraday, entry_a, target_key="target_2R")
        resolved_a["symbol"] = symbol
        trades.append(resolved_a)
        if resolved_a["outcome"] == "open":
            open_until[ptype] = len(intraday)
        else:
            idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
            open_until[ptype] = idx[0] if idx else len(intraday)
        trade_b = build_retest_trade(intraday, trade, atr_series)
        if trade_b is not None:
            resolved_b = resolve_trade(intraday, trade_b, target_key="target_2R")
            resolved_b["symbol"] = symbol
            trades.append(resolved_b)

    return trades


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

VARIANTS = [
    ("Baseline",                baseline_run_symbol),
    ("F1: Retest-only",         filter1_run_symbol),
    ("F2: Rising EMA slope",    filter2_run_symbol),
    ("F3: Volume confirm",      filter3_run_symbol),
    ("F4: Min depth 3%",        filter4_run_symbol),
    ("F5: Double-bottom only",  filter5_run_symbol),
]

if __name__ == "__main__":
    results = []
    for name, fn in VARIANTS:
        print(f"\n=== Running: {name} ===")
        r = _run_all(SYMBOLS, fn)
        r["variant"] = name
        results.append(r)
        print(f"  -> trades={r['trades']}, win_rate={r['win_rate']}%, expectancy={r['expectancy_R']}R, "
              f"final_equity=${r['final_equity']}, gain={r['gain_pct']}%, open={r['open_trades']}")

    # Print summary table
    print("\n")
    print("=" * 100)
    header = f"{'Variant':<26} {'Trades':>7} {'Win%':>6} {'Exp(R)':>8} {'Total R':>8} {'Final Eq':>10} {'Gain%':>8} {'Open':>6}"
    print(header)
    print("-" * 100)
    for r in results:
        row = (
            f"{r['variant']:<26} "
            f"{r['trades']:>7} "
            f"{r['win_rate']:>5.1f}% "
            f"{r['expectancy_R']:>8.3f} "
            f"{r['total_R']:>8.2f} "
            f"${r['final_equity']:>9.2f} "
            f"{r['gain_pct']:>7.1f}% "
            f"{r['open_trades']:>6}"
        )
        print(row)
    print("=" * 100)
