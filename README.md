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

## Options premium-collection agent

A second, independent strategy lives in `premium_agent/`: the wheel (cash-secured
puts, plus covered calls on any resulting assignment). Same safety posture —
PAPER mode only, read-only option tools, every decision logged before anything
else happens with it.

- `CLAUDE_OPTIONS.md` ......... strategy and safety rules (first draft, needs review)
- `premium_agent/dataio.py` ... parse Robinhood option contract+quote JSON
- `premium_agent/quality_screen.py` . Step 1 fundamental/liquidity gate (volume, P/E, 52wk range, trailing growth) + dividend lookup
- `premium_agent/trend.py` .... Step 1 technical gate: price above 20/50-day SMA, from local daily bars
- `premium_agent/screener.py` . CSP / covered-call candidate screening (delta, DTE, liquidity, min yield)
- `premium_agent/realized_vol.py` . IV-richness proxy from the swing agent's daily bars
- `premium_agent/positions.py` . current "shares owned" state from CSP assignment (routes Step 5 vs. Step 1-3)
- `premium_agent/ledger.py` ... write PROPOSED trades, roll chains, deployed-collateral accounting
- `premium_agent/scan.py` ..... **the end-to-end runner** -- screens the universe, proposes sizing-capped trades
- `data/options_config.json` .. paper equity, sizing caps, delta/DTE targets, quality-screen thresholds
- `data/options_universe.txt` . candidate symbols -- a live Robinhood-scanner snapshot, not a hand-curated list (see below)

## Running the options agent

Unlike the swing agent's `data/universe.txt` (S&P 500, mostly $100+ names),
the options agent needs its own universe: Step 6's own sizing caps limit a
single CSP contract to roughly a $25 strike, so most large-caps structurally
don't fit this account regardless of the Step 1 price floor. Robinhood's
native scanner (`get_scanner_filter_specs` / `create_scan` / `run_scan`) is
the "no third-party screener" way to build that universe --
`data/options_universe.txt` is a snapshot from one (price $8-$60, 30d avg
volume > 2M, 30d avg options volume > 500), not something to hand-edit;
refresh it by re-running the scan periodically.

Each cycle, the agent:
1. Fetches `get_equity_fundamentals` + `get_equity_quotes` + `get_financials`
   (annual, limit 2) + `get_earnings_calendar` for the universe, and saves
   one combined record per symbol to `data/options/universe_snapshot.json`:
   `{"SYMBOL": {"fundamentals": {...}, "price": ..., "financials_annual": [...],
   "next_earnings_date": "YYYY-MM-DD" or null}}`.
2. For each symbol still worth pricing options on: resolves the chain with
   `get_option_chains`, lists strikes in the DTE window with
   `get_option_instruments`, batches `get_option_quotes` for those instrument
   ids, merges each instrument with its quote, and saves the list to
   `data/options/<SYMBOL>_<EXPIRATION>.json` (`premium_agent/dataio.py`'s
   documented record shape).
3. Runs `python3 -m premium_agent.scan`, which is pure Python from here --
   Step 1 quality/trend/growth gates, CSP/covered-call screening, Step 6
   sizing caps, and proposing the winners to
   `data/paper_options_ledger.json` all happen offline, reading only what
   was fetched and saved in steps 1-2. Held positions
   (`data/options_positions.json`) get screened for covered calls; everything
   else gets screened for cash-secured puts.

Screening the whole universe every cycle is real work, not a formality: a
first live run against F, T, and SOFI rejected F (a real 2025 earnings
collapse) and SOFI (P/E over 30, real earnings decline) on Step 1, and found
T's only in-delta-band strike paid 0.93% over 39 days -- just under the 1%
yield floor. Zero trades proposed. That's the gate working, not a bug; the
strategy doesn't force a trade to have something to show for a cycle.

`CLAUDE_OPTIONS.md` has the full rules and flags the open data gap (no
historical IV series from this MCP) plus `max_rolls_before_assignment`,
which still needs a real number before this runs unattended.
