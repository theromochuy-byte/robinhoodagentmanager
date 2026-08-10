# Proposal: MA-crossover pullback entry for swing_agent

**Status: IMPLEMENTED, added alongside the existing system (signed off).**
Per sign-off: added as a second, separately-tagged trigger
(`type: "ma_crossover_pullback"`) rather than replacing double-bottom /
inverse-H&S, which keeps its real backtest history
(`reports/backtest/`) undisturbed. Both trigger types are now detected,
scanned, and backtested side by side so performance can actually be
compared instead of assumed.

**Resolved decisions** (were open questions in the draft, now settled):

1. **Timeframe**: the 20/50 crossover is computed on the *daily* chart as
   trend confirmation (`patterns.daily_ma_crossover_bias`); the pullback +
   candlestick trigger stays on the entry timeframe (4h). Matches
   `CLAUDE.md`'s existing "bias from one timeframe up" rule.
2. **EMA vs. SMA**: gates on EMA (reuses `indicators.ema()`, zero new code)
   — but computes and logs the SMA(20)/SMA(50) crossover state on every
   bias check too (`ema_agrees_with_sma` field), rather than requiring
   agreement. Requiring both to cross would be a rule the source never
   specified; logging both lets us compare EMA-only vs. SMA-confirmed
   performance later with real data instead of guessing which is better now.
3. **Stop/target**: reuses the existing rule exactly — swing low (here, the
   pullback low) minus 1×ATR(14) for the stop; prior structure high / 1R /
   2R for targets. No changes needed in `simulator.py`: the new pattern is
   shaped to match `detect_double_bottom`/`detect_inverse_hns`'s dict
   contract (`neckline` = the higher swing high, `stop_basis` = the pullback
   low) so `build_trade`/`resolve_trade` handle it unmodified.

**What was built:**

- `swing_agent/candlesticks.py` (new) — `is_hammer()`, `is_bullish_engulfing()`.
  Genuinely new code; nothing like this existed in the repo before.
- `swing_agent/patterns.py` — `daily_ma_crossover_bias()` and
  `detect_ma_crossover_pullback()`, reusing the existing `swing_points()` /
  `pivots()` fractal detection for the "higher swing high" and "first
  pullback" checks.
- `swing_agent/scanner.py` and `swing_agent/backtest.py` — wired in
  alongside the existing detectors. The new type is exempt from the old
  `bias_asof()` check (its own daily-crossover bias is baked into the
  detector) rather than double-gated by a bias rule it was never meant to
  satisfy.

**Verified two ways**, matching how the options agent's screener was
validated: a hand-built synthetic fixture (crossover → higher high →
pullback → hammer, correctly detected; a negative control with no crossover
yet, correctly empty; wired through `build_trade`/`resolve_trade` end to
end), then a real run against all 209 symbols with local data in this repo
— zero crashes, and two genuine real signals: GD (triggered today) and CHRW
(watching, pullback in progress), both with sane risk/reward geometry.

## What the source describes

A five-part checklist (a different "Cash Flow Academy" source than the
options videos, same channel), used as a general swing-entry system rather
than a specific reversal pattern:

1. **20/50 moving-average crossover** — the 20-period average crossing above
   the 50-period is treated as the new-uptrend signal.
2. **Higher swing high** — after the crossover, the stock must print a swing
   high above its prior swing high (structure confirmation).
3. **Buy the first pullback** — specifically the *first* dip after the
   crossover, not a later one. The logic given: earliest entry into a new
   trend has the most room left to run.
4. **Support confirmation on the pullback** — the pullback should be finding
   a floor at one of: the 20-period MA itself, an old resistance level now
   acting as support ("old ceiling becomes new floor"), or a trendline under
   the prior pivot lows.
5. **Candlestick reversal trigger** — don't buy the pullback itself; wait for
   a hammer (small body, long lower wick) or bullish-engulfing candle on the
   support zone as confirmation that buyers have returned, then enter.

Three things the source is explicit about, worth carrying into any review:

- It walked through three live examples (Tesla, Coinbase, Nvidia) picked via
  ChatGPT rather than cherry-picked, including one that met every criterion
  but *didn't* trend far (Tesla) and one that failed the criteria outright
  and correctly should have been skipped (Coinbase — crossover happened
  while the stock was breaking below its averages, no higher-high pullback).
  That's a real, if informal, out-of-sample-style check on the logic, not
  just a single cherry-picked winner.
- It also showed a losing example (earnings-related gap down after a
  seemingly valid setup) and was explicit that this is not a high-win-rate
  system — the source's own estimate is "maybe 50/50, I think better" — and
  that the edge comes from asymmetric reward (catching a trend early), not
  from a high hit rate.
- **No stop-loss or target rule was given.** The source mentions moving
  stops and taking losses in general terms but never specifies where the
  stop goes or what the target is. That's a real gap — resolved above by
  reusing the existing system's rule rather than guessing at a new one.

## How this compares to what's live today

| | Current (`CLAUDE.md` / `patterns.py`) | This proposal |
|---|---|---|
| Trend bias | Daily: price fully above 20 EMA (no touch) + swing-low structure intact | Daily: 20 EMA crosses above 50 EMA |
| Entry trigger | 4h: double-bottom or inverse-H&S neckline break | 4h: candlestick reversal (hammer / bullish engulfing) on a pullback that found support |
| Entry philosophy | Reversal pattern completing and breaking out | Continuation — buying the first dip in an already-confirmed new trend |
| Structure check | Pattern-specific (two comparable lows, or shoulder/head/shoulder) | General: higher swing high after the crossover |
| Stop / target | Swing low − 1×ATR(14); prior structure high, 1R, 2R (Step 4-5 of `CLAUDE.md`) | Same rule, reused as-is |

**What was reused:** `swing_agent/indicators.py`'s `swing_points()` /
`pivots()` already detects fractal highs/lows, which is exactly what
"higher swing high" needed — no new pivot-detection code required. `ema()`
already existed for the 20-period average; the 50-period average is the
same function with a different `period` argument.

**What's genuinely new:** candlestick pattern recognition
(`swing_agent/candlesticks.py` — hammer, bullish engulfing) didn't exist
anywhere in this codebase before this proposal.

## Known limitations, worth a look before this earns real trust

- The "old resistance becomes new support" check is a coarse proximity band
  (pullback landing within roughly -2%/+10% of a prior swing high), not true
  geometric resistance-zone detection. It's one of two ways `Step 4` support
  can be confirmed (the other is the fast EMA) — reasonable as a first pass,
  but the band width was picked to make the logic work, not derived from
  anything in the source.
- No trendline-under-the-lows support check (the source's third support
  type) — only the fast-MA and old-resistance checks were implemented.
- Backtest history for this trigger is exactly what the two real hits found
  during implementation testing (GD, CHRW) plus whatever the next scans
  turn up — there's no track record yet the way double-bottom/inverse-H&S
  has in `reports/backtest/`. Worth watching before trusting it the same way.
