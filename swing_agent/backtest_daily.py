"""Day-by-day paper backtest: replays the forward scanner over historical data.

For each trading day in the 4-hour data, slices all bars to that date,
runs the scanner logic, checks open positions for stop/target hits, and
accumulates a full P&L ledger.

Usage:
  python3 -m swing_agent.backtest_daily
  python3 -m swing_agent.backtest_daily --from 2026-04-01
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .backtest import bias_asof, daily_bias_series
from .dataio import load
from .indicators import atr
from .patterns import detect_double_bottom, detect_inverse_hns
from .simulator import build_trade

ROOT    = Path(__file__).resolve().parent.parent
DATA    = ROOT / "data"
REPORTS = ROOT / "reports"
BACKTEST_LEDGER = DATA / "paper_backtest.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trading_dates(symbol: str = "AAPL") -> list[str]:
    h4 = load(DATA / f"{symbol}_4hour.json", symbol)
    dates = sorted(set(str(ts)[:10] for ts in h4["begins_at"]))
    return dates


def _slice_to_date(df: pd.DataFrame, date_str: str) -> pd.DataFrame:
    """Return rows whose begins_at date <= date_str."""
    mask = pd.to_datetime(df["begins_at"]).dt.strftime("%Y-%m-%d") <= date_str
    return df[mask].reset_index(drop=True)


def _scan_symbol_asof(
    symbol: str,
    daily_full: pd.DataFrame,
    h4_full: pd.DataFrame,
    date_str: str,
    equity: float,
    risk_pct: float,
) -> dict:
    """Same logic as scanner.scan_symbol but operates on data sliced to date_str."""
    daily = _slice_to_date(daily_full, date_str)
    h4    = _slice_to_date(h4_full, date_str)
    if len(daily) < 30 or len(h4) < 30:
        return {"watching": [], "triggered": []}

    bias       = daily_bias_series(daily)
    atr_series = atr(h4, 14)
    patterns   = detect_double_bottom(h4) + detect_inverse_hns(h4)
    patterns.sort(key=lambda p: p["break_index"])

    last_bar = len(h4) - 1
    watching  = []
    triggered = []

    for p in patterns:
        bi = p["break_index"]
        if bi < last_bar - 11:
            continue
        if not bias_asof(bias, p["break_time"]):
            continue
        if (p["neckline"] - p["stop_basis"]) / p["neckline"] < 0.03:
            continue

        trade = build_trade(h4, p, atr_series, equity, risk_pct)
        if trade is None:
            continue

        neckline   = p["neckline"]
        bars_since = last_bar - bi
        last_close = float(h4.loc[last_bar, "close"])
        last_low   = float(h4.loc[last_bar, "low"])
        last_open  = float(h4.loc[last_bar, "open"])

        in_pullback = any(
            float(h4.loc[i, "low"]) <= neckline * 1.005
            for i in range(bi + 1, last_bar + 1)
        )

        triggered_today = (
            in_pullback
            and last_close >= neckline
            and last_close > last_open
        )

        setup = {
            "symbol":          symbol,
            "type":            p["type"],
            "neckline":        round(neckline, 4),
            "stop":            round(trade["stop"], 4),
            "target_2R":       round(trade["target_2R"], 4),
            "risk_per_share":  round(trade["risk_per_share"], 4),
            "shares":          round(trade["shares"], 4),
            "break_time":      str(p["break_time"]),
            "bars_since_break": bars_since,
            "in_pullback":     in_pullback,
            "last_close":      round(last_close, 4),
            "last_bar_time":   str(h4.loc[last_bar, "begins_at"]),
            "scan_date":       date_str,
        }

        if triggered_today:
            setup["entry"]      = round(last_close, 4)
            setup["entry_time"] = str(h4.loc[last_bar, "begins_at"])
            setup["status"]     = "open"
            triggered.append(setup)
        else:
            setup["status"] = "watching"
            watching.append(setup)

    return {"watching": watching, "triggered": triggered}


def _check_exits(open_positions: list[dict], h4_bars_today: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Check open positions against today's 4-hour bars.
    Returns (still_open, closed_today).
    Conservative: stop takes priority if same bar touches both.

    Tracks max_price and touched_1r per position for exit-signal analysis.
    Implements breakeven stop: once price touches 1R gain, stop moves to entry.
    """
    still_open   = []
    closed_today = []

    for pos in open_positions:
        pos    = dict(pos)
        entry  = pos["entry"]
        risk   = pos.get("risk_per_share", 0)
        stop   = pos["stop"]
        target = pos["target_2R"]
        target_1r = entry + risk  # 1R checkpoint

        # Carry forward max_price and touched_1r across days
        max_price  = pos.get("max_price", entry)
        touched_1r = pos.get("touched_1r", False)
        closed = False

        for _, row in h4_bars_today.iterrows():
            lo = float(row["low"])
            hi = float(row["high"])
            ts = str(row["begins_at"])

            # Update max price seen
            if hi > max_price:
                max_price = hi

            # Check if 1R has been touched (for breakeven stop tracking)
            if not touched_1r and hi >= target_1r:
                touched_1r = True

            # Effective stop: if 1R touched, move stop to breakeven (entry)
            effective_stop = entry if touched_1r else stop

            if lo <= effective_stop:
                outcome = "loss" if effective_stop < entry else "breakeven"
                exit_p  = round(effective_stop, 4)
                r_mult  = round((exit_p - entry) / risk, 3) if risk else 0.0
                pos.update(
                    outcome=outcome,
                    exit_price=exit_p,
                    exit_time=ts,
                    pnl_per_share=round(exit_p - entry, 4),
                    pnl_dollars=round((exit_p - entry) * pos["shares"], 2),
                    r_multiple=r_mult,
                    max_price=round(max_price, 4),
                    touched_1r=touched_1r,
                    status="closed",
                    exit_reason="stop" if effective_stop == stop else "breakeven_stop",
                )
                closed_today.append(pos)
                closed = True
                break
            if hi >= target:
                pos.update(
                    outcome="win",
                    exit_price=round(target, 4),
                    exit_time=ts,
                    pnl_per_share=round(target - entry, 4),
                    pnl_dollars=round((target - entry) * pos["shares"], 2),
                    r_multiple=2.0,
                    max_price=round(max_price, 4),
                    touched_1r=True,
                    status="closed",
                    exit_reason="2R_target",
                )
                closed_today.append(pos)
                closed = True
                break

        if not closed:
            pos["max_price"]  = round(max_price, 4)
            pos["touched_1r"] = touched_1r
            still_open.append(pos)

    return still_open, closed_today


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_backtest(
    symbols: list[str],
    equity: float = 1500.0,
    risk_pct: float = 0.02,
    from_date: str | None = None,
) -> dict:
    REPORTS.mkdir(exist_ok=True)
    backtest_dir = REPORTS / "backtest"
    backtest_dir.mkdir(exist_ok=True)

    # Load all symbol data once
    print("Loading data...", flush=True)
    all_daily: dict[str, pd.DataFrame] = {}
    all_h4:    dict[str, pd.DataFrame] = {}
    for sym in symbols:
        dp = DATA / f"{sym}_day.json"
        h4p = DATA / f"{sym}_4hour.json"
        if dp.exists() and h4p.exists():
            all_daily[sym] = load(dp, sym)
            all_h4[sym]    = load(h4p, sym)

    trading_dates = _trading_dates()
    if from_date:
        trading_dates = [d for d in trading_dates if d >= from_date]

    print(f"Replaying {len(trading_dates)} trading days: {trading_dates[0]} → {trading_dates[-1]}")

    open_positions: list[dict] = []
    all_closed:     list[dict] = []
    seen_entries: set[tuple] = set()  # (symbol, type, entry_time) dedup

    daily_reports = []

    for date_str in trading_dates:
        # 1. Check exits for all open positions on today's bars
        todays_bars: dict[str, pd.DataFrame] = {}
        for sym in {p["symbol"] for p in open_positions}:
            if sym in all_h4:
                df = all_h4[sym]
                mask = pd.to_datetime(df["begins_at"]).dt.strftime("%Y-%m-%d") == date_str
                todays_bars[sym] = df[mask].reset_index(drop=True)

        by_symbol: dict[str, list] = {}
        for pos in open_positions:
            by_symbol.setdefault(pos["symbol"], []).append(pos)

        open_positions = []
        closed_today   = []
        for sym, positions in by_symbol.items():
            bars = todays_bars.get(sym, pd.DataFrame())
            if bars.empty:
                open_positions.extend(positions)
            else:
                still, closed = _check_exits(positions, bars)
                open_positions.extend(still)
                closed_today.extend(closed)
        all_closed.extend(closed_today)

        # 2. Scan for new setups
        new_entries   = []
        new_watching  = []
        for sym in symbols:
            if sym not in all_daily or sym not in all_h4:
                continue
            result = _scan_symbol_asof(sym, all_daily[sym], all_h4[sym], date_str, equity, risk_pct)
            for t in result["triggered"]:
                key = (t["symbol"], t["type"], t.get("entry_time"))
                if key not in seen_entries:
                    seen_entries.add(key)
                    new_entries.append(t)
                    open_positions.append(t)
            new_watching.extend(result["watching"])

        # 3. Day report
        rpt = {
            "date":          date_str,
            "new_entries":   len(new_entries),
            "closed_today":  len(closed_today),
            "open_count":    len(open_positions),
            "entries":       new_entries,
            "closed":        closed_today,
            "watching_count": len(new_watching),
        }
        daily_reports.append(rpt)
        (backtest_dir / f"{date_str}.json").write_text(json.dumps(rpt, indent=2))

        wins   = sum(1 for t in closed_today if t.get("outcome") == "win")
        losses = sum(1 for t in closed_today if t.get("outcome") == "loss")
        print(f"  {date_str}  +{len(new_entries):2d} entries  "
              f"{len(closed_today):2d} closed ({wins}W/{losses}L)  "
              f"{len(open_positions):3d} open", flush=True)

    # Mark remaining open positions as open (MTM at last close)
    for pos in open_positions:
        sym = pos["symbol"]
        if sym in all_h4:
            df = all_h4[sym]
            last_close = float(df.iloc[-1]["close"])
            pos = dict(pos)
            pos.update(
                outcome="open",
                exit_price=round(last_close, 4),
                pnl_per_share=round(last_close - pos["entry"], 4),
                pnl_dollars=round((last_close - pos["entry"]) * pos["shares"], 2),
                r_multiple=round((last_close - pos["entry"]) / pos["risk_per_share"], 3)
                    if pos["risk_per_share"] else 0.0,
            )
        all_closed.append(pos)

    # Summary stats — include breakeven as a distinct outcome
    closed_only = [t for t in all_closed if t.get("outcome") in ("win", "loss", "breakeven")]
    wins       = [t for t in closed_only if t["outcome"] == "win"]
    losses     = [t for t in closed_only if t["outcome"] == "loss"]
    breakevens = [t for t in closed_only if t["outcome"] == "breakeven"]
    n = len(closed_only)
    total_pnl    = sum(t["pnl_dollars"] for t in all_closed)
    closed_pnl   = sum(t["pnl_dollars"] for t in closed_only)
    open_unrealized = sum(t["pnl_dollars"] for t in all_closed if t.get("outcome") == "open")

    summary = {
        "from_date":        trading_dates[0],
        "to_date":          trading_dates[-1],
        "symbols_scanned":  len(symbols),
        "total_entries":    len(all_closed),
        "closed_trades":    n,
        "wins":             len(wins),
        "losses":           len(losses),
        "breakevens":       len(breakevens),
        "win_rate":         round(len(wins) / n, 3) if n else 0.0,
        "avg_R":            round(sum(t["r_multiple"] for t in closed_only) / n, 3) if n else 0.0,
        "total_R":          round(sum(t["r_multiple"] for t in closed_only), 2),
        "closed_pnl":       round(closed_pnl, 2),
        "open_unrealized":  round(open_unrealized, 2),
        "total_pnl":        round(total_pnl, 2),
        "open_positions":   len([t for t in all_closed if t.get("outcome") == "open"]),
        "equity_start":     equity,
        "equity_end":       round(equity + closed_pnl, 2),
        # By pattern
        "by_pattern": {
            ptype: {
                "wins":      sum(1 for t in wins if t.get("type") == ptype),
                "losses":    sum(1 for t in losses if t.get("type") == ptype),
                "breakevens":sum(1 for t in breakevens if t.get("type") == ptype),
                "avg_R":     round(
                    sum(t["r_multiple"] for t in closed_only if t.get("type") == ptype) /
                    max(1, sum(1 for t in closed_only if t.get("type") == ptype)), 3),
            }
            for ptype in ["inverse_hns", "double_bottom"]
        },
    }

    BACKTEST_LEDGER.write_text(json.dumps(all_closed, indent=2))
    (REPORTS / "backtest_summary.json").write_text(json.dumps(summary, indent=2))

    return summary


