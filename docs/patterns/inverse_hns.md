# Inverse Head & Shoulders — Pattern Knowledge Base

*Last updated: 2026-11-24. Sources: StockDataAnalytics (67,041 automated trades), Bulkowski (hand-selected patterns), practitioner literature. No peer-reviewed academic literature exists specifically on this pattern. All statistics are directional, not definitive.*

---

## Win-Rate Reality Check

Widely cited "89–98% accurate" figures are marketing. What the data actually shows:

| Source | Win Rate | Caveats |
|--------|----------|---------|
| Bulkowski | 71% hit measured-move target; 11% outright failure | Stocks, unlimited hold, hand-selected |
| StockDataAnalytics (67k trades) | ~47% unfiltered → up to 77% with quality score ≥30 | Vendor self-report |
| Independent 20-session backtest (138 trades) | 41% gained ≥1R | Fixed 12-day horizon — most comparable to ours |
| **Our backtest (base, n=11)** | **46% win, +0.069R avg** | 1H bars, daily bias, 2yr data |
| **Our backtest (filtered, n=6)** | **83% win, +0.808R avg** | +depth cap + gap cap + early exit — small sample |

**Takeaway:** 40–55% win rate is realistic unfiltered. Quality filtering can push substantially higher. Our 83% (n=6) is directionally consistent with what large-scale filtering achieves — but it's too small to trust.

---

## Why Entry B (Retest) Beats Entry A (Breakout)

~60% of inverse HnS patterns produce a **throwback to the neckline** after the initial break. This is the norm, not the exception.

**Breakout (Entry A) weaknesses:**
- Buys before the throwback — often walks straight into a pullback
- Entry gap above neckline is baked in from the start
- Single-bar close above neckline is weak confirmation in choppy markets

**Retest (Entry B) advantages:**
- Tests neckline-as-support in real time — confirmation, not just the break
- Entry price closer to neckline → tighter stop → better R:R
- Filters patterns where the break was momentum-only and unsustained
- Our gap filter (≤1.2% above neckline) ensures the "retest" is an actual neckline test, not a pullback into mid-pattern space

**Our data confirms:** LRCX (+1.76% gap) and MRNA (+3.05% gap) both lost. The wider the gap at retest entry, the less the neckline is acting as true support.

**One counterpoint:** StockDataAnalytics reported clean breakouts won ~85% while retests won ~43% — but they define "retest" as a *failed* throwback (price returns and continues down). Their "clean breakout" selects the strongest patterns. That's classifying failure modes, not choosing entry style.

---

## Pattern Depth — The Most Important Filter

**StockDataAnalytics (67k trades) by right-shoulder depth below neckline:**

| Depth Range | Win Rate | Notes |
|-------------|----------|-------|
| Shallow: 1.6%–5.7% | 32.8% | 19,477 trades — worst bucket |
| Medium: ~5.7%–10% | 53.9% | Best-performing bucket |
| Deep: >10% | Declining | R:R degrades, needs too much recovery |

**Our backtest data:** All 5 winners had depth ≤ 0.086 (8.6%). Losers clustered at ≥ 0.090.

**Combined evidence gives a range: 4%–8.7%.** 
- Too shallow (< 4%) → sellers didn't capitulate enough
- Too deep (> 8.7%) → entry-to-stop risk too wide, pattern needs too much recovery

**Current scanner:** `depth > 0.03` (too low a floor). The experimental filter caps at `0.087`. Both filters together give the validated range.

---

## Volume Signature — Stage by Stage

Ideal sequence (consensus across sources):

| Stage | Expected Volume | Signal |
|-------|----------------|--------|
| Left shoulder decline | High / above avg | Initial selling pressure |
| Left shoulder rally | Declining as price rises | Normal — buyers absorbing |
| Head decline | **High ≥ left shoulder** | Climactic selling / capitulation |
| Head rally | Rising | Buyers absorbing the low |
| Right shoulder decline | **Lower than head** | Key — sellers exhausted |
| Right shoulder rally | Lower than head | Coiling / building |
| **Neckline break** | **Surge ≥ 1.5× 20-bar avg** | Institutional participation |
| Post-break retest | **Drying up / light** | Healthy — consolidating at neckline |
| Continuation | Rising | Buyers in control |

**Our scanner checks (✓ already implemented):**
- Head volume ≥ left shoulder ✓
- Right shoulder volume < head ✓
- Breakout volume > rolling average ✓

