"""Tunable strategy parameters — edit here, nowhere else.

All values can be changed between scan cycles without touching logic code.
After editing, the next run_daily invocation picks up the new values.
"""

# ── Time stop ──────────────────────────────────────────────────────────────────
# A trade is considered "stale" and exited if it has not shown a sign of working
# within this many *trading* days of entry.
#
# "Sign of working" means price has either:
#   (a) touched 1R at any point (tracked via touched_1r on the ledger), OR
#   (b) made progress of at least TIME_STOP_MIN_PROGRESS_FRAC of the way
#       toward 1R (e.g. 0.30 = 30% of the way from entry to 1R target).
#
# If NEITHER condition is met by the deadline, the position is closed at the
# current price and logged as "time_stop".
#
# Tune TIME_STOP_TRADING_DAYS first; tighten TIME_STOP_MIN_PROGRESS_FRAC if
# you want to catch earlier "nothing is happening" behaviour.
TIME_STOP_TRADING_DAYS: int   = 12    # trading days before stale-trade exit fires
TIME_STOP_MIN_PROGRESS_FRAC: float = 0.25  # 0.0 = only touched_1r counts (original behaviour)
                                            # 0.25 = also exits if < 25% of way to 1R after deadline

# ── Entry timeframe ────────────────────────────────────────────────────────────
ENTRY_TIMEFRAME: str = "1hour"  # bar size used by scanner for pattern detection
FRESHNESS_BARS: int  = 36       # max bars since neckline break to qualify (36×1H ≈ 4.5 trading days)

# ── Scanner quality / entry caps ───────────────────────────────────────────────
MIN_QUALITY_SCORE: float = 0.45  # skip triggered setups below this threshold
MAX_ENTRIES_PER_DAY: int  = 2    # cap new paper positions opened per scan run

# ── Risk / sizing ──────────────────────────────────────────────────────────────
RISK_PCT: float = 0.02          # fraction of starting equity risked per trade
STARTING_EQUITY: float = 2500.0

# ── Partial exit / target structure ───────────────────────────────────────────
# Full target is 1.5R. At 1R, 50% of shares are exited and the stop moves to
# entry + BREAKEVEN_BUFFER_R (a small buffer above entry). The remaining 50%
# runs to 1.5R or gets stopped at the buffer.
TARGET_FULL_R: float     = 1.5   # full exit target (replaces 2R)
PARTIAL_EXIT_R: float    = 1.0   # first exit at this R level
PARTIAL_EXIT_FRAC: float = 0.5   # fraction of shares to exit at PARTIAL_EXIT_R
BREAKEVEN_BUFFER_R: float = 0.15 # stop moves to entry + this fraction of risk
