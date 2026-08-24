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
from .indicators import atr, ema
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
    ema9_series = ema(h4["close"], 9)
    patterns   = detect_inverse_hns(h4)  # double_bottom suspended pending investigation
    patterns.sort(key=lambda p: p["break_index"])

    last_bar = len(h4) - 1
    watching  = []
    triggered = []

    for p in patterns:
        bi = p["break_index"]
        # Freshness: break must be within the last 11 bars (~2.75 trading days on 4h)
        if bi < last_bar - 11:
            continue
        if not bias_asof(bias, p["break_time"]):
            continue
        # Minimum risk distance: stop must be at least 3% below neckline
        if (p["neckline"] - p["stop_basis"]) / p["neckline"] < 0.03:
            continue

        trade = build_trade(h4, p, atr_series, equity, risk_pct)
        if trade is None:
            continue

        neckline   = p["neckline"]
        bars_since = last_bar - bi
        last_close = float(h4.loc[last_bar, "close"])
        last_open  = float(h4.loc[last_bar, "open"])
        ema9_now   = float(ema9_series.iloc[last_bar])

        in_pullback = any(
            float(h4.loc[i, "low"]) <= neckline * 1.005
            for i in range(bi + 1, last_bar + 1)
        )

        triggered_today = (
            in_pullback
            and last_close >= neckline
            and last_close > last_open
            and last_close >= ema9_now  # momentum: price above 9 EMA (bull area)
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


def _days_between(start_ts: str, end_ts: str) -> float | None:
    """Calendar days between two ISO timestamp strings. Returns None on parse error."""
    try:
        a = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
        return round((b - a).total_seconds() / 86400, 2)
    except Exception:
        return None


def _check_exits(open_positions: list[dict], h4_bars_today: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Check open positions against today's 4-hour bars.
    Returns (still_open, closed_today).
    Conservative: stop takes priority if same bar touches both targets.

    Trailing stop ladder:
      - Default stop: original hard stop below pattern low
      - Touch 1R → stop moves to entry (breakeven)
      - Touch 2R → stop moves to 1R (lock in 1R), target becomes 3R (full win)
      - Touch 3R → full win
    """
    still_open   = []
    closed_today = []

    for pos in open_positions:
        pos    = dict(pos)
        entry  = pos["entry"]
        risk   = pos.get("risk_per_share", 0)
        stop   = pos["stop"]
        target_1r = entry + risk
        target_2r = entry + 2 * risk
        target_3r = pos.get("target_3R", entry + 3 * risk)

        # Carry forward milestone flags across days
        max_price  = pos.get("max_price", entry)
        touched_1r = pos.get("touched_1r", False)
        touched_2r = pos.get("touched_2r", False)
        closed = False

        for _, row in h4_bars_today.iterrows():
            lo = float(row["low"])
            hi = float(row["high"])
            ts = str(row["begins_at"])

            if hi > max_price:
                max_price = hi

            # Advance milestone flags in order
            if not touched_1r and hi >= target_1r:
                touched_1r = True
            if not touched_2r and hi >= target_2r:
                touched_2r = True

            # Effective stop rises with each milestone
            if touched_2r:
                effective_stop = target_2r   # stop at 2R, running to 3R
            elif touched_1r:
                effective_stop = entry        # stop at breakeven, running to 2R→3R
            else:
                effective_stop = stop         # original hard stop

            if lo <= effective_stop:
                exit_p = round(effective_stop, 4)
                r_mult = round((exit_p - entry) / risk, 3) if risk else 0.0
                if touched_2r:
                    outcome    = "win_2r"       # locked in 2R, didn't reach 3R
                    exit_reason = "2R_stop"
                elif touched_1r:
                    outcome    = "breakeven"
                    exit_reason = "breakeven_stop"
                else:
                    outcome    = "loss"
                    exit_reason = "stop"
                days_held = _days_between(pos.get("entry_time", ""), ts)
                pos.update(
                    outcome=outcome,
                    exit_price=exit_p,
                    exit_time=ts,
                    pnl_per_share=round(exit_p - entry, 4),
                    pnl_dollars=round((exit_p - entry) * pos["shares"], 2),
                    r_multiple=r_mult,
                    max_price=round(max_price, 4),
                    touched_1r=touched_1r,
                    touched_2r=touched_2r,
                    status="closed",
                    exit_reason=exit_reason,
                    days_held=days_held,
                )
                closed_today.append(pos)
                closed = True
                break
            if hi >= target_3r:
                days_held = _days_between(pos.get("entry_time", ""), ts)
                pos.update(
                    outcome="win",
                    exit_price=round(target_3r, 4),
                    exit_time=ts,
                    pnl_per_share=round(target_3r - entry, 4),
                    pnl_dollars=round((target_3r - entry) * pos["shares"], 2),
                    r_multiple=3.0,
                    max_price=round(max_price, 4),
                    touched_1r=True,
                    touched_2r=True,
                    status="closed",
                    exit_reason="3R_target",
                    days_held=days_held,
                )
                closed_today.append(pos)
                closed = True
                break

        if not closed:
            pos["max_price"]  = round(max_price, 4)
            pos["touched_1r"] = touched_1r
            pos["touched_2r"] = touched_2r
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
    last_date_ts = trading_dates[-1] + "T23:59:00Z"
    for pos in open_positions:
        sym = pos["symbol"]
        if sym in all_h4:
            df = all_h4[sym]
            last_bar_ts  = str(df.iloc[-1]["begins_at"])
            last_close   = float(df.iloc[-1]["close"])
            days_open    = _days_between(pos.get("entry_time", ""), last_bar_ts)
            pos = dict(pos)
            pos.update(
                outcome="open",
                exit_price=round(last_close, 4),
                pnl_per_share=round(last_close - pos["entry"], 4),
                pnl_dollars=round((last_close - pos["entry"]) * pos["shares"], 2),
                r_multiple=round((last_close - pos["entry"]) / pos["risk_per_share"], 3)
                    if pos["risk_per_share"] else 0.0,
                days_open=days_open,
            )
        all_closed.append(pos)

    # Summary stats — outcomes: win (3R), win_1r (stopped at 1R floor), breakeven (0R), loss (-1R)
    # outcomes: win (3R), win_2r (stopped at 2R floor), breakeven (0R), loss (-1R)
    closed_only = [t for t in all_closed if t.get("outcome") in ("win", "win_2r", "loss", "breakeven")]
    wins       = [t for t in closed_only if t["outcome"] == "win"]
    wins_2r    = [t for t in closed_only if t["outcome"] == "win_2r"]
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
        "wins_3r":          len(wins),
        "wins_2r":          len(wins_2r),
        "losses":           len(losses),
        "breakevens":       len(breakevens),
        "win_rate":         round((len(wins) + len(wins_2r)) / n, 3) if n else 0.0,
        "full_win_rate":    round(len(wins) / n, 3) if n else 0.0,
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
                "wins_3r":   sum(1 for t in wins if t.get("type") == ptype),
                "wins_2r":   sum(1 for t in wins_2r if t.get("type") == ptype),
                "losses":    sum(1 for t in losses if t.get("type") == ptype),
                "breakevens":sum(1 for t in breakevens if t.get("type") == ptype),
                "avg_R":     round(
                    sum(t["r_multiple"] for t in closed_only if t.get("type") == ptype) /
                    max(1, sum(1 for t in closed_only if t.get("type") == ptype)), 3),
                "avg_days_held": round(
                    sum(t["days_held"] for t in closed_only
                        if t.get("type") == ptype and t.get("days_held") is not None) /
                    max(1, sum(1 for t in closed_only
                        if t.get("type") == ptype and t.get("days_held") is not None)), 1),
            }
            for ptype in ["inverse_hns", "double_bottom"]
        },
        "avg_days_held": {
            "wins_3r":   round(sum(t["days_held"] for t in wins if t.get("days_held") is not None) /
                               max(1, sum(1 for t in wins if t.get("days_held") is not None)), 1),
            "wins_2r":   round(sum(t["days_held"] for t in wins_2r if t.get("days_held") is not None) /
                               max(1, sum(1 for t in wins_2r if t.get("days_held") is not None)), 1),
            "losses":    round(sum(t["days_held"] for t in losses if t.get("days_held") is not None) /
                               max(1, sum(1 for t in losses if t.get("days_held") is not None)), 1),
            "breakevens":round(sum(t["days_held"] for t in breakevens if t.get("days_held") is not None) /
                               max(1, sum(1 for t in breakevens if t.get("days_held") is not None)), 1),
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
    print(f"Closed trades:     {summary['closed_trades']}  "
          f"({summary['wins_3r']}W3R / {summary['wins_2r']}W2R / {summary['losses']}L / {summary['breakevens']}BE)")
    print(f"Win rate (any):    {summary['win_rate']:.1%}   Full 3R rate: {summary['full_win_rate']:.1%}")
    for ptype, stats in summary.get("by_pattern", {}).items():
        tot = stats["wins_3r"] + stats["wins_2r"] + stats["losses"] + stats["breakevens"]
        wr = (stats["wins_3r"] + stats["wins_2r"]) / tot if tot else 0
        print(f"  {ptype:<18} {stats['wins_3r']}W3R/{stats['wins_2r']}W2R/{stats['losses']}L/{stats['breakevens']}BE  "
              f"wr={wr:.1%}  avgR={stats['avg_R']:+.3f}  avg_days={stats['avg_days_held']:.1f}")
    adh = summary.get("avg_days_held", {})
    print(f"Avg days held:     W3R={adh.get('wins_3r','?')}d  W2R={adh.get('wins_2r','?')}d  "
          f"L={adh.get('losses','?')}d  BE={adh.get('breakevens','?')}d")
    print(f"Avg R:             {summary['avg_R']:.2f}R")
    print(f"Total R:           {summary['total_R']:.2f}R")
    print(f"Closed P&L:        ${summary['closed_pnl']:+.2f}")
    print(f"Open unrealized:   ${summary['open_unrealized']:+.2f}")
    print(f"Total P&L:         ${summary['total_pnl']:+.2f}")
    print(f"Starting equity:   ${summary['equity_start']:,.2f}")
    print(f"Ending equity:     ${summary['equity_end']:,.2f}")
    print(f"Open positions:    {summary['open_positions']}")