if __name__ == "__main__":
    args = sys.argv[1:]
    from_date = None
    if "--from" in args:
        from_date = args[args.index("--from") + 1]

    symbols = (ROOT / "data" / "universe.txt").read_text().split()
    summary = run_backtest(symbols, from_date=from_date)

    print(f"\n{'='*60}")
    print(f"BACKTEST SUMMARY  {summary['from_date']} → {summary['to_date']}")
    print(f"{'='*60}")
    print(f"Symbols scanned:   {summary['symbols_scanned']}")
    print(f"Total entries:     {summary['total_entries']}")
    print(f"Closed trades:     {summary['closed_trades']}  ({summary['wins']}W / {summary['losses']}L / {summary['breakevens']}BE)")
    print(f"Win rate:          {summary['win_rate']:.1%}")
    for ptype, stats in summary.get("by_pattern", {}).items():
        tot = stats["wins"] + stats["losses"] + stats["breakevens"]
        wr = stats["wins"] / tot if tot else 0
        print(f"  {ptype:<18} {stats['wins']}W/{stats['losses']}L/{stats['breakevens']}BE  wr={wr:.1%}  avgR={stats['avg_R']:+.3f}")
    print(f"Avg R:             {summary['avg_R']:.2f}R")
    print(f"Total R:           {summary['total_R']:.2f}R")
    print(f"Closed P&L:        ${summary['closed_pnl']:+.2f}")
    print(f"Open unrealized:   ${summary['open_unrealized']:+.2f}")
    print(f"Total P&L:         ${summary['total_pnl']:+.2f}")
    print(f"Starting equity:   ${summary['equity_start']:,.2f}")
    print(f"Ending equity:     ${summary['equity_end']:,.2f}")
    print(f"Open positions:    {summary['open_positions']}")
