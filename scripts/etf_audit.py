#!/usr/bin/env python3
"""Live-realism audit of the ETF rotation (research/backtest only).

Locks in the reported rule ("top 3 of 9 sectors + TLT + IEF + GLD by 12-1 month momentum,
equal weight, monthly") and re-tests it with frictions a live account would face:
  - execution one trading day after the signal, at the open or the close
  - higher slippage
  - rebalancing on other days of the month
  - neighbouring lookbacks and holding counts (is the chosen setting a lucky pick?)
  - a taxable account (25% on net realized gains each year, losses carried forward)
  - two separate halves of history
SPY is always run through the same machinery for comparison.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import etf_rotation_test as E
from swing_agent.dataio import bars_to_df

REPORTS = ROOT / "reports"
UNIVERSE = E.SECTORS + E.DEFENSIVE


def load():
    closes, opens = {}, {}
    for s in E.SYMBOLS:
        d = bars_to_df(json.load(open(E.ETF / f"{s}_day.json")))
        ix = pd.to_datetime(d["begins_at"]).dt.normalize()
        closes[s] = pd.Series(d["close"].values, index=ix)
        opens[s] = pd.Series(d["open"].values, index=ix)
    c = pd.DataFrame(closes).sort_index().dropna()
    o = pd.DataFrame(opens).reindex(c.index)
    return c, o


def top_n(n, months, skip=21):
    look = int(round(months * 21)) - skip if months == 12 else int(round(months * 21))
    sk = skip if months == 12 else 0

    def pick(closes, i):
        m = closes.iloc[i - sk] / closes.iloc[i - look - sk] - 1
        best = list(m[UNIVERSE].nlargest(n).index)
        return {s: 1 / n for s in best}, look + sk
    return pick


def spy(closes, i):
    return {"SPY": 1.0}, 0


def simulate(closes, opens, selector, lag=1, fill="close", slippage=0.0005, offset=0, tax=0.0, warmup=252):
    idx = closes.index
    month_end = list(pd.Series(range(len(idx)), index=idx).groupby([idx.year, idx.month]).max())
    signals = [p + offset for p in month_end if warmup <= p + offset < len(idx) - lag]
    exec_at = {s + lag: s for s in signals}
    shares, basis, cash = {}, {}, 1.0
    realized, carry, year = 0.0, 0.0, idx[signals[0]].year
    curve = {}
    started = False
    for t in range(signals[0], len(idx)):
        day = idx[t]
        if day.year != year:
            if realized > 0:
                taxable = max(0.0, realized - carry)
                carry = max(0.0, carry - realized)
            else:
                taxable, carry = 0.0, carry - realized
            if tax and taxable > 0:
                prev = idx[t - 1]
                value = cash + sum(q * closes.at[prev, s] for s, q in shares.items())
                scale = max(0.0, 1 - tax * taxable / value) if value > 0 else 1.0
                shares = {s: q * scale for s, q in shares.items()}
                basis = {s: b * scale for s, b in basis.items()}
                cash *= scale
            realized, year = 0.0, day.year
        if t in exec_at:
            started = True
            target, _ = selector(closes, exec_at[t])
            px = (opens if fill == "open" else closes).iloc[t]
            value = cash + sum(q * px[s] for s, q in shares.items())
            new = {s: value * w / px[s] for s, w in target.items()}
            for s in set(shares) | set(new):
                old_q, new_q = shares.get(s, 0.0), new.get(s, 0.0)
                if new_q < old_q and old_q > 0:
                    sold_frac = (old_q - new_q) / old_q
                    realized += (old_q - new_q) * px[s] - basis[s] * sold_frac
                    basis[s] *= 1 - sold_frac
                elif new_q > old_q:
                    basis[s] = basis.get(s, 0.0) + (new_q - old_q) * px[s]
                if new_q == 0:
                    basis.pop(s, None)
            cash = value - sum(q * px[s] for s, q in new.items()) - slippage * sum(
                abs(new.get(s, 0.0) - shares.get(s, 0.0)) * px[s] for s in set(shares) | set(new))
            shares = new
        if started:
            curve[day] = cash + sum(q * closes.at[day, s] for s, q in shares.items())
    return pd.Series(curve)


def summary(eq, start=None, end=None):
    e = eq.loc[start:end] if (start or end) else eq
    s = E.stats(e / e.iloc[0])
    return s


def row(label, s, ref=None):
    extra = f"   vs SPY {s['cagr'] - ref['cagr']:+.1f}%/yr" if ref else ""
    return (f"  {label:42s} CAGR {s['cagr']:+5.1f}%/yr  maxDD {s['max_dd']:4.1f}%  worst yr {s['worst_year']:+5.1f}%  "
            f"2008 {s['yearly'].get(2008, float('nan')):+5.1f}%  2022 {s['yearly'].get(2022, float('nan')):+5.1f}%{extra}")


def main():
    closes, opens = load()
    base = top_n(3, 12)
    out = {}

    def both(label, **kw):
        st = summary(simulate(closes, opens, base, **kw))
        sp = summary(simulate(closes, opens, spy, **kw))
        out[label] = {"strategy": st, "spy": sp}
        print(row(label, st, sp))
        return st, sp

    print(f"ETF rotation audit, data {closes.index[0].date()} to {closes.index[-1].date()}\n")
    print("1) Execution timing (slippage 0.05%)")
    both("same-day close (original backtest)", lag=0)
    both("next-day close", lag=1)
    both("next-day open", lag=1, fill="open")

    print("\n2) Slippage per side (next-day open)")
    for sl in (0.0005, 0.001, 0.0025):
        both(f"slippage {sl:.2%}", lag=1, fill="open", slippage=sl)

    print("\n3) Rebalance day (next-day open, 0.10% slippage)")
    for off in (0, 5, 10, 15):
        both(f"{off} trading days after month-end", lag=1, fill="open", slippage=0.001, offset=off)

    print("\n4) Neighbouring settings (next-day open, 0.10% slippage) - is the chosen one a lucky pick?")
    sp = summary(simulate(closes, opens, spy, lag=1, fill="open", slippage=0.001))
    grid = {}
    for months in (3, 6, 9, 12):
        for n in (2, 3, 4):
            st = summary(simulate(closes, opens, top_n(n, months), lag=1, fill="open", slippage=0.001))
            grid[f"{months}m/top{n}"] = st
            print(row(f"{months}-month momentum, top {n}" + ("   <- chosen" if (months, n) == (12, 3) else ""), st, sp))
    out["grid"] = grid

    print("\n5) Taxes (next-day open, 0.10% slippage)")
    both("IRA / no tax", lag=1, fill="open", slippage=0.001)
    both("taxable account, 25% on net gains", lag=1, fill="open", slippage=0.001, tax=0.25)

    print("\n6) Two halves of history (next-day open, 0.10% slippage)")
    eq = simulate(closes, opens, base, lag=1, fill="open", slippage=0.001)
    eqs = simulate(closes, opens, spy, lag=1, fill="open", slippage=0.001)
    for a, b in (("2006-01-01", "2015-12-31"), ("2016-01-01", None)):
        if eq.loc[a:b].empty:
            continue
        st, ss = summary(eq, a, b), summary(eqs, a, b)
        out[f"half {a[:4]}"] = {"strategy": st, "spy": ss}
        print(row(f"{a[:4]}-{(b or 'now')[:4]}", st, ss))
        print(f"  {'  SPY same period':42s} CAGR {ss['cagr']:+5.1f}%/yr  maxDD {ss['max_dd']:4.1f}%")

    print("\n7) LIVE-LIKE HEADLINE: next-day open, 0.10% slippage, month-end, taxable 25%")
    st, ss = both("live-like, taxable", lag=1, fill="open", slippage=0.001, tax=0.25)
    print(f"  $2,500 -> ${st['end_value']:,}   (SPY bought and held, tax deferred until sold: ${ss['end_value']:,})")
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "etf_audit.json").write_text(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
