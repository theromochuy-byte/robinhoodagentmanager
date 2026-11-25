#!/usr/bin/env python3
"""Weekly volatile-batch swing lab (paper/backtest only).

Each Monday: rank the universe by ATR(14)% among the more liquid half (20-day dollar volume)
using daily data up to the prior session, and take the top BATCH names for the week.
During the week, trade dips on 1H bars with one of several entry/exit rules.
All variants share: stop = entry - STOP_ATR x daily ATR(14), max hold MAX_HOLD_DAYS,
flat by the week's last bar, SLIPPAGE per side, one position per symbol at a time.

Baselines: buy every batch stock at Monday's first bar and sell at the week's last bar.

Usage: python scripts/weekly_swing_lab.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from swing_agent.dataio import bars_to_df
from swing_agent.indicators import atr

CACHE = ROOT / "data" / "hist_cache"
REPORTS = ROOT / "reports"
BATCH = 10
STOP_ATR = 1.0
MAX_HOLD_DAYS = 3
BARS_PER_DAY = 7      # 1H bars per session in the cache
SLIPPAGE = 0.0005


def rsi(close: pd.Series, period: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, 1e-12))


def load():
    daily, hourly = {}, {}
    for p in CACHE.glob("*_hour.json"):
        sym = p.stem.split("_")[0]
        dp = CACHE / f"{sym}_day.json"
        if not dp.exists():
            continue
        d = bars_to_df(json.load(open(dp)), sym).reset_index(drop=True)
        h = bars_to_df(json.load(open(p)), sym).reset_index(drop=True)
        if len(d) < 60 or len(h) < 200:
            continue
        d["date"] = pd.to_datetime(d["begins_at"]).dt.strftime("%Y-%m-%d")
        d["atr"] = atr(d, 14)
        d["atr_pct"] = d["atr"] / d["close"]
        d["dollar_vol"] = (d["close"] * d["volume"]).rolling(20).mean()
        sma50 = d["close"].rolling(50).mean()
        d["uptrend"] = (d["close"] > sma50) & (d["close"].ewm(span=20, adjust=False).mean() > sma50)
        ts = pd.to_datetime(h["begins_at"])
        h["date"] = ts.dt.strftime("%Y-%m-%d")
        h["week"] = ts.dt.strftime("%G-%V")
        c = h["close"]
        h["rsi2"] = rsi(c, 2)
        h["rsi14"] = rsi(c, 14)
        h["sma20"] = c.rolling(20).mean()
        h["sd20"] = c.rolling(20).std()
        h["sma35"] = c.rolling(5 * BARS_PER_DAY).mean()
        daily[sym], hourly[sym] = d, h
    return daily, hourly


def weekly_batches(daily, hourly, uptrend_only=False):
    weeks = sorted({w for h in hourly.values() for w in h["week"].unique()})
    out = {}
    for w in weeks:
        first_day = min(h.loc[h["week"] == w, "date"].min() for h in hourly.values() if (h["week"] == w).any())
        rows = []
        for sym, d in daily.items():
            prior = d[d["date"] < first_day]
            if len(prior) < 30 or pd.isna(prior["atr_pct"].iloc[-1]):
                continue
            if uptrend_only and not prior["uptrend"].iloc[-1]:
                continue
            rows.append((sym, prior["atr_pct"].iloc[-1], prior["dollar_vol"].iloc[-1], prior["atr"].iloc[-1]))
        if not rows:
            continue
        df = pd.DataFrame(rows, columns=["sym", "atr_pct", "dv", "atr"])
        liquid = df[df["dv"] >= df["dv"].median()]
        out[w] = liquid.nlargest(BATCH, "atr_pct").set_index("sym")["atr"].to_dict()
    return out


ENTRIES = {
    "rsi2<10":        lambda h, i, a: h.at[i, "rsi2"] < 10,
    "rsi14<30":       lambda h, i, a: h.at[i, "rsi14"] < 30,
    "below_bband":    lambda h, i, a: h.at[i, "close"] < h.at[i, "sma20"] - 2 * h.at[i, "sd20"],
    "stretch_1atr":   lambda h, i, a: h.at[i, "close"] < h.at[i, "sma35"] - 1.0 * a,
}
EXITS = {
    "back_to_sma20":  lambda h, i, e, a: h.at[i, "close"] >= h.at[i, "sma20"],
    "rsi2>70":        lambda h, i, e, a: h.at[i, "rsi2"] > 70,
    "plus_1R":        lambda h, i, e, a: h.at[i, "high"] >= e + STOP_ATR * a,
}


def trade_week(h, idx, atr_d, entry_rule, exit_rule):
    trades, i, last = [], idx[0], idx[-1]
    while i < last:
        if not entry_rule(h, i, atr_d):
            i += 1
            continue
        e = float(h.at[i, "close"])
        stop = e - STOP_ATR * atr_d
        risk = e - stop
        j, px, why = i, None, None
        for j in range(i + 1, last + 1):
            if h.at[j, "low"] <= stop:
                px, why = stop, "stop"
                break
            if exit_rule(h, j, e, atr_d):
                px = e + STOP_ATR * atr_d if exit_rule is EXITS["plus_1R"] else float(h.at[j, "close"])
                why = "target"
                break
            if j - i >= MAX_HOLD_DAYS * BARS_PER_DAY or j == last:
                px, why = float(h.at[j, "close"]), "time" if j < last else "friday"
                break
        if px is None:
            break
        r = (px - e) / risk - 2 * SLIPPAGE * e / risk
        trades.append({"sym": h.at[i, "symbol"] if "symbol" in h else "", "entry_time": h.at[i, "begins_at"],
                       "r": round(r, 3), "ret_pct": round(100 * ((px - e) / e - 2 * SLIPPAGE), 3),
                       "exit": why, "bars": j - i})
        i = j + 1
    return trades


def stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    win = sum(r > 0 for r in rs)
    gw, gl = sum(r for r in rs if r > 0), -sum(r for r in rs if r <= 0)
    ex = {}
    for t in trades:
        ex[t["exit"]] = ex.get(t["exit"], 0) + 1
    return {"n": n, "win_pct": round(100 * win / n, 1), "avg_r": round(sum(rs) / n, 3),
            "avg_ret_pct": round(sum(t["ret_pct"] for t in trades) / n, 3),
            "pf": round(gw / gl, 2) if gl else None, "exits": ex}


def main():
    daily, hourly = load()
    for uptrend_only in (False, True):
        run(daily, hourly, uptrend_only)


def run(daily, hourly, uptrend_only):
    batches = weekly_batches(daily, hourly, uptrend_only)
    print(f"\n=== batch = top {BATCH} volatile liquid names{' in a daily uptrend' if uptrend_only else ''} ===")
    results = {}
    base = []
    for w, names in batches.items():
        for sym, a in names.items():
            h = hourly[sym]
            idx = h.index[h["week"] == w].tolist()
            if len(idx) < 2:
                continue
            e, x = float(h.at[idx[0], "open"]), float(h.at[idx[-1], "close"])
            risk = STOP_ATR * a
            base.append({"r": round((x - e) / risk - 2 * SLIPPAGE * e / risk, 3),
                         "ret_pct": round(100 * ((x - e) / e - 2 * SLIPPAGE), 3), "exit": "friday"})
    results["baseline: hold Mon-Fri"] = stats(base)
    for en, ef in ENTRIES.items():
        for xn, xf in EXITS.items():
            ts = []
            for w, names in batches.items():
                for sym, a in names.items():
                    h = hourly[sym]
                    idx = h.index[h["week"] == w].tolist()
                    if len(idx) >= 2:
                        ts += trade_week(h, idx, a, ef, xf)
            results[f"{en} -> {xn}"] = stats(ts)

    print(f"{len(batches)} weeks, {BATCH} names/week, stop {STOP_ATR} daily ATR, max hold {MAX_HOLD_DAYS}d, flat Friday")
    for k, s in results.items():
        print(f"  {k:30s} n={s['n']:4d} win%={s['win_pct']:5.1f} avgR={s['avg_r']:+.3f} "
              f"avg%={s['avg_ret_pct']:+.3f} PF={s['pf']}  {s['exits']}")
    sample = {w: sorted(n) for w, n in list(batches.items())[-3:]}
    print("Recent batches:", sample)
    tag = "_uptrend" if uptrend_only else ""
    churn = [len(set(b) - set(a)) for a, b in zip(list(batches.values()), list(batches.values())[1:])]
    print(f"Avg new names per week: {sum(churn) / max(1, len(churn)):.1f} of {BATCH}")
    (REPORTS / f"weekly_swing_lab{tag}.json").write_text(json.dumps({"results": results, "batches": {w: sorted(n) for w, n in batches.items()}}, indent=1))


if __name__ == "__main__":
    main()
