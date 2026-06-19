"""End-to-end paper backtest runner.

Pipeline per symbol:
  1. Load daily bars, compute 20 EMA, mark per-bar long bias (fully above EMA).
  2. Load 4h bars, detect double-bottom and inverse-H&S patterns (neckline break).
  3. Keep only patterns whose breakout occurred while the daily bias was long
     (higher-timeframe confluence).
  4. Build a proposed long trade (entry / ATR stop / targets), size at 1% risk.
  5. Resolve each trade forward over 4h bars; record outcome and P/L.

Writes data/paper_ledger.json and reports/summary.json. Places no live orders.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from .dataio import load
from .indicators import atr, ema
from .patterns import detect_double_bottom, detect_inverse_hns
from .simulator import build_retest_trade, build_trade, compound_ledger, resolve_trade, summarize

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = ROOT / "reports"


def daily_bias_series(daily: pd.DataFrame, ema_period: int = 20) -> pd.Series:
    e = ema(daily["close"], ema_period)
    bias = (daily["close"] > e) & (daily["low"] > e)
    return pd.Series(bias.values, index=pd.to_datetime(daily["begins_at"]))


def bias_asof(bias: pd.Series, when) -> bool:
    when = pd.to_datetime(when)
    prior = bias[bias.index <= when]
    return bool(prior.iloc[-1]) if len(prior) else False


def run_symbol(symbol: str, equity: float, risk_pct: float = 0.01) -> list[dict]:
    daily_path = DATA / f"{symbol}_day.json"
    intraday_path = DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not intraday_path.exists():
        print(f"  [skip] {symbol}: missing data files")
        return []

    daily = load(daily_path, symbol)
    intraday = load(intraday_path, symbol)
    if len(daily) < 30 or len(intraday) < 30:
        print(f"  [skip] {symbol}: not enough bars")
        return []

    bias = daily_bias_series(daily)
    atr_series = atr(intraday, 14)

    patterns = detect_double_bottom(intraday) + detect_inverse_hns(intraday)
    patterns.sort(key=lambda p: p["break_index"])

    trades = []
    # one open position per pattern type per symbol — double bottom and inverse H&S
    # are treated as independent setups and don't block each other
    open_until: dict[str, int] = {}
    for p in patterns:
        if not bias_asof(bias, p["break_time"]):
            continue  # higher-timeframe trend not aligned
        ptype = p["type"]
        if p["break_index"] <= open_until.get(ptype, -1):
            continue  # already in a position of this type on this symbol
        trade = build_trade(intraday, p, atr_series, equity, risk_pct)
        if trade is None:
            continue
        # Entry A: breakout close, target = 2R
        entry_a = dict(trade)
        entry_a["entry_style"] = "breakout"
        resolved_a = resolve_trade(intraday, entry_a, target_key="target_2R")
        resolved_a["symbol"] = symbol
        trades.append(resolved_a)
        # track when this pattern-type slot reopens
        if resolved_a["outcome"] == "open":
            open_until[ptype] = len(intraday)
        else:
            idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
            open_until[ptype] = idx[0] if idx else len(intraday)
        # Entry B: neckline retest (variant — tracked separately, does not consume the slot)
        trade_b = build_retest_trade(intraday, trade, atr_series)
        if trade_b is not None:
            resolved_b = resolve_trade(intraday, trade_b, target_key="target_2R")
            resolved_b["symbol"] = symbol
            trades.append(resolved_b)

    n_a = sum(1 for t in trades if t.get("entry_style") == "breakout")
    n_b = sum(1 for t in trades if t.get("entry_style") == "retest")
    print(f"  {symbol}: {len(patterns)} raw patterns, {n_a} breakout trades, {n_b} retest trades")
    return trades


def run(symbols: list[str], equity: float = 10_000.0, risk_pct: float = 0.01) -> dict:
    REPORTS.mkdir(exist_ok=True)
    all_trades: list[dict] = []
    for sym in symbols:
        all_trades.extend(run_symbol(sym, equity, risk_pct))

    all_trades.sort(key=lambda t: t["entry_time"])
    all_trades = compound_ledger(all_trades, equity, risk_pct)

    ledger_path = DATA / "paper_ledger.json"
    with open(ledger_path, "w") as f:
        json.dump(all_trades, f, indent=2)

    summary = summarize(all_trades)
    by_pattern = {}
    for ptype in ("double_bottom", "inverse_hns"):
        subset = [t for t in all_trades if t["type"] == ptype]
        by_pattern[ptype] = summarize(subset)
    by_entry = {}
    for style in ("breakout", "retest"):
        subset = [t for t in all_trades if t.get("entry_style") == style]
        by_entry[style] = summarize(subset)

    closed = [t for t in all_trades if t["outcome"] != "open"]
    final_equity = round(equity + sum(t["pnl_dollars"] for t in closed), 2)
    equity_growth = {"starting_equity": equity, "final_equity": final_equity,
                     "gain_dollars": round(final_equity - equity, 2),
                     "gain_pct": round((final_equity - equity) / equity * 100, 2)}

    report = {"overall": summary, "by_pattern": by_pattern, "by_entry": by_entry,
              "equity_growth": equity_growth, "symbols": symbols}
    with open(REPORTS / "summary.json", "w") as f:
        json.dump(report, f, indent=2)
    return report


if __name__ == "__main__":
    import sys

    syms = sys.argv[1:] or ["AAPL"]
    rep = run(syms)
    print(json.dumps(rep, indent=2))
