# Double Bottom — Pattern Knowledge Base

*Last updated: 2026-11-25. Sources: Bulkowski (thepatternsite), Gainzalgo, StockDataAnalytics.*

---

## Why Double Bottom Underperforms Inverse HnS in Our Data

**Break-even failure rates are nearly identical** in the literature (~12% double bottom vs ~11% inverse HnS). The gap in our results (29% win vs 46% win for retest entries) is a **trading structure problem, not a pattern-quality problem**.

**The structural difference:**

Inverse HnS has a built-in higher low — the right shoulder sits higher than the head. That's buyer strength confirmed *before* the neckline break. The neckline break is confirmation of an already-strengthening structure.

Double bottom has two equal lows. The second bottom doesn't demonstrate improving buyer strength — it just tests support again. Without a confirmed higher low, the pattern relies on the neckline break alone for confirmation, making it more vulnerable to false breaks.

> "Double bottom fails because price attempts to reverse without first printing a confirmed higher low." — Gainzalgo

---

## Volume Signature (Stricter Than Inverse HnS)

Volume matters more for double bottom because there's no structural price confirmation:

| Stage | Expected Volume |
|-------|----------------|
| First bottom | High / above avg (accumulation) |
| Middle rally | Declining |
| Second bottom | **Meaningfully lower than first** (contraction — key) |
| Neckline break | Expansion (≥ 1.5× avg) |

**Our scanner checks:** second bottom volume < first bottom volume ✓  
**Gap:** "below" is not tight enough — if second bottom volume is 95% of the first, that's not meaningful contraction. Consider requiring second bottom volume < 70% of first.

---

## Filter Status

| Filter | Status | Evidence |
|--------|--------|----------|
| Vol: 1st bottom above avg | ✅ Live scanner | Consensus |
| Vol: 2nd bottom < 1st bottom | ✅ Live scanner | Consensus |
| Vol: break bar ≥ 1.5× avg | ✅ Live scanner | Consensus |
| Vol: 2nd bottom < 70% of 1st | ❌ Not yet | Moderate — tighten contraction |
| Require 2nd bottom = higher low | ❌ Not yet | Would make it a mini inverse HnS |

---

## Decision: Keep or Drop?

**Don't tune it yet — wait for n ≥ 20 retest trades.**

Current backtest has 7 double bottom + retest trades — too few to know if the pattern is broken or just unlucky. Options when we have enough data:

1. Tighten the volume contraction requirement
2. Add a "higher second low" requirement (turns it into a mini inverse HnS structurally)
3. Remove it if it still underperforms after 20+ trades
