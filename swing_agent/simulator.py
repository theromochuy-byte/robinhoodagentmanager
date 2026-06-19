"""Paper-trading simulator.

Takes confirmed patterns plus the entry-timeframe bars and produces resolved
paper trades. NO live orders are ever placed; this is pure simulation over
historical bars so we can collect P/L on proposed entries and exits.
"""
from __future__ import annotations

import pandas as pd

from .indicators import atr


def _prior_structure_high(df: pd.DataFrame, before_index: int, neckline: float) -> float | None:
    """Highest high to the left of the breakout that sits above the neckline,
    used as the primary (structure) target.
    """
    window = df.loc[: before_index - 1]
    above = window[window["high"] > neckline]
    if len(above) == 0:
        return None
    return float(above["high"].max())


def build_trade(
    df: pd.DataFrame,
    pattern: dict,
    atr_series: pd.Series,
    equity: float,
    risk_pct: float = 0.01,
    atr_mult: float = 1.0,
) -> dict | None:
    """Construct a proposed long trade from a confirmed pattern.

    Entry = close of the breakout bar (Entry A / breakout style).
    Stop  = stop_basis low minus atr_mult * ATR at the breakout bar.
    Targets: primary = prior structure high; plus 1R and 2R reference levels.
    """
    bi = pattern["break_index"]
    entry = float(df.loc[bi, "close"])
    a = float(atr_series.iloc[bi])
    stop = pattern["stop_basis"] - atr_mult * a
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return None

    r1 = entry + risk_per_share
    r2 = entry + 2 * risk_per_share
    struct = _prior_structure_high(df, bi, pattern["neckline"])
    primary_target = struct if (struct and struct > entry) else r2

    shares = (equity * risk_pct) / risk_per_share

    return {
        "type": pattern["type"],
        "entry_time": str(df.loc[bi, "begins_at"]),
        "entry_index": bi,
        "entry": round(entry, 4),
        "stop": round(stop, 4),
        "neckline": round(pattern["neckline"], 4),
        "risk_per_share": round(risk_per_share, 4),
        "target_primary": round(primary_target, 4),
        "target_1R": round(r1, 4),
        "target_2R": round(r2, 4),
        "shares": round(shares, 4),
        "atr": round(a, 4),
    }


def build_retest_trade(df: pd.DataFrame, trade: dict, atr_series: pd.Series) -> dict | None:
    """Entry B: wait for a pullback to the neckline after the breakout, then enter
    on the first bullish close at or above the neckline (close > open and close >= neckline).
    Stop and targets are recalculated from the new entry price; risk-per-share stays
    the same R-distance, so share size is recalculated too.
    """
    neckline = trade["neckline"]
    start = trade["entry_index"] + 1
    shares_equity_risk = trade["risk_per_share"] * trade["shares"]  # 1% of equity

    in_pullback = False
    for i in range(start, len(df)):
        lo = float(df.loc[i, "low"])
        hi = float(df.loc[i, "high"])
        c = float(df.loc[i, "close"])
        o = float(df.loc[i, "open"])

        # pulled back to neckline zone
        if lo <= neckline * 1.005:
            in_pullback = True

        if in_pullback and c >= neckline and c > o:
            # bullish reaction bar at the neckline — entry
            entry_b = c
            a = float(atr_series.iloc[i])
            stop_b = trade["stop"]  # same structural stop
            risk_b = entry_b - stop_b
            if risk_b <= 0:
                return None
            shares_b = shares_equity_risk / risk_b
            r1_b = entry_b + risk_b
            r2_b = entry_b + 2 * risk_b
            t = dict(trade)
            t.update(
                {
                    "entry_style": "retest",
                    "entry_time": str(df.loc[i, "begins_at"]),
                    "entry_index": i,
                    "entry": round(entry_b, 4),
                    "risk_per_share": round(risk_b, 4),
                    "target_primary": round(r2_b, 4),  # use 2R for retest entry too
                    "target_1R": round(r1_b, 4),
                    "target_2R": round(r2_b, 4),
                    "shares": round(shares_b, 4),
                    "atr": round(a, 4),
                }
            )
            return t

        # price ran away without retesting — no Entry B
        if hi > neckline * 1.03 and not in_pullback:
            return None

    return None


