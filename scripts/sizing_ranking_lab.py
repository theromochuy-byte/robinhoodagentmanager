#!/usr/bin/env python3
"""Position sizing x signal ranking lab (paper/backtest only).

Tests whether smaller positions spread over more slots, and/or ranking each day's signals,
improve the account results of the pullback setup and the relaxed pattern setup.

Account rules: cash account (total position value <= equity), slippage per side, per-position
value cap = 1 / slots of equity, risk per trade as configured. Each day's signals are pooled and
taken in rank order. For 1H pattern trades this lets a later-in-the-day signal outrank an earlier
one, a mild look-ahead that can flatter ranked pattern results.

Rankings (computed from daily data before the signal date):
  random     baseline, 200 shuffles
  rs_high    strongest 63-day return first
  calm       lowest ATR% first
  oversold   pullback only: lowest RSI(2) first
"""
from __future__ import annotations

import random
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import compare_strategies as cs
from swing_agent.indicators import atr

SIZING = [(0.02, 4), (0.01, 8), (0.005, 16)]
SHUFFLES = 200


def features(daily):
    feats = {}
    for sym, d in daily.items():
        a = atr(d.reset_index(), 14).values / d["close"].values
        r63 = d["close"].pct_change(63).values
        feats[sym] = (list(d.index), a, r63)
    return feats


def feat(feats, sym, day):
    idx, a, r63 = feats[sym]
    lo, hi = 0, len(idx)
    while lo < hi:
        mid = (lo + hi) // 2
        if idx[mid] < day:
            lo = mid + 1
        else:
            hi = mid
    k = lo - 1
    return (a[k], r63[k]) if k >= 0 else (float("nan"), float("nan"))


def simulate(trades, start, risk_pct, slots):
    trades = sorted((t for t in trades if t["entry_time"][:10] >= start),
                    key=lambda t: (t["entry_time"][:10], t["rank"]))
    equity = peak = cs.STARTING_EQUITY
    max_dd, open_pos, per_day, taken = 0.0, [], defaultdict(int), 0
    cap = 1.0 / slots

    def close_until(when):
        nonlocal equity, peak, max_dd
        for p in sorted([p for p in open_pos if p["exit_time"] <= when], key=lambda p: p["exit_time"]):
            equity += p["pnl"]
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)
            open_pos.remove(p)

    for t in trades:
        close_until(t["entry_time"])
        day = t["entry_time"][:10]
        if len(open_pos) >= slots or per_day[day] >= max(2, slots // 2):
            continue
        if any(p["symbol"] == t["symbol"] for p in open_pos):
            continue
        rps, entry = t["risk_per_share"], t["entry"]
        in_use = sum(p["value"] for p in open_pos)
        shares = min(equity * risk_pct / rps, equity * cap / entry, max(0.0, equity - in_use) / entry)
        if shares <= 0:
            continue
        net_r = t["r"] - 2 * cs.SLIPPAGE * entry / rps
        open_pos.append({**t, "pnl": net_r * rps * shares, "value": shares * entry})
        per_day[day] += 1
        taken += 1
    close_until("9999")
    ret = 100 * (equity / cs.STARTING_EQUITY - 1)
    return ret, 100 * max_dd, taken


def main():
    daily = cs.load_daily()
    feats = features(daily)
    pb = cs.at_close(cs.pullback_trades("long", None))
    pat = cs.funnel_trades(cs.RELAXED["patterns_relaxed"])
    sets = {"pullback": pb, "patterns_relaxed": pat, "pullback+patterns": pb + pat}
    for name, tr in sets.items():
        for t in tr:
            a, r63 = feat(feats, t["symbol"], t["entry_time"][:10])
            t["_calm"], t["_rs"] = a if a == a else 9, -(r63 if r63 == r63 else -9)
            t["_oversold"] = t.get("rsi2", 50)
        print(f"\n=== {name} ({len(tr)} signals, {cs.WINDOW_START} to Nov 2026) ===")
        print(f"  {'risk/slots':12s} {'ranking':10s} {'return':>8s} {'maxDD':>7s} {'trades':>7s}   random: median [10th..90th], % runs profitable")
        rng = random.Random(11)
        for risk, slots in SIZING:
            rets = []
            for _ in range(SHUFFLES):
                for t in tr:
                    t["rank"] = rng.random()
                rets.append(simulate(tr, cs.WINDOW_START, risk, slots)[0])
            rets.sort()
            band = f"{st.median(rets):+5.1f}% [{rets[SHUFFLES // 10]:+5.1f}..{rets[9 * SHUFFLES // 10]:+5.1f}], {100 * sum(r > 0 for r in rets) / SHUFFLES:.0f}%"
            ranks = ["rs_high", "calm"] + (["oversold"] if name != "patterns_relaxed" else [])
            for i, rk in enumerate(ranks):
                key = {"rs_high": "_rs", "calm": "_calm", "oversold": "_oversold"}[rk]
                for t in tr:
                    t["rank"] = t[key]
                ret, dd, n = simulate(tr, cs.WINDOW_START, risk, slots)
                pct = 100 * sum(r < ret for r in rets) / SHUFFLES
                print(f"  {f'{risk:.1%}/{slots}':12s} {rk:10s} {ret:+7.1f}% {dd:6.1f}% {n:7d}   "
                      f"beats {pct:3.0f}% of random" + (f"   | random: {band}" if i == 0 else ""))


if __name__ == "__main__":
    main()
