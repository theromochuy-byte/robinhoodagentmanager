# Swing Agent: Robinhood Paper-Test Harness

A swing-trading research harness that screens for two bullish reversal setups
(double bottom and inverse head & shoulders), confirms the daily trend with the
20 EMA, and simulates proposed entries and exits to collect profit-and-loss data.
It runs in PAPER mode only. It never places a live order.

## How it fits together

```
Robinhood MCP (data only)            Python engine (this package)
  get_equity_historicals  ──▶  data/<SYM>_day.json   ─┐
  get_equity_historicals  ──▶  data/<SYM>_4hour.json ─┤
                                                      ▼
                          swing_agent.backtest  ──▶  data/paper_ledger.json
                                                 └─▶  reports/summary.json
```

The agent (Claude Code) is the only thing that talks to Robinhood. It fetches
bars with the MCP, writes the raw JSON to `data/`, then runs the Python engine,
which does no network I/O. That separation keeps the analytics offline and
testable, and keeps the order-placement tools out of the loop entirely.

## The strategy (v1, long only)

1. Daily trend bias: long only when the most recent daily bar is fully above the
   20 EMA (no touch).
2. Entry timeframe: 4-hour. Detect a double bottom or inverse head & shoulders
   whose neckline has been broken to the upside.
3. Confluence: keep a signal only if the daily bias was long at the moment the
   neckline broke.
4. Entry: close of the breakout bar. Stop: the pattern's defining swing low minus
   1 x ATR(14). Targets: prior structure high (primary), plus 1R and 2R logged
   for comparison.
5. Sizing: risk 1% of paper equity per trade.

All rules live in `CLAUDE.md` and the detector parameters in `swing_agent/patterns.py`.

## Run it in Claude Code

1. Confirm the connector: `claude mcp add robinhood-trading --transport http https://agent.robinhood.com/mcp/trading`, then `/mcp` to authenticate.
2. Have the agent fetch data for each symbol in `data/universe.txt`. For each symbol call `get_equity_historicals` twice and save the raw JSON:
   - daily: `interval="day"`, `start_time` about 12 months back, save to `data/<SYM>_day.json`
   - 4-hour: `interval="4hour"`, `start_time` about 6 to 12 months back, save to `data/<SYM>_4hour.json`
   - leave `adjustment_type` at its default (`split`), which is correct for backtesting
3. Run the engine: `python3 -m swing_agent.backtest AAPL MSFT NVDA ...` (or pass the whole universe).
4. Review `data/paper_ledger.json` (every proposed trade) and `reports/summary.json` (win rate, average R, expectancy, dollar P/L, split by pattern).

## Validate the engine offline

`python3 make_fixtures.py && python3 -m swing_agent.backtest TEST` builds a
controlled series containing one of each pattern and runs the full pipeline, so
you can confirm detection, sizing, simulation, and reporting without pulling live
data.

## Files

- `CLAUDE.md` ............ strategy and safety rules the agent must follow
- `swing_agent/indicators.py` .. EMA, ATR, swing-pivot detection
- `swing_agent/patterns.py` ... double bottom, inverse H&S, daily bias
- `swing_agent/simulator.py` .. trade construction, forward simulation, stats
- `swing_agent/dataio.py` ..... parse Robinhood MCP JSON into clean OHLC
- `swing_agent/backtest.py` ... the end-to-end runner
- `make_fixtures.py` ......... offline validation fixtures
- `data/universe.txt` ........ the symbols to scan

## Known tuning items (surfaced during validation)

- One position per symbol at a time: de-duplicate when both detectors fire on the
  same breakout, and avoid opening a new trade while one is open on that symbol.
- Exit rule comparison: the simulator already records 1R and 2R levels. Run the
  same signals under each exit rule to see which is best before committing.
- Entry B (neckline retest): v1 enters on the breakout close. Adding the retest
  entry will change fills and R, and is worth testing as a variant.
- Pattern tolerances (shoulder symmetry, bottom equality, swing window) live at
  the top of each detector and should be tuned against your reviewed results.

## What this does not do

No live orders. No options, crypto, or futures. No external data feed. Robinhood
is read-only here, used for price history and account context only.