**Not yet checked:** Volume drying up during the retest candle itself. A high-volume retest candle can mean the neckline is under pressure, not simply being tested. Soft filter candidate.

---

## Neckline Slope

- **Upward-sloping (peak2 ≥ peak1):** Favored. Buyers stepping in at progressively higher prices between head and right shoulder. Reinforces bullish structure.
- **Flat:** Standard and reliable.
- **Downward-sloping:** Suspect for the bullish inverse version. A declining neckline means highs between shoulders are getting lower — not bullish.

**Current scanner:** `neckline = max(peak1, peak2)` — no slope check.  
**Potential filter:** Require `peak2 ≥ peak1` (upward slope). Not yet tested on our data.

---

## Time & Price Symmetry

**Bulkowski's data:**
- Median left-shoulder-to-head gap: 28 days
- Median head-to-right-shoulder gap: 29 days — nearly symmetric
- **Extended right shoulder reduces performance.** A right shoulder that takes much longer to form than the left-shoulder-to-head segment predicts worse outcomes.

**Price symmetry:** Right shoulder at approximately the same level as left is the rule. A slightly higher right shoulder (higher low) is acceptable and mildly bullish. A dramatically higher or lower right shoulder weakens the pattern.

**Our scanner:** `shoulder_tol = 0.06` (6% relative difference). Consistent with "imperfect patterns allowed."  
**Potential filter:** Right shoulder time span ≤ 1.5× the left-shoulder-to-head span. Not yet implemented.

---

## Known Failure Modes (ranked)

1. **Low-volume neckline break.** Most cited. Closes above neckline on below-average volume often resolve back into the range. Our ≥1.5× avg threshold addresses this.

2. **Wrong market context.** Pattern forming against a larger downtrend. Bear markets suppress success. Our daily-bias filter screens this.

3. **Very shallow right shoulder** (< 4% depth). Seller capitulation wasn't complete. StockDataAnalytics: 32.8% win rate. Addressable with a depth floor.

4. **Entry gap too wide.** "Retest" that fires 2–3% above neckline isn't a true neckline test. Our ≤1.2% gap filter addresses this.

5. **Extended right shoulder.** Much wider in time than left shoulder. Suggests indecision, not coiling. Time symmetry filter candidate.

6. **Pattern at prior resistance.** Neckline sitting at a major prior resistance level causes stalls. Daily-bias filter partially screens (above 20 EMA, intact higher highs).

---

## Our Specific Context: Pullback Reversal vs. Full Reversal

The literature focuses on inverse HnS as a *full downtrend-to-uptrend reversal*. We use it differently:

- Daily chart already bullish (above 20 EMA, intact swing lows)
- 1H pattern forms as a corrective pullback within the larger uptrend
- We're timing a **re-entry into the trend**, not catching a full trend reversal

This is arguably the best-possible context because the larger trend is already confirmed in our favor. It likely explains why our filtered results exceed literature averages. The pattern is doing less heavy lifting.

---

## Filter Status & Evidence Rating

| Filter | In Codebase | Evidence |
|--------|-------------|----------|
| Depth 4%–8.7% (inverse HnS cap at 8.7%) | ✅ Experimental (`--filters`) | Strong — StockDataAnalytics + our data |
| Retest gap ≤ 1.2% above neckline | ✅ Experimental (`--filters`) | Moderate — our data only, n=11 |
| Early exit at 6 days / no 0.5R progress | ✅ Experimental (`--filters`) | Moderate — our data only |
| Volume ≥ 1.5× avg on break bar | ✅ Live scanner | Multi-source consensus |
| Head vol ≥ left shoulder | ✅ Live scanner | Consensus |
| Right shoulder vol < head | ✅ Live scanner | Consensus |
| Depth floor > 3% (current) → raise to 4% | ❌ Not yet | StockDataAnalytics data |
| Peak2 ≥ peak1 (upward-sloping neckline) | ❌ Tested, **rejected** — eliminated all winners | Moderate in literature; inverse in our data |
| Right shoulder span ≤ 1.5× left-to-head span | ❌ Tested, **rejected** — eliminated all winners | Moderate in literature; inverse in our data |
| Retest candle volume drying up (< 20-bar avg AND < breakout bar) | ❌ Next to implement | Moderate — two independent sources confirm the concept |

---

