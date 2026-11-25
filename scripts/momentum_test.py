#!/usr/bin/env python3
"""Monthly momentum rotation vs. buy-and-hold SPY (research/backtest only).

Reads adjusted daily bars from data/hist_long/. At each month-end close, ranks the universe by
past return and holds the top N equally weighted until the next month-end. Holdings drift with
prices between rebalances; SLIPPAGE is charged on traded value at each rebalance.

Survivorship bias is severe for momentum: the universe is today's list, so it is full of stocks
that are known (with hindsight) to have been big winners. Treat results as an upper bound.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from swing_agent.dataio import bars_to_df

LONG = ROOT / "data" / "hist_long"
REPORTS = ROOT / "reports"
SLIPPAGE = 0.0005
SELLOFFS = {
    "Q4 2018 sell-off": ("2018-10-01", "2018-12-24"),
    "COVID crash": ("2020-02-19", "2020-03-23"),
    "2022 bear market": ("2022-01-03", "2022-10-12"),
}


def load_closes() -> pd.DataFrame:
    cols = {}
    for p in LONG.glob("*_day.json"):
        d = bars_to_df(json.load(open(p)))
        cols[p.stem.split("_")[0]] = pd.Series(d["close"].values, index=pd.to_datetime(d["begins_at"]).dt.normalize())
    return pd.DataFrame(cols).sort_index()


def month_ends(idx: pd.DatetimeIndex) -> list[pd.Timestamp]:
    s = pd.Series(idx, index=idx)
    return list(s.groupby([idx.year, idx.month]).max())


def run(closes, spy, lookback, skip, top_n, trend_filter=False, spy_weight=0.0, pick="momentum", seed=0):
    """Return daily equity curve (start 1.0) for a monthly rotation strategy.
    pick: 'momentum' (top_n by past return), 'random' (top_n at random), 'all' (every stock)."""
    import random as _r
    rng = _r.Random(seed)
    stocks = closes.drop(columns=["SPY"])
    sma200 = spy.rolling(200).mean()
    rebal = [d for d in month_ends(closes.index) if closes.index.get_loc(d) >= lookback + skip]
    equity = pd.Series(index=closes.index, dtype=float)
    value, weights = 1.0, {}
    start = rebal[0]
    for k, d in enumerate(rebal):
        i = closes.index.get_loc(d)
        past, recent = stocks.iloc[i - lookback - skip], stocks.iloc[i - skip]
        mom = (recent / past - 1).dropna()
        mom = mom[stocks.iloc[i].notna()]
        if pick == "momentum":
            picks = list(mom.nlargest(top_n).index)
        elif pick == "random":
            picks = rng.sample(sorted(mom.index), min(top_n, len(mom)))
        else:
            picks = list(mom.index)
        risk_on = not trend_filter or spy.iloc[i] > sma200.iloc[i]
        target = {}
        if spy_weight:
            target["SPY"] = spy_weight
        if risk_on and picks:
            for s in picks:
                target[s] = target.get(s, 0) + (1 - spy_weight) / len(picks)
        turnover = sum(abs(target.get(s, 0) - weights.get(s, 0)) for s in set(target) | set(weights))
        value *= 1 - SLIPPAGE * turnover
        weights = target
        end = rebal[k + 1] if k + 1 < len(rebal) else closes.index[-1]
        seg = closes.loc[d:end, list(weights)] if weights else None
        if seg is None or seg.empty:
            equity.loc[d:end] = value
        else:
            rel = seg.ffill() / seg.iloc[0]
            cash = 1 - sum(weights.values())
            path = value * (cash + (rel * pd.Series(weights)).sum(axis=1))
            equity.loc[d:end] = path
            last = rel.iloc[-1]
            grown = {s: weights[s] * last[s] for s in weights}
            total = sum(grown.values()) + cash
            value = float(path.iloc[-1])
            weights = {s: v / total for s, v in grown.items()}
    return equity.loc[start:].dropna()


def stats(eq: pd.Series) -> dict:
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = 100 * ((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1)
    dd = 100 * float((1 - eq / eq.cummax()).max())
    yearly = {int(y): round(100 * (g.iloc[-1] / eq[eq.index < g.index[0]].iloc[-1] - 1), 1) if (eq.index < g.index[0]).any()
              else round(100 * (g.iloc[-1] / g.iloc[0] - 1), 1) for y, g in eq.groupby(eq.index.year)}
    sell = {}
    for k, (a, b) in SELLOFFS.items():
        w = eq.loc[a:b]
        if len(w) > 1:
            sell[k] = round(100 * (w.iloc[-1] / w.iloc[0] - 1), 1)
    return {"cagr": round(cagr, 1), "total": round(100 * (eq.iloc[-1] / eq.iloc[0] - 1), 0), "max_dd": round(dd, 1),
            "worst_year": min(yearly.values()), "yearly": yearly, "selloffs": sell,
            "start": str(eq.index[0].date()), "end": str(eq.index[-1].date())}


def main():
    closes = load_closes()
    spy = closes["SPY"]
    runs = {
        "Top 10, 12-month momentum": dict(lookback=231, skip=21, top_n=10),
        "Top 10, 6-month momentum": dict(lookback=126, skip=0, top_n=10),
        "Top 20, 12-month momentum": dict(lookback=231, skip=21, top_n=20),
        "Top 10, 12-month, cash when SPY < 200-day": dict(lookback=231, skip=21, top_n=10, trend_filter=True),
        "50% SPY + 50% top 10, 12-month": dict(lookback=231, skip=21, top_n=10, spy_weight=0.5),
        "CONTROL: equal weight, all stocks in list": dict(lookback=231, skip=21, top_n=0, pick="all"),
    }
    curves = {name: run(closes, spy, **kw) for name, kw in runs.items()}
    start = max(c.index[0] for c in curves.values())
    curves = {"Buy and hold SPY": spy.loc[start:] / spy.loc[start:].iloc[0], **{k: v.loc[start:] for k, v in curves.items()}}
    out = {}
    print(f"Monthly momentum vs SPY, {start.date()} to {closes.index[-1].date()}, {closes.shape[1] - 1} stocks, $2,500 start\n")
    for name, eq in curves.items():
        s = stats(eq)
        out[name] = s
        print(f"{name:44s} CAGR {s['cagr']:+5.1f}%/yr  total {s['total']:+6.0f}%  $2,500 -> ${2500 * (1 + s['total'] / 100):,.0f}  "
              f"maxDD {s['max_dd']:4.1f}%  worst year {s['worst_year']:+.1f}%")
        print("    by year: " + "  ".join(f"{y}:{v:+.1f}%" for y, v in s["yearly"].items()))
        print("    sell-offs: " + "  ".join(f"{k}: {v:+.1f}%" for k, v in s["selloffs"].items()))
    rand = sorted(stats(run(closes, spy, 231, 21, 10, pick="random", seed=s).loc[start:])["cagr"] for s in range(30))
    out["CONTROL: random 10 stocks"] = {"cagr_median": rand[15], "cagr_10th": rand[3], "cagr_90th": rand[26],
                                       "cagr_best": rand[-1]}
    print(f"{'CONTROL: random 10 stocks (30 tries)':44s} CAGR median {rand[15]:+5.1f}%/yr  "
          f"[10th {rand[3]:+.1f} .. 90th {rand[26]:+.1f}, best {rand[-1]:+.1f}]")
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "momentum_test.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
