#!/usr/bin/env python3
"""Filter funnel for the live pattern scanner (paper/backtest only).

Every long pattern on 1H bars is turned into a retest (Entry B) trade and resolved with the
current exit rules, whether or not it passes the scanner's filters. Each filter is recorded
as a pass/fail flag, so we can see:
  - how many trades each filter removes, and how those removed trades would have done
  - what happens if one filter at a time is relaxed while the rest stay on

Filters mirrored from the live scanner:
  bias      daily close and low above 20 EMA, 20 EMA above 50 SMA
  stack     20 EMA > 50 SMA > 200 SMA
  rs        stock's 20-day return beats the equal-weight universe index
  depth     buffered-stop depth between 3% and 12% of the neckline
  qs        backtest quality score within [MIN_QUALITY_SCORE, 0.52]
  pattern   inverse H&S or cup-and-handle (double bottom is suspended live)

Usage: python scripts/filter_funnel.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from run_backtest import QUALITY_SCORE_CAP, _quality_score, resolve_current
from swing_agent.backtest import bias_asof, daily_bias_series
from swing_agent.config import MIN_QUALITY_SCORE, RISK_PCT, STARTING_EQUITY
from swing_agent.dataio import bars_to_df
from swing_agent.indicators import atr, ema
from swing_agent.patterns import detect_cup_and_handle, detect_double_bottom, detect_inverse_hns
from swing_agent.simulator import build_retest_trade, build_trade

CACHE = ROOT / "data" / "hist_cache"
REPORTS = ROOT / "reports"
SLIPPAGE = 0.0005
GATES = ["pattern", "bias", "stack", "rs", "depth", "qs"]


def universe_index() -> pd.Series:
    closes = {}
    for p in CACHE.glob("*_day.json"):
        d = bars_to_df(json.load(open(p)), p.stem.split("_")[0])
        closes[p.stem.split("_")[0]] = pd.Series(d["close"].values, index=pd.to_datetime(d["begins_at"]).dt.strftime("%Y-%m-%d"))
    df = pd.DataFrame(closes).sort_index()
    return (df / df.bfill().iloc[0]).mean(axis=1)


def rows_for(symbol: str, idx: pd.Series) -> list[dict]:
    dp, hp = CACHE / f"{symbol}_day.json", CACHE / f"{symbol}_hour.json"
    if not (dp.exists() and hp.exists()):
        return []
    daily = bars_to_df(json.load(open(dp)), symbol)
    h1 = bars_to_df(json.load(open(hp)), symbol)
    if len(daily) < 60 or len(h1) < 100:
        return []
    daily["date"] = pd.to_datetime(daily["begins_at"]).dt.strftime("%Y-%m-%d")
    c = daily["close"]
    e20, s50, s200 = ema(c, 20), c.rolling(50).mean(), c.rolling(200).mean()
    bias = daily_bias_series(daily)
    atr_s = atr(h1, 14)
    pats = detect_inverse_hns(h1) + detect_cup_and_handle(h1) + detect_double_bottom(h1)
    out = []
    for p in pats:
        bi = p["break_index"]
        trade = build_trade(h1, p, atr_s, STARTING_EQUITY, RISK_PCT)
        if trade is None:
            continue
        tb = build_retest_trade(h1, trade, atr_s)
        if tb is None:
            continue
        res = resolve_current(h1, tb)
        if res["outcome"] == "open":
            continue
        day = str(p["break_time"])[:10]
        k = daily.index[daily["date"] < day]
        if len(k) < 200:
            continue
        k = k[-1]
        i_up = idx[idx.index < day]
        stock_20 = c.iat[k] / c.iat[k - 20] - 1
        idx_20 = i_up.iloc[-1] / i_up.iloc[-21] - 1 if len(i_up) > 21 else -1
        depth = (p["neckline"] - trade["stop"]) / p["neckline"]
        atr_val = float(atr_s.iloc[bi])
        qs = _quality_score(p, h1, atr_val)
        net_r = res["r_multiple"] - 2 * SLIPPAGE * tb["entry"] / tb["risk_per_share"]
        out.append({
            "symbol": symbol, "type": p["type"], "break_time": str(p["break_time"]),
            "entry_time": tb["entry_time"], "exit_time": res["exit_time"],
            "entry": tb["entry"], "risk_per_share": tb["risk_per_share"], "r": res["r_multiple"],
            "net_r": round(net_r, 3), "exit": res["exit_reason"], "depth": round(depth, 4), "qs": qs,
            "pattern": p["type"] in ("inverse_hns", "cup_and_handle"),
            "bias": bool(bias_asof(bias, p["break_time"])),
            "stack": bool(e20.iat[k] > s50.iat[k] > s200.iat[k]),
            "rs": bool(stock_20 >= idx_20),
            "depth_ok": 0.03 <= depth <= 0.12,
            "qs_ok": MIN_QUALITY_SCORE <= qs <= QUALITY_SCORE_CAP,
        })
    return out


def ok(r: dict, gate: str) -> bool:
    return r[{"depth": "depth_ok", "qs": "qs_ok"}.get(gate, gate)]


def stats(rs: list[dict]) -> dict:
    n = len(rs)
    if not n:
        return {"n": 0, "win": 0, "avg": 0, "total": 0}
    v = [r["net_r"] for r in rs]
    return {"n": n, "win": round(100 * sum(x > 0 for x in v) / n, 1),
            "avg": round(sum(v) / n, 3), "total": round(sum(v), 1)}


def line(label: str, s: dict) -> str:
    return f"  {label:38s} n={s['n']:4d} win%={s['win']:5.1f} avgR={s['avg']:+.3f} totalR={s['total']:+.1f}"


def one_at_a_time(rows: list[dict]) -> list[dict]:
    """Per symbol, drop any trade that would open while an earlier one is still open."""
    out, busy = [], {}
    for r in sorted(rows, key=lambda r: r["entry_time"]):
        if r["entry_time"] < busy.get(r["symbol"], ""):
            continue
        out.append(r)
        busy[r["symbol"]] = r["exit_time"]
    return out


def main() -> None:
    idx = universe_index()
    syms = sorted(p.stem.split("_")[0] for p in CACHE.glob("*_hour.json"))
    raw = [r for s in syms for r in rows_for(s, idx)]
    for label, rows in (("RAW (overlapping trades counted)", raw),):
        print(f"{label}: {line('', stats(rows))[2:]}")
    report(raw, "deduped per filter combination")


def report(raw: list[dict], label: str) -> None:
    print(f"\n##### {label}: each filter set is deduped to one open trade per stock #####")
    rows = one_at_a_time(raw)
    passing = one_at_a_time([r for r in raw if all(ok(r, g) for g in GATES)])
    print(f"Raw trades {len(raw)} -> {len(rows)} after keeping one open trade per stock")

    print(f"All retest trades (no filters): {line('', stats(rows))[2:]}")
    print(f"All filters on (live scanner):  {line('', stats(passing))[2:]}")

    sel = lambda f: stats(one_at_a_time([r for r in raw if f(r)]))
    print("\nEach filter on its own: trades it keeps vs. trades it removes")
    for g in GATES:
        print(line(f"{g}: kept", sel(lambda r: ok(r, g))))
        print(line(f"{g}: removed", sel(lambda r: not ok(r, g))))

    print("\nRelax one filter, keep the rest on")
    for g in GATES:
        print(line(f"without {g}", sel(lambda r: all(ok(r, x) for x in GATES if x != g))))

    print("\nQuality score buckets (all other filters on)")
    for lo, hi in [(0, .45), (.45, .52), (.52, .6), (.6, .7), (.7, 1.01)]:
        print(line(f"qs {lo:.2f}-{hi:.2f}",
                   sel(lambda r: all(ok(r, x) for x in GATES if x != "qs") and lo <= r["qs"] < hi)))

    print("\nCombinations worth considering")
    combos = {
        "pattern only (no other filters)": lambda r: ok(r, "pattern"),
        "pattern + depth": lambda r: ok(r, "pattern") and ok(r, "depth"),
        "pattern + depth, NOT in uptrend bias": lambda r: ok(r, "pattern") and ok(r, "depth") and not ok(r, "bias"),
        "pattern + depth + bias": lambda r: ok(r, "pattern") and ok(r, "depth") and ok(r, "bias"),
        "all patterns incl. double bottom + depth": lambda r: ok(r, "depth"),
    }
    for k, f in combos.items():
        print(line(k, sel(f)))
        for t in ("inverse_hns", "cup_and_handle", "double_bottom"):
            s = sel(lambda r: f(r) and r["type"] == t)
            if s["n"]:
                print(line(f"    {t}", s))

    (REPORTS / "filter_funnel.json").write_text(json.dumps(raw, indent=1))


if __name__ == "__main__":
    main()
