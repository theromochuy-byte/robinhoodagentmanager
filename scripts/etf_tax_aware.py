#!/usr/bin/env python3
"""Tax-aware ETF rotation vs. plain rotation vs. buy-and-hold SPY, in a taxable account
(research/backtest only). Rules were fixed before running; no tuning.

Signal: 12-1 month momentum over 9 sector SPDRs + TLT + IEF + GLD, checked at each month-end,
traded at the next day's open with SLIPPAGE per side.
  plain      hold the top 3, equal weight, fully rebalanced every month
  tax-aware  hold 3; sell a holding only when it ranks below 6th, and then only if it is at a loss,
             or every lot is over a year old, or it ranks in the bottom 3. Sale proceeds go to the
             best-ranked fund not held. No trimming back to equal weight.
  SPY        buy once, hold.
Taxes: lot-level FIFO; short-term gains 25%, long-term 15%; losses offset gains and carry forward;
paid each January by selling holdings pro rata. Every strategy is liquidated at the end and pays
its final tax, so deferred tax is not mistaken for return.
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
from etf_audit import UNIVERSE, load

REPORTS = ROOT / "reports"
SLIPPAGE = 0.001
ST_RATE, LT_RATE = 0.25, 0.15
HOLD, KEEP_RANK, FORCE_RANK = 3, 6, 9
LOOK, SKIP = 231, 21


class Book:
    def __init__(self):
        self.lots: dict[str, list[list]] = {}   # sym -> [[shares, cost_per_share, buy_date], ...]
        self.cash = 1.0
        self.st = self.lt = 0.0
        self.carry = 0.0
        self.trades = 0

    def value(self, px) -> float:
        return self.cash + sum(q * px[s] for s, lots in self.lots.items() for q, _, _ in lots)

    def buy(self, sym, dollars, price, day):
        if dollars <= 0:
            return
        cost = dollars * SLIPPAGE
        self.cash -= dollars
        self.lots.setdefault(sym, []).append([(dollars - cost) / price, price, day])
        self.trades += 1

    def sell(self, sym, shares, price, day):
        remaining = shares
        proceeds = 0.0
        lots = self.lots.get(sym, [])
        while remaining > 1e-12 and lots:
            q, c, d = lots[0]
            take = min(q, remaining)
            gain = take * (price - c)
            if (day - d).days > 365:
                self.lt += gain
            else:
                self.st += gain
            proceeds += take * price
            remaining -= take
            if take >= q - 1e-12:
                lots.pop(0)
            else:
                lots[0][0] = q - take
        if not lots:
            self.lots.pop(sym, None)
        self.cash += proceeds * (1 - SLIPPAGE)
        self.trades += 1

    def tax_due(self) -> float:
        st, lt = self.st, self.lt
        if st < 0 and lt > 0:
            lt, st = lt + st, 0.0
        elif lt < 0 and st > 0:
            st, lt = st + lt, 0.0
        loss = -min(st, 0) - min(lt, 0)
        st, lt = max(st, 0), max(lt, 0)
        use = min(self.carry, st)
        st -= use
        use2 = min(self.carry - use, lt)
        lt -= use2
        self.carry = self.carry - use - use2 + loss
        self.st = self.lt = 0.0
        return ST_RATE * st + LT_RATE * lt

    def pay(self, amount, px):
        v = self.value(px)
        if amount <= 0 or v <= 0:
            return
        scale = 1 - amount / v
        self.cash *= scale
        for lots in self.lots.values():
            for lot in lots:
                lot[0] *= scale


def ranking(closes, i):
    m = closes.iloc[i - SKIP] / closes.iloc[i - LOOK - SKIP] - 1
    return list(m[UNIVERSE].sort_values(ascending=False).index)


def run(closes, opens, mode):
    idx = closes.index
    month_end = list(pd.Series(range(len(idx)), index=idx).groupby([idx.year, idx.month]).max())
    signals = {p + 1: p for p in month_end if LOOK + SKIP <= p < len(idx) - 1}
    first = min(signals)
    b, year, curve = Book(), idx[first].year, {}
    for t in range(first, len(idx)):
        day = idx[t]
        if day.year != year:
            b.pay(b.tax_due(), closes.iloc[t - 1])
            year = day.year
        if t in signals:
            px = opens.iloc[t]
            if mode == "spy":
                if not b.lots:
                    b.buy("SPY", b.cash, px["SPY"], day)
            else:
                rank = ranking(closes, signals[t])
                if mode == "plain":
                    top = rank[:HOLD]
                    target = b.value(px) / HOLD
                    for s in list(b.lots):
                        held = sum(q for q, _, _ in b.lots[s])
                        if s not in top:
                            b.sell(s, held, px[s], day)
                        elif held * px[s] > target:
                            b.sell(s, held - target / px[s], px[s], day)
                    short = {s: target - sum(q for q, _, _ in b.lots.get(s, [])) * px[s] for s in top}
                    need = sum(v for v in short.values() if v > 0)
                    for s, v in short.items():
                        if v > 0 and need > 0:
                            b.buy(s, b.cash * v / need, px[s], day)
                else:
                    for s in list(b.lots):
                        r = rank.index(s)
                        if r < KEEP_RANK:
                            continue
                        lots = b.lots[s]
                        gain = sum(q * (px[s] - c) for q, c, _ in lots)
                        all_long = all((day - d).days > 365 for _, _, d in lots)
                        if gain <= 0 or all_long or r >= FORCE_RANK:
                            b.sell(s, sum(q for q, _, _ in lots), px[s], day)
                    free = HOLD - len(b.lots)
                    picks = [s for s in rank if s not in b.lots][:free]
                    for k, s in enumerate(picks):
                        b.buy(s, b.cash / (free - k), px[s], day)
        curve[day] = b.value(closes.iloc[t])
    pre_tax = pd.Series(curve)
    last = closes.iloc[-1]
    for s in list(b.lots):
        b.sell(s, sum(q for q, _, _ in b.lots[s]), last[s], idx[-1])
    final_after_tax = b.cash - b.tax_due()
    return pre_tax, final_after_tax, b.trades


def main():
    closes, opens = load()
    out = {}
    print(f"Tax-aware ETF rotation, taxable account (ST {ST_RATE:.0%} / LT {LT_RATE:.0%}), "
          f"next-day open, {SLIPPAGE:.2%} slippage\n")
    for label, mode in (("Buy and hold SPY", "spy"), ("Plain rotation (top 3 monthly)", "plain"),
                        ("Tax-aware rotation", "tax_aware")):
        eq, after, trades = run(closes, opens, mode)
        s = E.stats(eq / eq.iloc[0])
        years = (eq.index[-1] - eq.index[0]).days / 365.25
        cagr_after = 100 * (after ** (1 / years) - 1)
        out[label] = {**s, "after_tax_cagr": round(cagr_after, 1), "after_tax_end_value": round(2500 * after),
                      "trades_per_year": round(trades / years, 1)}
        print(f"{label:32s} after-tax CAGR {cagr_after:+5.1f}%/yr  $2,500 -> ${2500 * after:>7,.0f} after all taxes   "
              f"maxDD {s['max_dd']:4.1f}%  worst yr {s['worst_year']:+5.1f}%  "
              f"2008 {s['yearly'].get(2008, float('nan')):+5.1f}%  2022 {s['yearly'].get(2022, float('nan')):+5.1f}%  "
              f"trades/yr {trades / years:4.1f}")
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "etf_tax_aware.json").write_text(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