def resolve_trade(df: pd.DataFrame, trade: dict, target_key: str = "target_primary") -> dict:
    """Walk bars forward from entry and resolve to the chosen target or the stop.

    Conservative rule: if a single bar touches both stop and target, the stop
    is assumed hit first. Returns the trade enriched with outcome fields.
    """
    entry = trade["entry"]
    stop = trade["stop"]
    target = trade[target_key]
    start = trade["entry_index"] + 1

    outcome = "open"
    exit_price = None
    exit_time = None
    for i in range(start, len(df)):
        lo = float(df.loc[i, "low"])
        hi = float(df.loc[i, "high"])
        if lo <= stop:
            outcome, exit_price, exit_time = "loss", stop, str(df.loc[i, "begins_at"])
            break
        if hi >= target:
            outcome, exit_price, exit_time = "win", target, str(df.loc[i, "begins_at"])
            break

    if outcome == "open":
        # mark to last close, unrealized
        exit_price = float(df.loc[len(df) - 1, "close"])
        exit_time = str(df.loc[len(df) - 1, "begins_at"])

    pnl_per_share = exit_price - entry
    r_multiple = pnl_per_share / trade["risk_per_share"] if trade["risk_per_share"] else 0.0
    pnl_dollars = pnl_per_share * trade["shares"]

    resolved = dict(trade)
    resolved.update(
        {
            "outcome": outcome,
            "exit_time": exit_time,
            "exit_price": round(exit_price, 4),
            "pnl_per_share": round(pnl_per_share, 4),
            "r_multiple": round(r_multiple, 3),
            "pnl_dollars": round(pnl_dollars, 2),
            "target_used": target_key,
        }
    )
    return resolved


def compound_ledger(trades: list[dict], starting_equity: float, risk_pct: float = 0.01) -> list[dict]:
    """Recompute share sizes and dollar P&L with compounding equity.

    Trades are processed in entry_time order. Equity is updated after each
    trade's exit_time; concurrent open positions each draw 1% of the equity
    that was available at their own entry time. R-multiples and outcomes are
    unchanged — only shares and pnl_dollars are recomputed.
    """
    equity = starting_equity
    # build a map of exit_time -> equity delta so we can apply gains/losses
    # in chronological order across all symbols
    sorted_trades = sorted(trades, key=lambda t: t["entry_time"])
    exit_events: list[tuple[str, int, float]] = []  # (exit_time, idx, pnl)

    result = []
    for idx, t in enumerate(sorted_trades):
        shares = (equity * risk_pct) / t["risk_per_share"] if t["risk_per_share"] else 0.0
        pnl = round(t["pnl_per_share"] * shares, 2)
        updated = dict(t)
        updated["shares"] = round(shares, 4)
        updated["pnl_dollars"] = pnl
        updated["equity_at_entry"] = round(equity, 2)
        result.append(updated)
        if t["outcome"] != "open":
            exit_events.append((t["exit_time"], idx, pnl))
        # advance equity for trades that have already closed before this entry
        # (apply all exits up to but not including this entry_time)
        pending = [(et, i, p) for et, i, p in exit_events if et <= t["entry_time"] and i < idx]
        for et, i, p in pending:
            equity += p
            exit_events = [(et2, i2, p2) for et2, i2, p2 in exit_events if i2 != i]

    return result


def summarize(trades: list[dict]) -> dict:
    """Aggregate performance stats over a list of resolved trades."""
    closed = [t for t in trades if t["outcome"] in ("win", "loss")]
    wins = [t for t in closed if t["outcome"] == "win"]
    losses = [t for t in closed if t["outcome"] == "loss"]
    total_r = sum(t["r_multiple"] for t in closed)
    total_pnl = sum(t["pnl_dollars"] for t in closed)
    n = len(closed)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n, 3) if n else 0.0,
        "avg_R": round(total_r / n, 3) if n else 0.0,
        "expectancy_R": round(total_r / n, 3) if n else 0.0,
        "total_R": round(total_r, 2),
        "total_pnl_dollars": round(total_pnl, 2),
        "open_trades": len([t for t in trades if t["outcome"] == "open"]),
    }
