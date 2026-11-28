# Strategy Research Summary (November 2026)

**Verdict: in a taxable brokerage account, buying and holding SPY beat every strategy tested, after tax.** The only thing an active strategy reliably bought was smaller crashes. Research only, paper mode. Backtests, not a promise of future results, and not personalized financial advice.

## Decisive comparison: $2,500 over 21 years in a taxable account

Dec 2005 to Nov 2026 (includes 2008 and 2022). Next-day open fills, 0.10% slippage. Taxes: 25% short-term, 15% long-term, losses carried forward. Every strategy fully sold at the end and pays its final tax.

| Strategy | After-tax yearly return | $2,500 became | Worst drawdown | 2008 | 2022 |
|---|---|---|---|---|---|
| **Buy and hold SPY** | **+9.9%** | **$18,076** | 55.2% | -36.8% | -18.2% |
| ETF rotation (top 3 of 12 funds, monthly) | +8.3% | $13,307 | 22.4% | +6.2% | +5.1% |
| Tax-aware ETF rotation | +7.3% | $11,000 | 39.4% | -21.1% | +10.4% |

## Everything tested

| Strategy | Period | Yearly return | Worst drawdown | Verdict |
|---|---|---|---|---|
| Buy and hold SPY | 2005–2026 | +10.7% before tax, +9.9% after | 55% | Benchmark |
| ETF rotation: top 3 of 9 sectors + TLT + IEF + GLD | 2005–2026 | +10.6% before tax, +8.3% after | 22–35% | Smaller crashes; trails SPY after tax |
| 60% SPY / 40% IEF | 2005–2026 | +8.0% before tax | 33% | Simple, smoother alternative |
| Tax-aware ETF rotation | 2005–2026 | +7.3% after tax | 39% | Lost its crash protection |
| Stock momentum, top 10 monthly | 2015–2026 | +37.6% on paper | 39% | Hindsight-inflated; random picks from the same list made +16.5% |
| Pullback swing (RSI(2) < 10 in uptrend, 0.5% risk / 16 slots) | 2014–2026 | +5.3% before tax | 17.5% | Real but small edge; about -10% in 2018 and 2022 |
| Bearish head and shoulders (short, retest entry) | 2025–2026 | +0.04R/trade, 21 trades | n/a | Too few trades |
| Weekly volatile-stock dip buying (1H) | 2025–2026 | about 0R/trade after slippage | n/a | No edge |
| Pattern scanner, relaxed filters | 2025–2026 | -4.2% typical run | 15–19% | Loses under position limits |
| Current pattern scanner (live in paper) | 2025–2026 | -2.0% backtest; -$129 on 16 live paper trades | 7% | Loses and barely trades |

## ETF rotation audit (next-day open unless noted)

| Change | Yearly return | vs SPY, same rules | Meaning |
|---|---|---|---|
| Original (traded at signal close) | +11.2% | +0.5% | Starting point |
| Next-day open | +10.9% | +0.3% | No look-ahead problem |
| 0.10% slippage | +10.6% | 0.0% | Realistic costs |
| Other rebalance days | +10.2% to +11.3% | -0.5% to +0.7% | 2008 ranged -0.1% to +9.5%: crash protection partly timing luck |
| 11 neighbouring settings | +5.5% to +10.7% | -5.1% to +0.1% | Chosen setting was best of 12; expect 1–2%/yr below SPY |
| 2006–2015 only | +10.8% | +3.7% | Wins when a big crash happens |
| 2016–2026 only | +10.4% | -3.8% | Lags in rising markets |
| Taxable (25% flat) | +8.3% | -2.3% | Taxes are the biggest cost |

## Why active strategies lose in a brokerage account

- Edges were small: 0 to 1% a year before tax once hindsight and lucky settings were removed.
- Tax drag was larger: about 1.5 to 2.5% a year from realized, mostly short-term gains.
- Trading less to save tax (tax-aware rotation) held stocks through 2008 and finished last.
- Early results were inflated by best-of-many settings, same-close fills and stock lists made of today's winners.

## Recommendation

1. Brokerage account: a broad, low-cost index fund held long term beat everything here after tax; a 60/40 mix is the simple option if a 55% drop is too hard to hold through.
2. Keep the trading agent in paper mode as a research tool. No strategy met the go-live bar.
3. If an IRA becomes the target, revisit the ETF rotation: roughly SPY's return before tax with 24–35% drawdowns instead of 55%.
4. Stop tuning: adjusting settings mostly finds luck in past data.

## Method and limits

- Data: Robinhood daily/hourly bars for 185 stocks (Nov 2024–Nov 2026); adjusted daily history from Yahoo Finance via yfinance, downloaded in GitHub Actions (this environment blocks outside data sites): 188 stocks from 2014, 15 ETFs from 2003.
- Account: $2,500, cash account, slippage on every fill, no commission.
- Limits: stock lists are today's survivors; tax rates are assumptions; one 21-year history cannot promise the next crisis plays out the same way.
- Scripts: `scripts/compare_strategies.py`, `scripts/sizing_ranking_lab.py`, `scripts/filter_funnel.py`, `scripts/weekly_swing_lab.py`, `scripts/long_history_test.py`, `scripts/momentum_test.py`, `scripts/etf_rotation_test.py`, `scripts/etf_audit.py`, `scripts/etf_tax_aware.py`; long-history results print in the `Fetch Long History (research)` workflow log.
- Nothing in the live scanner, daily run or paper ledger was changed by this research.
