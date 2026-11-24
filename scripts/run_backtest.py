#!/usr/bin/env python3
"""Walk-forward backtest using current strategy rules.

Reads cached daily + 1H bars from data/hist_cache/.
Applies:
  - Daily bias filter (price > 20 EMA, 20 EMA > 50 SMA)
  - Pattern detection (inverse HnS, double bottom, cup-and-handle)
  - Quality score filter (MIN_QUALITY_SCORE = 0.45, cap at 0.52)
  - Retest entry (Entry B only — matches how all 16 live trades actually entered)
  - Current exit rules:
      fast_loss:    exit at FAST_LOSS_R (-0.75R) if within FAST_LOSS_DAYS (3) trading days
      partial_exit: 50% shares out at 1R, stop moves to entry + 0.15R
      full_target:  remaining 50% exits at 1.5R
      time_stop:    exit at CLOSE after TIME_STOP_TRADING_DAYS (12) if 1R never touched

Outputs:
  reports/backtest_<date>.json   — full trade list
  reports/backtest_summary.json  — performance tables
  (also prints a human-readable summary to stdout)

Usage:
    python scripts/run_backtest.py                  # all cached symbols
    python scripts/run_backtest.py AAPL MSFT NVDA   # specific symbols
    python scripts/run_backtest.py --compare         # also runs old rules for comparison
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from swing_agent.backtest import daily_bias_series, bias_asof
from swing_agent.config import (
    MIN_QUALITY_SCORE, TARGET_FULL_R, PARTIAL_EXIT_R, PARTIAL_EXIT_FRAC,
    BREAKEVEN_BUFFER_R, TIME_STOP_TRADING_DAYS, FAST_LOSS_R, FAST_LOSS_DAYS,
    RISK_PCT, STARTING_EQUITY,
)
from swing_agent.dataio import bars_to_df
from swing_agent.indicators import atr, ema
from swing_agent.patterns import detect_double_bottom, detect_inverse_hns, detect_cup_and_handle
from swing_agent.simulator import build_trade, build_retest_trade

CACHE   = ROOT / "data" / "hist_cache"
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)

# Quality score cap — findings show 0.50-0.65 bucket is 0 wins / -0.56R avg
QUALITY_SCORE_CAP = 0.52

# 1H bars per trading day (6.5 trading hours)
BARS_PER_TRADING_DAY = 6.5


# ---------------------------------------------------------------------------
# Quality score (simplified version matching scanner._quality_score)
# ---------------------------------------------------------------------------

def _quality_score(pattern: dict, df: pd.DataFrame, atr_val: float) -> float:
    """Simplified quality score for backtest — same dimensions as scanner."""
    bi = pattern["break_index"]
    neckline = pattern["neckline"]
    stop_basis = pattern["stop_basis"]

    # Pattern depth (3-12% is ideal)
    depth = (neckline - stop_basis) / neckline if neckline > 0 else 0
    if depth < 0.03:
        depth_score = 0.0
    elif depth <= 0.07:
        depth_score = 1.0
    elif depth <= 0.12:
        depth_score = 0.7
    else:
        depth_score = 0.3

    # Volume on break bar vs 20-bar avg
    if "volume" in df.columns and bi >= 20:
        vol_avg = df["volume"].iloc[bi - 20:bi].mean()
        vol_ratio = df["volume"].iloc[bi] / vol_avg if vol_avg > 0 else 1.0
        if vol_ratio >= 1.5:
            vol_score = 1.0
        elif vol_ratio >= 1.2:
            vol_score = 0.7
        else:
            vol_score = 0.3
    else:
        vol_score = 0.5  # neutral when no volume data

    # ATR-relative stop distance (tighter = better R:R)
    risk = neckline - stop_basis
    atr_ratio = risk / atr_val if atr_val > 0 else 2.0
    if atr_ratio <= 1.0:
        atr_score = 1.0
    elif atr_ratio <= 2.0:
        atr_score = 0.7
    else:
        atr_score = 0.3

    return round((depth_score * 0.4 + vol_score * 0.35 + atr_score * 0.25), 4)


# ---------------------------------------------------------------------------
# Current-rules resolver (walk bar-by-bar)
# ---------------------------------------------------------------------------

def resolve_current(df: pd.DataFrame, trade: dict) -> dict:
    """Walk forward from entry applying current exit rules."""
    entry      = trade["entry"]
    stop       = trade["stop"]
    risk       = trade["risk_per_share"]
    start      = trade["entry_index"] + 1
    target_1r  = entry + PARTIAL_EXIT_R * risk
    target_full = entry + TARGET_FULL_R * risk
    be_stop    = entry + BREAKEVEN_BUFFER_R * risk

    # State
    touched_1r        = False
    partial_exit_done = False
    partial_exit_price = None

    outcome    = "open"
    exit_price = float(df.loc[len(df) - 1, "close"])
    exit_time  = str(df.loc[len(df) - 1, "begins_at"])
    exit_reason = "open"
    bars_held  = 0

    for i in range(start, len(df)):
        bars_held = i - start
        trading_days = bars_held / BARS_PER_TRADING_DAY

        hi = float(df.loc[i, "high"])
        lo = float(df.loc[i, "low"])
        cl = float(df.loc[i, "close"])

        # Mark 1R touch using intraday high
        if not touched_1r and hi >= target_1r:
            touched_1r = True

        # Partial exit fires when 1R is touched (use target_1r as fill price)
        if touched_1r and not partial_exit_done:
            partial_exit_done  = True
            partial_exit_price = target_1r

        eff_stop = be_stop if partial_exit_done else stop

        # Fast-loss: only before 1R, within FAST_LOSS_DAYS trading days
        if (FAST_LOSS_R < 0 and not touched_1r and
                trading_days <= FAST_LOSS_DAYS):
            current_r = (cl - entry) / risk if risk > 0 else 0
            if current_r <= FAST_LOSS_R:
                outcome    = "fast_loss"
                exit_price = cl
                exit_time  = str(df.loc[i, "begins_at"])
                exit_reason = "fast_loss"
                break

        # Stop hit (use intraday low)
        if lo <= eff_stop:
            outcome    = "stopped"
            exit_price = eff_stop
            exit_time  = str(df.loc[i, "begins_at"])
            exit_reason = "be_stop" if partial_exit_done else "stop"
            break

        # Full target (use intraday high)
        if hi >= target_full:
            outcome    = "target_hit"
            exit_price = target_full
            exit_time  = str(df.loc[i, "begins_at"])
            exit_reason = "1.5R"
            break

        # Time stop: 12 trading days without touching 1R
        if not touched_1r and trading_days >= TIME_STOP_TRADING_DAYS:
            outcome    = "time_stop"
            exit_price = cl
            exit_time  = str(df.loc[i, "begins_at"])
            exit_reason = "time_stop"
            break

    # Compute R multiple properly for partial exit
    if partial_exit_done and outcome not in ("fast_loss", "open"):
        # 50% exited at 1R (+1R), remaining 50% at exit_price
        partial_r  = (target_1r - entry) / risk * PARTIAL_EXIT_FRAC
        remaining_r = ((exit_price - entry) / risk) * (1 - PARTIAL_EXIT_FRAC)
        r_multiple  = round(partial_r + remaining_r, 3)
    elif outcome == "fast_loss":
        r_multiple = round((exit_price - entry) / risk, 3) if risk > 0 else 0
    else:
        r_multiple = round((exit_price - entry) / risk, 3) if risk > 0 else 0

    resolved = dict(trade)
    resolved.update({
        "outcome":       outcome,
        "exit_time":     exit_time,
        "exit_price":    round(exit_price, 4),
        "exit_reason":   exit_reason,
        "r_multiple":    r_multiple,
        "partial_exit":  partial_exit_done,
        "bars_held":     bars_held,
        "trading_days":  round(bars_held / BARS_PER_TRADING_DAY, 1),
    })
    return resolved


# ---------------------------------------------------------------------------
# Per-symbol scan
# ---------------------------------------------------------------------------

def scan_symbol(symbol: str, equity: float) -> list[dict]:
    day_path = CACHE / f"{symbol}_day.json"
    h1_path  = CACHE / f"{symbol}_hour.json"
    if not day_path.exists():
        print(f"  [skip] {symbol}: no daily cache")
        return []
    if not h1_path.exists():
        print(f"  [skip] {symbol}: no 1H cache")
        return []

    with open(day_path) as f:
        daily = bars_to_df(json.load(f), symbol)
    with open(h1_path) as f:
        h1 = bars_to_df(json.load(f), symbol)

    if len(daily) < 60 or len(h1) < 100:
        print(f"  [skip] {symbol}: thin data ({len(daily)}d / {len(h1)}h bars)")
        return []

    daily_bias = daily_bias_series(daily)
    atr_series = atr(h1, 14)
    patterns   = (detect_double_bottom(h1) +
                  detect_inverse_hns(h1) +
                  detect_cup_and_handle(h1))
    patterns.sort(key=lambda p: p["break_index"])

    trades: list[dict] = []
    seen_until: dict[str, int] = {}

    for p in patterns:
        ptype = p["type"]
        bi    = p["break_index"]

        # No overlapping same-type setups
        if bi <= seen_until.get(ptype, -1):
            continue

        # Daily bias must be long at breakout
        if not bias_asof(daily_bias, p["break_time"]):
            continue

        # Pattern depth filter
        depth = (p["neckline"] - p["stop_basis"]) / p["neckline"]
        if depth < 0.03 or depth > 0.12:
            continue

        # Quality score filter
        atr_val = float(atr_series.iloc[bi]) if bi < len(atr_series) else 0
        qs = _quality_score(p, h1, atr_val)
        if qs < MIN_QUALITY_SCORE or qs > QUALITY_SCORE_CAP:
            continue

        # Build Entry A (breakout: close of neckline break bar)
        trade_a = build_trade(h1, p, atr_series, equity, RISK_PCT)
        if trade_a is None:
            continue
        trade_a["symbol"]        = symbol
        trade_a["timeframe"]     = "1h"
        trade_a["quality_score"] = qs
        trade_a["pattern_depth"] = round(depth, 4)
        trade_a["entry_style"]   = "breakout"

        # Build Entry B (retest: pullback to neckline + bullish close)
        trade_b = build_retest_trade(h1, trade_a, atr_series)
        if trade_b is not None:
            trade_b["symbol"]        = symbol
            trade_b["timeframe"]     = "1h"
            trade_b["quality_score"] = qs
            trade_b["pattern_depth"] = round(depth, 4)
            trade_b["entry_style"]   = "retest"

        # Resolve both; track seen_until from Entry A (earlier exit = safe baseline)
        resolved_a = resolve_current(h1, trade_a)
        trades.append(resolved_a)

        if trade_b is not None:
            resolved_b = resolve_current(h1, trade_b)
            trades.append(resolved_b)

        # Advance the overlap cursor based on Entry A exit (the earlier one)
        ref = resolved_a
        if ref["outcome"] != "open":
            idx_list = h1.index[h1["begins_at"] == ref["exit_time"]].tolist()
            seen_until[ptype] = idx_list[0] if idx_list else bi + 1
        else:
            seen_until[ptype] = len(h1)

    n = len(trades)
    if n:
        wins = sum(1 for t in trades if t["r_multiple"] > 0)
        avg_r = sum(t["r_multiple"] for t in trades) / n
        print(f"  {symbol:8}: {n:3} trades  {wins}/{n} wins  avg {avg_r:+.2f}R")
    return trades


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------

def _summarize(trades: list[dict], label: str) -> dict:
    if not trades:
        return {"label": label, "n": 0}
    closed = [t for t in trades if t["outcome"] != "open"]
    n   = len(closed)
    wins = sum(1 for t in closed if t["r_multiple"] > 0)
    avg_r = sum(t["r_multiple"] for t in closed) / n if n else 0
    win_rate = wins / n if n else 0
    by_reason: dict[str, int] = {}
    for t in closed:
        r = t.get("exit_reason", "?")
        by_reason[r] = by_reason.get(r, 0) + 1
    return {
        "label":    label,
        "n":        n,
        "wins":     wins,
        "losses":   n - wins,
        "win_rate": round(win_rate, 3),
        "avg_R":    round(avg_r, 3),
        "exit_breakdown": by_reason,
        "open": len(trades) - n,
    }


def _print_summary(s: dict) -> None:
    if s["n"] == 0:
        print(f"  {s['label']}: no closed trades")
        return
    print(f"  {s['label']:25}  n={s['n']:3}  wins={s['wins']}/{s['n']}  "
          f"win%={s['win_rate']*100:.0f}%  avg R={s['avg_R']:+.3f}R")
    for reason, cnt in sorted(s["exit_breakdown"].items(), key=lambda x: -x[1]):
        print(f"    exit/{reason:20}: {cnt:3}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(symbols: list[str]) -> dict:
    equity = STARTING_EQUITY
    all_trades: list[dict] = []

    print(f"\nBacktest — {len(symbols)} symbols, equity=${equity:,.0f}, "
          f"risk={RISK_PCT*100:.0f}%/trade")
    print(f"Rules: fast_loss={FAST_LOSS_R}R/{FAST_LOSS_DAYS}d  "
          f"partial@{PARTIAL_EXIT_R}R  full@{TARGET_FULL_R}R  "
          f"time_stop={TIME_STOP_TRADING_DAYS}d  "
          f"QS=[{MIN_QUALITY_SCORE},{QUALITY_SCORE_CAP}]\n")

    for sym in symbols:
        all_trades.extend(scan_symbol(sym, equity))

    all_trades.sort(key=lambda t: t.get("entry_time", ""))

    # Separate by entry style
    entry_a = [t for t in all_trades if t.get("entry_style") == "breakout"]
    entry_b = [t for t in all_trades if t.get("entry_style") == "retest"]
    # Paired: same pattern, both entries present
    paired_patterns = len(entry_a)  # one entry_a per pattern

    overall_a = _summarize(entry_a, "Entry A (breakout)")
    overall_b = _summarize(entry_b, "Entry B (retest)")
    overall   = _summarize(all_trades, "Overall (all)")

    # By pattern × entry style
    by_pattern_entry: dict = {}
    for pt in ("inverse_hns", "double_bottom", "cup_and_handle"):
        by_pattern_entry[pt] = {
            "A": _summarize([t for t in entry_a if t.get("type") == pt], f"{pt}/A"),
            "B": _summarize([t for t in entry_b if t.get("type") == pt], f"{pt}/B"),
        }

    # Fast-loss
    with_fl = [t for t in all_trades if t.get("exit_reason") == "fast_loss"]

    report = {
        "run_time":        datetime.now(timezone.utc).isoformat(),
        "symbols":         symbols,
        "paired_patterns": paired_patterns,
        "entry_a":         overall_a,
        "entry_b":         overall_b,
        "overall":         overall,
        "by_pattern_entry": by_pattern_entry,
        "fast_loss_trades": len(with_fl),
        "fast_loss_avg_r": round(
            sum(t["r_multiple"] for t in with_fl) / len(with_fl), 3
        ) if with_fl else None,
    }

    # Save
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = REPORTS / f"backtest_{today}.json"
    with open(out, "w") as f:
        json.dump({"report": report, "trades": all_trades}, f, indent=2)
    (REPORTS / "backtest_latest.json").unlink(missing_ok=True)
    (REPORTS / "backtest_latest.json").symlink_to(out.name)

    # Print
    print(f"\n=== RESULTS ({paired_patterns} patterns, each tested A + B) ===")
    _print_summary(overall_a)
    _print_summary(overall_b)
    print()
    _print_summary(overall)
    print("\nBy pattern:")
    for pt, d in by_pattern_entry.items():
        for s in d.values():
            if s["n"] > 0:
                _print_summary(s)
    if with_fl:
        avg = report["fast_loss_avg_r"]
        print(f"\nFast-loss exits: {len(with_fl)} trades @ avg {avg:+.3f}R")
    print(f"\nFull results: {out}")

    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbols", nargs="*", help="symbols to backtest (default: all cached)")
    args = ap.parse_args()

    if args.symbols:
        syms = [s.upper() for s in args.symbols]
    else:
        # All symbols that have both day + hour cache files
        day_syms  = {p.stem.split("_")[0] for p in CACHE.glob("*_day.json")}
        hour_syms = {p.stem.split("_")[0] for p in CACHE.glob("*_hour.json")}
        syms = sorted(day_syms & hour_syms)
        if not syms:
            print("No cached data found. Run the data fetch first.")
            print("Ask Claude to fetch history via fetch_history.py.")
            sys.exit(1)

    run(syms)


if __name__ == "__main__":
    main()
