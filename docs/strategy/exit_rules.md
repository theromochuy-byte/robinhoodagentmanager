# Exit Rules — Research & Optimization Notes

*Last updated: 2026-11-25.*

---

## Current Exit Rules (live)

| Rule | Setting | Trigger |
|------|---------|---------|
| Fast loss | -0.75R | Within first 3 trading days, 1R never touched |
| Partial exit | 50% at 1R | Intraday high touches 1R; stop moves to entry + 0.15R |
| Full exit | 50% at 1.5R | Intraday high touches 1.5R |
| Early exit | Close at 6 trading days | < 0.5R touched (experimental filter) |
| Time stop | Close at 12 trading days | 1R never touched |

---

## Target: Is 1.5R Right?

**Short answer:** no rigorous study establishes an optimal R target. All guidance is practitioner convention.

**What Bulkowski shows for double bottoms:**
- Win rate at 1× measured-move target: ~56%
- Win rate at 4× measured-move target: ~38%
- Win rate declines as target grows — expected

**Measured move vs. fixed R:**
The measured move (neckline + pattern height) would be ~5–8% above neckline for our typical depth range. With a stop ~5–8% below neckline, that's roughly 1.5–2.5R depending on ATR. Our 1.5R is at the low end of the measured-move range — may be leaving money on the table when the pattern is strong.

**On partial exits (50% at 1R):**
Evidence is mixed:
- One backtest: full exits outperform partials because they don't cut winners short
- Another: partials raise win rate but reduce average return substantially
- Our winners (TXN, ZS, INTC) all hit 1.5R clean in 3–10 days — suggests they were strong enough for a 2R full target

**Experiments to run (priority order):**

1. **Full exit at 1.5R** vs current 50%@1R + 50%@1.5R — does dropping the partial improve avg R?
2. **Full exit at 2R** — do our winners have enough steam to reach it?
3. **8-day time stop** vs current 12-day — all losers showed zero progress by day 6; 8-day splits the difference

---

## Time Stop: 12 Days — Is It Too Long?

**Our backtest data says yes for losers:**
- All 4 time-stop losers (ETN, LLY, UPS, NEM) showed no meaningful movement past day 6
- Early-exit-at-6-days filter captured those without touching winners

**The early-exit filter already solves this partially** — it exits at 6 days if < 0.5R touched. So the 12-day time stop is a backstop for trades that showed some progress (touched 0.5R) but then stalled. That's a different scenario and may still need the full 12 days.

**Practical note:** 12 trading days = ~2.5 calendar weeks. That's a long time to be in a stalling trade. Consider: if 0.5R was touched but price has since retraced back below entry + 0.15R buffer, that's a signal to exit early regardless of day count.

---

## Retest Volume Filter (next planned addition)

**Concept:** On the retest candle (Entry B), volume should be drying up — neckline holding as support, not being attacked.

**Sources say:**
- Retest bar volume < 20-bar rolling average (two independent sources)
- Retest bar volume < breakout bar volume (implied by "drying up after surge")
- Heavy-volume retest = warning sign (neckline under pressure)

**Implementation target:** `swing_agent/simulator.py` → `build_retest_trade()`. Add check that the retest confirmation candle's volume is below the 20-bar average. This is a soft filter — if volume data is unavailable, skip the check.

**Status:** Not yet implemented. This is the next planned backtest experiment.

---

## Fibonacci Retracement Depth (future filter)

**Concept:** For pullback-within-uptrend context, the 1H correction should retrace 38–61.8% of the prior daily up-leg. Retracement < 38% = too shallow (may not have reset enough); > 61.8% = starts looking like a reversal, not a continuation.

**Current status:** Not implemented. Requires knowing the prior daily swing high and low at the time of the 1H pattern. Would need changes to `scan_symbol()` to pass daily OHLC context into the pattern evaluation.

**Priority:** Low — test other filters first. This requires more infrastructure than the volume check.
