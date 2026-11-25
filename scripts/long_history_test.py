#!/usr/bin/env python3
"""Long-history test of the pullback setup (research/backtest only).

Reads adjusted daily bars from data/hist_long/ (see scripts/fetch_long_history.py).
Runs the pullback long setup (RSI(2) < 10 in an uptrend, exit on close above the 5-day SMA)
through a cash account at several risk/slot sizes, 100 random-selection runs each, and
reports: whole-period return and drawdown, each calendar year, the three big sell-offs,
per-trade results by SPY regime, and a variant that opens no trades while SPY < its 200-day SMA.

Survivorship bias: the universe is today's list, so companies that failed are missing.
"""
from __future__ import annotations

import json
import random
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import run_pullback_benchmark as pbm
from swing_agent.config import STARTING_EQUITY
from swing_agent.dataio import bars_to_df

LONG = ROOT / "data" / "hist_long"
REPORTS = ROOT / "reports"
SLIPPAGE = 0.0005
SIZING = [(0.02, 4), (0.01, 8), (0.005, 16)]
RUNS = 100
SELLOFFS = {
    "Q4 2018 sell-off": ("2018-10-01", "2018-12-24"),
    "COVID crash": ("2020-02-19", "2020-03-23"),
    "2022 bear market": ("2022-01-03", "2022-10-12"),
}


def spy_regime() -> pd.Series:
    d = bars_to_df(json.load(open(LONG / "SPY_day.json")))
    d["date"] = pd.to_datetime(d["begins_at"]).dt.strftime("%Y-%m-%d")
    up = d["close"] > d["close"].rolling(200).mean()
    return pd.Series(up.values, index=d["date"])


def trades() -> list[dict]:
    pbm.CACHE = LONG
    syms = sorted(p.stem.split("_")[0] for p in LONG.glob("*_day.json") if p.stem != "SPY_day")
    out = []
    for s in syms:
        for t in pbm.scan(s, "ours", "classic", None, "long"):
            t["entry_time"] = t["entry_time"][:10] + "T20:00:00Z"
            t["exit_time"] = t["exit_time"][:10] + "T20:00:00Z"
            out.append(t)
    return out


def simulate(ts, risk_pct, slots, rng):
    ts = sorted(ts, key=lambda t: (t["entry_time"], rng.random()))
    equity = peak = STARTING_EQUITY
    max_dd, open_pos, per_day, curve = 0.0, [], defaultdict(int), []

    def close_until(when):
        nonlocal equity, peak, max_dd
        for p in sorted([p for p in open_pos if p["exit_time"] <= when], key=lambda p: p["exit_time"]):
            equity += p["pnl"]
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)
            open_pos.remove(p)
            curve.append((p["exit_time"][:10], equity))

    for t in ts:
        close_until(t["entry_time"])
        day = t["entry_time"][:10]
        if len(open_pos) >= slots or per_day[day] >= max(2, slots // 2):
            continue
        if any(p["symbol"] == t["symbol"] for p in open_pos):
            continue
        rps, entry = t["risk_per_share"], t["entry"]
        in_use = sum(p["value"] for p in open_pos)
        shares = min(equity * risk_pct / rps, equity / slots / entry, max(0.0, equity - in_use) / entry)
        if shares <= 0:
            continue
        net_r = t["r"] - 2 * SLIPPAGE * entry / rps
        open_pos.append({**t, "pnl": net_r * rps * shares, "value": shares * entry})
        per_day[day] += 1
    close_until("9999")
    return equity, max_dd, curve


def equity_at(curve, day, default):
    v = default
    for d, e in curve:
        if d > day:
            break
        v = e
    return v


def main():
    regime = spy_regime()
    all_trades = trades()
    first = min(t["entry_time"][:10] for t in all_trades)
    last = max(t["exit_time"][:10] for t in all_trades)
    years = (pd.Timestamp(last) - pd.Timestamp(first)).days / 365.25
    for t in all_trades:
        t["spy_up"] = bool(regime.get(t["entry_time"][:10], True))
    print(f"{len(all_trades)} signals, {first} to {last} ({years:.1f} years)\n")

    print("Per trade by SPY regime (before position limits, after slippage):")
    for lab, f in (("SPY above 200-day", True), ("SPY below 200-day", False)):
        rs = [t["r"] - 2 * SLIPPAGE * t["entry"] / t["risk_per_share"] for t in all_trades if t["spy_up"] == f]
        if rs:
            print(f"  {lab:20s} n={len(rs):5d} win%={100 * sum(r > 0 for r in rs) / len(rs):5.1f} avgR={sum(rs) / len(rs):+.3f}")

    variants = {"all signals": all_trades, "skip when SPY < 200-day": [t for t in all_trades if t["spy_up"]]}
    report = {}
    for vname, ts in variants.items():
        print(f"\n=== {vname} ===")
        for risk, slots in SIZING:
            rng = random.Random(5)
            runs = [simulate(ts, risk, slots, rng) for _ in range(RUNS)]
            ends = sorted(r[0] for r in runs)
            dds = sorted(r[1] for r in runs)
            cagr = [100 * ((e / STARTING_EQUITY) ** (1 / years) - 1) for e in ends]
            mid = sorted(runs, key=lambda r: r[0])[RUNS // 2]
            print(f"  {risk:.1%}/{slots:<3d} CAGR median {st.median(cagr):+5.1f}%/yr [10th {cagr[RUNS // 10]:+5.1f} .. 90th {cagr[9 * RUNS // 10]:+5.1f}]  "
                  f"total {100 * (st.median(ends) / STARTING_EQUITY - 1):+6.1f}%  maxDD median {100 * st.median(dds):4.1f}% (worst {100 * dds[-1]:4.1f}%)  "
                  f"profitable {100 * sum(e > STARTING_EQUITY for e in ends) / RUNS:.0f}%")
            yearly = {}
            for y in range(int(first[:4]), int(last[:4]) + 1):
                s = equity_at(mid[2], f"{y - 1}-12-31", STARTING_EQUITY)
                e = equity_at(mid[2], f"{y}-12-31", s)
                yearly[y] = round(100 * (e / s - 1), 1)
            sell = {}
            for k, (a, b) in SELLOFFS.items():
                s = equity_at(mid[2], a, STARTING_EQUITY)
                e = equity_at(mid[2], b, s)
                sell[k] = round(100 * (e / s - 1), 1)
            print(f"      median run by year: " + "  ".join(f"{y}:{v:+.1f}%" for y, v in yearly.items()))
            print(f"      median run in sell-offs: " + "  ".join(f"{k}: {v:+.1f}%" for k, v in sell.items()))
            report[f"{vname} | {risk:.1%}/{slots}"] = {"cagr_median": round(st.median(cagr), 2),
                                                       "max_dd_median": round(100 * st.median(dds), 1),
                                                       "yearly_median_run": yearly, "selloffs_median_run": sell}
    (REPORTS / "long_history_test.json").write_text(json.dumps({"first": first, "last": last, "results": report}, indent=1))


if __name__ == "__main__":
    main()
