#!/usr/bin/env python3
"""Monthly ETF rotation strategies vs. buy-and-hold (research/backtest only).

Reads adjusted daily bars from data/hist_etf/. Every fund here existed through 2008, so
there is no hindsight in the fund list. Rebalances at each month-end close, holdings drift
between rebalances, SLIPPAGE is charged on traded value.

Momentum = return over the 12 months ending one month ago (231 trading days, skipping 21).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from swing_agent.dataio import bars_to_df

ETF = ROOT / "data" / "hist_etf"
REPORTS = ROOT / "reports"
SLIPPAGE = 0.0005
LOOKBACK, SKIP = 231, 21
SECTORS = ["XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB"]
DEFENSIVE = ["TLT", "IEF", "GLD"]
SYMBOLS = SECTORS + DEFENSIVE + ["SPY", "EFA", "SHY"]
SELLOFFS = {
    "2008 financial crisis": ("2007-10-09", "2009-03-09"),
    "Q4 2018 sell-off": ("2018-10-01", "2018-12-24"),
    "COVID crash": ("2020-02-19", "2020-03-23"),
    "2022 bear market": ("2022-01-03", "2022-10-12"),
}


def load() -> pd.DataFrame:
    cols = {}
    for s in SYMBOLS:
        d = bars_to_df(json.load(open(ETF / f"{s}_day.json")))
        cols[s] = pd.Series(d["close"].values, index=pd.to_datetime(d["begins_at"]).dt.normalize())
    return pd.DataFrame(cols).sort_index().dropna()


def momentum(closes: pd.DataFrame, i: int) -> pd.Series:
    return closes.iloc[i - SKIP] / closes.iloc[i - LOOKBACK - SKIP] - 1


def fixed(weights):
    return lambda closes, i: dict(weights)


def top_n(cands, n, absolute=False, fallback="IEF"):
    def pick(closes, i):
        m = momentum(closes, i)
        best = list(m[cands].nlargest(n).index)
        out = {}
        for s in best:
            key = fallback if absolute and m[s] <= m["SHY"] else s
            out[key] = out.get(key, 0) + 1 / n
        return out
    return pick


def dual_momentum(closes, i):
    m = momentum(closes, i)
    if m["SPY"] <= m["SHY"]:
        return {"IEF": 1.0}
    return {"SPY" if m["SPY"] >= m["EFA"] else "EFA": 1.0}


def rotate(closes: pd.DataFrame, selector) -> pd.Series:
    idx = closes.index
    ends = list(pd.Series(idx, index=idx).groupby([idx.year, idx.month]).max())
    ends = [d for d in ends if idx.get_loc(d) >= LOOKBACK + SKIP]
    equity = pd.Series(index=idx, dtype=float)
    value, weights = 1.0, {}
    for k, d in enumerate(ends):
        target = selector(closes, idx.get_loc(d))
        turnover = sum(abs(target.get(s, 0) - weights.get(s, 0)) for s in set(target) | set(weights))
        value *= 1 - SLIPPAGE * turnover
        end = ends[k + 1] if k + 1 < len(ends) else idx[-1]
        seg = closes.loc[d:end, list(target)]
        rel = seg / seg.iloc[0]
        path = value * (rel * pd.Series(target)).sum(axis=1)
        equity.loc[d:end] = path
        grown = {s: target[s] * rel[s].iloc[-1] for s in target}
        total = sum(grown.values())
        value = float(path.iloc[-1])
        weights = {s: v / total for s, v in grown.items()}
    return equity.dropna()


def stats(eq: pd.Series) -> dict:
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    yearly = {}
    prev = eq.iloc[0]
    for y, g in eq.groupby(eq.index.year):
        yearly[int(y)] = round(100 * (g.iloc[-1] / prev - 1), 1)
        prev = g.iloc[-1]
    sell = {k: round(100 * (eq.loc[a:b].iloc[-1] / eq.loc[a:b].iloc[0] - 1), 1)
            for k, (a, b) in SELLOFFS.items() if len(eq.loc[a:b]) > 1}
    return {"cagr": round(100 * ((eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1), 1),
            "max_dd": round(100 * float((1 - eq / eq.cummax()).max()), 1),
            "worst_year": min(yearly.values()), "losing_years": int(sum(v < 0 for v in yearly.values())),
            "end_value": int(round(2500 * eq.iloc[-1] / eq.iloc[0])), "yearly": yearly, "selloffs": sell}


def main():
    closes = load()
    strategies = {
        "Buy and hold SPY": fixed({"SPY": 1.0}),
        "60% SPY / 40% bonds (IEF)": fixed({"SPY": 0.6, "IEF": 0.4}),
        "Top 3 sectors": top_n(SECTORS, 3),
        "Top 3 of sectors + bonds + gold": top_n(SECTORS + DEFENSIVE, 3),
        "Top 3 sectors, weak ones -> bonds": top_n(SECTORS, 3, absolute=True),
        "Top 3 of all, weak ones -> bonds": top_n(SECTORS + DEFENSIVE, 3, absolute=True),
        "Dual momentum (SPY / EFA / bonds)": dual_momentum,
    }
    curves = {k: rotate(closes, f) for k, f in strategies.items()}
    start = max(c.index[0] for c in curves.values())
    curves = {k: c.loc[start:] for k, c in curves.items()}
    print(f"ETF rotation, {start.date()} to {closes.index[-1].date()}, $2,500 start, monthly rebalance\n")
    out = {}
    for k, eq in curves.items():
        s = stats(eq)
        out[k] = s
        print(f"{k:36s} CAGR {s['cagr']:+5.1f}%/yr  $2,500 -> ${s['end_value']:>7,}  maxDD {s['max_dd']:4.1f}%  "
              f"worst year {s['worst_year']:+5.1f}%  losing years {s['losing_years']}")
        print("    sell-offs: " + "  ".join(f"{n}: {v:+.1f}%" for n, v in s["selloffs"].items()))
        print("    by year: " + "  ".join(f"{y}:{v:+.0f}" for y, v in s["yearly"].items()))
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "etf_rotation_test.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