## Ascending Triangle Backtest Summary (2026-11-25)

After fixing type-specific depth bounds (0.5%–4% for ascending triangles vs 3%–12% for inverse HnS):

| Entry | n | Win% | Avg R | Fast-loss rate |
|-------|---|------|-------|----------------|
| A (breakout) | 109 | 45% | -0.129R | 27% |
| B (retest)   | 87  | 40% | -0.209R | 33% |

Win rates are acceptable but **avg R is negative on both entries**. Fast-loss exits (29 each) are the primary drag — 33% fast-loss rate vs only 9% for inverse HnS/B.

**Root cause:** The -0.75R fast-loss rule was calibrated for inverse HnS retest entries where the neckline is proven support. Ascending triangles have tighter stops (0.5-2% depth), so a small adverse move hits -0.75R quickly. The fast-loss threshold that filters noise for loose-stop patterns is too aggressive for tight-stop patterns.

**Follow-up: removed fast-loss rule for ascending triangles (2026-11-25):**

| Entry | Before (w/ fast-loss) | After (no fast-loss) |
|-------|----------------------|----------------------|
| A (breakout) | n=109, 45% win, -0.129R | n=109, 48% win, -0.064R |
| B (retest)   | n=87,  40% win, -0.209R | n=87,  45% win, -0.129R |

Removing fast-loss improved results (+3-5pp win rate, +0.07-0.08R) but avg R is still negative on both. Stop exits now dominate — ~49% of trades reach the full stop. Root cause: the ATR-buffered stop below the last higher low is getting hit on normal consolidation before the move materializes.

**Remaining hypotheses to test before live scanner:**
1. Wider stop: use 1.5× ATR buffer instead of 1× (last higher low gets clipped by tight consolidation)
2. Measured-move exit: pattern height added to resistance (~2-4R) instead of fixed 1.5R — AT measured move is structurally larger than inverse HnS
3. Daily timeframe AT only: 1H may be too noisy for a continuation pattern that needs room

**Decision:** Do not add ascending triangles to the live scanner yet.

---

## Next Backtest Experiments (priority order)

1. **Raise depth floor to 4%** — StockDataAnalytics shows < 4% wins only 32.8%
2. ~~**Add peak2 ≥ peak1 neckline slope check**~~ — **TESTED 2026-11-25: rejected.** Eliminated all 5 winners. Hypothesis: in uptrend pullback context, daily bias already provides the slope — the 1H neckline doesn't need to slope up. The slope filter is a full-reversal concept.
3. ~~**Add right shoulder time symmetry check**~~ — **TESTED 2026-11-25: rejected.** Same result — Bulkowski's data is daily charts; 1H pullbacks have different timing dynamics.
4. **Volume at retest candle** — require retest bar volume < 20-bar avg AND < breakout bar volume; implement in `simulator.py build_retest_trade()`
5. **Get to n ≥ 30 inverse HnS + retest** before trusting any win-rate figures
6. **Fibonacci retracement depth** (lower priority) — 1H correction should be 38–61.8% of prior daily up-leg

---

## Sources

- [StockDataAnalytics: 67,041 trades inverse HnS analysis](https://www.stockdataanalytics.com/blog/inverse-head-and-shoulders-pattern-67000-trade-analysis/)
- [Bulkowski via Liberated Stock Trader](https://www.liberatedstocktrader.com/inverse-head-shoulders/)
- [Pattern depth & win rate — InvestingGoal](https://investingoal.com/trading/chart-pattern/inverse-head-and-shoulders/)
- [~60% throwback rate — JournalPlus](https://journalplus.co/patterns/inverse-head-and-shoulders/)
- [Neckline slope — thinkorswim](https://toslc.thinkorswim.com/center/reference/Patterns/Classic-Patterns/Inverse-Head-and-Shoulders.html)
- [Volume signature — CMC Markets](https://www.cmcmarkets.com/en-gb/trading-strategy/inverse-head-and-shoulders-patterns)
- [Time symmetry — Bulkowski/thepatternsite](https://thepatternsite.com/HSBSym.html)
- [Market context — Capital.com](https://capital.com/en-gb/learn/technical-analysis/inverse-head-and-shoulders-pattern)
- [Failure modes — Market Bulls](https://market-bulls.com/failed-head-and-shoulders-pattern/)
- [Neckline slope (upward) — Seeking Alpha](https://seekingalpha.com)
