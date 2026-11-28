#!/usr/bin/env python3
"""Benchmark: buy short-term pullbacks inside a daily uptrend (paper/backtest only).

Long  : daily close when RSI(2) < 10 and the uptrend filter holds.
Short : daily close when RSI(2) > 90 and the downtrend filter holds (mirror).
Stop  : 1.5 x ATR(14) beyond entry (defines R; checked on the intraday extreme).
Exits (two variants, reported separately):
  classic : close back through the 5-day SMA, else stop, else time stop.
  ours    : 50% at 1R (stop to entry + 0.15R), rest at 1.5R, fast loss, time stop.
Trend filters:
  sma200  : close beyond the 200-day SMA
  ours    : close beyond the 50 SMA and 20 EMA beyond the 50 SMA

Usage: python scripts/run_pullback_benchmark.py [--since 2025-12-22]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from swing_agent.config import (
    BREAKEVEN_BUFFER_R, FAST_LOSS_DAYS, FAST_LOSS_R, PARTIAL_EXIT_FRAC,
    PARTIAL_EXIT_R, TARGET_FULL_R, TIME_STOP_TRADING_DAYS,
)
from swing_agent.dataio import bars_to_df
from swing_agent.indicators import atr, ema

CACHE = ROOT / "data" / "hist_cache"
REPORTS = ROOT / "reports"

RSI_ENTRY = 10
STOP_ATR_MULT = 1.5


def rsi(close: pd.Series, period: int = 2) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, 1e-12))


def trend_mask(df: pd.DataFrame, kind: str, side: str) -> pd.Series:
    c = df["close"]
    sma50 = c.rolling(50).mean()
    if kind == "sma200":
        sma200 = c.rolling(200).mean()
        return c > sma200 if side == "long" else c < sma200
    e20 = ema(c, 20)
    if side == "long":
        return (c > sma50) & (e20 > sma50)
    return (c < sma50) & (e20 < sma50)


def resolve(df: pd.DataFrame, i: int, entry: float, stop: float, exit_rule: str, side: str):
    """Return (exit_index, r_multiple, reason) or None if still open. sgn flips prices for shorts."""
    sgn = 1 if side == "long" else -1
    risk = sgn * (entry - stop)
    sma5 = df["close"].rolling(5).mean()
    gain = lambda px: sgn * (px - entry) / risk
    t1, tfull, be = (entry + sgn * k * risk for k in (PARTIAL_EXIT_R, TARGET_FULL_R, BREAKEVEN_BUFFER_R))
    partial = False
    for j in range(i + 1, len(df)):
        days = j - i
        lo, hi, cl = df.at[j, "low"], df.at[j, "high"], df.at[j, "close"]
        adverse, favorable = (lo, hi) if side == "long" else (hi, lo)
        if exit_rule == "classic":
            if sgn * (adverse - stop) <= 0:
                return j, gain(stop), "stop"
            if sgn * (cl - sma5.iat[j]) > 0:
                return j, gain(cl), "sma5"
            if days >= TIME_STOP_TRADING_DAYS:
                return j, gain(cl), "time_stop"
            continue
        if not partial and sgn * (favorable - t1) >= 0:
            partial = True
        eff = be if partial else stop
        blend = (lambda px: PARTIAL_EXIT_FRAC * PARTIAL_EXIT_R + (1 - PARTIAL_EXIT_FRAC) * gain(px)) if partial else gain
        if not partial and days <= FAST_LOSS_DAYS and gain(cl) <= FAST_LOSS_R:
            return j, gain(cl), "fast_loss"
        if sgn * (adverse - eff) <= 0:
            return j, blend(eff), "be_stop" if partial else "stop"
        if sgn * (favorable - tfull) >= 0:
            return j, blend(tfull), "1.5R"
        if not partial and days >= TIME_STOP_TRADING_DAYS:
            return j, gain(cl), "time_stop"
    return None


def scan(symbol: str, trend: str, exit_rule: str, since: str | None, side: str = "long") -> list[dict]:
    df = bars_to_df(json.load(open(CACHE / f"{symbol}_day.json")), symbol).reset_index(drop=True)
    if len(df) < 220:
        return []
    r2, a14, ok = rsi(df["close"]), atr(df, 14), trend_mask(df, trend, side)
    sgn = 1 if side == "long" else -1
    trades, free_from = [], 0
    for i in range(200, len(df) - 1):
        if i < free_from or not ok.iat[i]:
            continue
        if (side == "long" and r2.iat[i] >= RSI_ENTRY) or (side == "short" and r2.iat[i] <= 100 - RSI_ENTRY):
            continue
        if since and str(df.at[i, "begins_at"]) < since:
            continue
        entry = float(df.at[i, "close"])
        risk = STOP_ATR_MULT * float(a14.iat[i])
        res = resolve(df, i, entry, entry - sgn * risk, exit_rule, side)
        if res is None:
            break
        j, r, why = res
        trades.append({"symbol": symbol, "side": side, "type": f"pullback_{side}",
                       "entry_time": str(df.at[i, "begins_at"]), "exit_time": str(df.at[j, "begins_at"]),
                       "entry": round(entry, 4), "risk_per_share": round(risk, 4),
                       "rsi2": round(float(r2.iat[i]), 2), "r": round(r, 3), "exit": why, "days": j - i})
        free_from = j + 1
    return trades


def summary(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    exits: dict[str, int] = {}
    for t in trades:
        exits[t["exit"]] = exits.get(t["exit"], 0) + 1
    return {"n": n, "win_pct": round(100 * sum(r > 0 for r in rs) / n, 1),
            "avg_r": round(sum(rs) / n, 3), "total_r": round(sum(rs), 1),
            "avg_days": round(sum(t["days"] for t in trades) / n, 1), "exits": exits}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None)
    args = ap.parse_args()
    symbols = sorted(p.stem.split("_")[0] for p in CACHE.glob("*_day.json"))
    out = {}
    print(f"Pullback benchmark — {len(symbols)} symbols, since={args.since or 'all'}")
    for side in ("long", "short"):
        for trend in ("sma200", "ours"):
            for exit_rule in ("classic", "ours"):
                trades = [t for s in symbols for t in scan(s, trend, exit_rule, args.since, side)]
                s = summary(trades)
                out[f"{side}/{trend}/{exit_rule}"] = {"summary": s, "trades": trades}
                print(f"  {side:5s} trend={trend:6s} exit={exit_rule:7s}  n={s['n']:4d}  "
                      f"win%={s.get('win_pct', 0):5.1f}  avgR={s.get('avg_r', 0):+.3f}  "
                      f"totalR={s.get('total_r', 0):+.1f}  days={s.get('avg_days', 0)}")
    tag = f"_since{args.since}" if args.since else ""
    (REPORTS / f"pullback_benchmark{tag}.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
