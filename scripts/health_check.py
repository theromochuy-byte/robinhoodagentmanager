#!/usr/bin/env python3
"""System health check for the paper trading pipeline.

Run manually any time, or at the start of every weekly check-in:
    python scripts/health_check.py

Prints a pass/warn/fail summary and exits non-zero if any FAIL is found.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DATA = ROOT / "data"
LEDGER = DATA / "paper_trades_live.json"

WARN = "WARN"
FAIL = "FAIL"
OK   = " OK "

findings: list[tuple[str, str, str]] = []  # (level, section, message)


def check(level: str, section: str, msg: str) -> None:
    findings.append((level, section, msg))


# ── 1. Pull latest ledger from remote ────────────────────────────────────────

def sync_ledger() -> None:
    print("Syncing ledger from remote branch...")
    branch = "claude/wonderful-cerf-sa7c0d"
    r = subprocess.run(
        ["git", "fetch", "origin", branch],
        capture_output=True, text=True, cwd=ROOT
    )
    if r.returncode != 0:
        check(WARN, "Sync", f"git fetch failed: {r.stderr.strip()}")
        return
    r = subprocess.run(
        ["git", "checkout", f"origin/{branch}", "--", "data/paper_trades_live.json"],
        capture_output=True, text=True, cwd=ROOT
    )
    if r.returncode != 0:
        check(WARN, "Sync", f"git checkout failed: {r.stderr.strip()}")
    else:
        check(OK, "Sync", "Ledger pulled from remote")


# ── 2. Ledger age ─────────────────────────────────────────────────────────────

def check_ledger_age(now: datetime) -> None:
    if not LEDGER.exists():
        check(FAIL, "Ledger", "paper_trades_live.json not found")
        return
    age_h = (now.timestamp() - LEDGER.stat().st_mtime) / 3600
    if age_h > 13:
        check(FAIL, "Ledger", f"Last modified {age_h:.1f}h ago — CI may not be committing")
    else:
        check(OK, "Ledger", f"Last modified {age_h:.1f}h ago")


# ── 3. Open position freshness ────────────────────────────────────────────────

def check_position_freshness(trades: list, now: datetime) -> None:
    open_t = [t for t in trades if t.get("status") == "entered"]
    if not open_t:
        check(OK, "Freshness", "No open positions")
        return
    for t in open_t:
        sym = t["symbol"]
        checked = t.get("checked_at", "")
        if not checked:
            check(FAIL, "Freshness", f"{sym}: no checked_at field")
            continue
        dt = datetime.fromisoformat(checked.replace("Z", "+00:00"))
        age_h = (now - dt).total_seconds() / 3600
        if age_h > 13:
            check(FAIL, "Freshness", f"{sym}: price last updated {age_h:.1f}h ago (>13h)")
        else:
            check(OK, "Freshness", f"{sym}: checked {age_h:.1f}h ago")


# ── 4. Stop ladder sanity ─────────────────────────────────────────────────────

def check_stop_ladder(trades: list) -> None:
    for t in [t for t in trades if t.get("status") == "entered"]:
        sym = t["symbol"]
        entry = t.get("entry", 0)
        stop  = t.get("stop", 0)
        t1r   = t.get("target_1R", 0)
        price = t.get("last_price", entry)
        eff_stop = t.get("breakeven_stop", stop)

        if stop >= entry:
            check(FAIL, "StopLadder", f"{sym}: stop {stop} >= entry {entry}")
        elif t1r <= entry:
            check(FAIL, "StopLadder", f"{sym}: 1R target {t1r} <= entry {entry}")
        elif price < eff_stop:
            check(FAIL, "StopLadder",
                  f"{sym}: price {price} < effective stop {eff_stop:.2f} — should be exited")
        else:
            check(OK, "StopLadder",
                  f"{sym}: OK  price={price}  stop={eff_stop:.2f}  1R={t1r}")


# ── 5. Time stop proximity ────────────────────────────────────────────────────

def check_time_stops(trades: list, now: datetime) -> None:
    from swing_agent.config import TIME_STOP_TRADING_DAYS
    for t in [t for t in trades if t.get("status") == "entered"]:
        sym = t["symbol"]
        entry_time = t.get("entry_time", "")
        if not entry_time:
            continue
        dt = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
        days_held = (now - dt).total_seconds() / 86400
        trading_days = days_held * (5 / 7)
        days_left = TIME_STOP_TRADING_DAYS - trading_days
        touched = t.get("touched_1r", False)
        if not touched and days_left < 0:
            check(FAIL, "TimeStop",
                  f"{sym}: {trading_days:.1f}/{TIME_STOP_TRADING_DAYS} trading days, "
                  f"touched_1r=False — should have been time-stopped")
        elif not touched and days_left < 3:
            check(WARN, "TimeStop",
                  f"{sym}: {trading_days:.1f}/{TIME_STOP_TRADING_DAYS} trading days, "
                  f"{days_left:.1f} left, touched_1r=False — fires soon")
        else:
            check(OK, "TimeStop",
                  f"{sym}: {trading_days:.1f}/{TIME_STOP_TRADING_DAYS} trading days  "
                  f"({days_left:.1f} left)  touched_1r={touched}")


# ── 6. Stuck pending fills ────────────────────────────────────────────────────

def check_pending_fills(trades: list, now: datetime) -> None:
    pending = [t for t in trades if t.get("status") == "pending_fill"]
    if not pending:
        check(OK, "Pending", "No stuck pending fills")
        return
    for t in pending:
        sym = t["symbol"]
        sig = t.get("signal_time", "")
        if sig:
            dt = datetime.fromisoformat(sig.replace("Z", "+00:00"))
            age_h = (now - dt).total_seconds() / 3600
            level = FAIL if age_h > 13 else WARN
            check(level, "Pending",
                  f"{sym}: pending_fill for {age_h:.1f}h — "
                  + ("resolve_pending_fills() may have not run" if age_h > 13 else "ok if signal was tonight"))
        else:
            check(WARN, "Pending", f"{sym}: pending_fill with no signal_time")


# ── 7. Ledger field integrity ─────────────────────────────────────────────────

def check_field_integrity(trades: list) -> None:
    required_open   = ["symbol", "type", "status", "entry", "stop",
                       "target_1R", "shares", "entry_time", "checked_at"]
    required_closed = ["symbol", "status", "entry", "exit_price",
                       "exit_reason", "realized_pnl"]
    closed_statuses = {"time_stop", "stopped", "breakeven", "target_hit"}

    for t in [t for t in trades if t.get("status") == "entered"]:
        missing = [k for k in required_open if k not in t]
        if missing:
            check(FAIL, "Integrity", f"{t['symbol']}: missing fields {missing}")
        else:
            check(OK, "Integrity", f"{t['symbol']} (open): all required fields present")

    for t in [t for t in trades if t.get("status") in closed_statuses]:
        missing = [k for k in required_closed if k not in t]
        if missing:
            check(FAIL, "Integrity", f"{t['symbol']}: missing closed fields {missing}")


# ── 8. Python imports ─────────────────────────────────────────────────────────

def check_imports() -> None:
    for mod in ["swing_agent.scanner", "swing_agent.run_daily",
                "swing_agent.config", "swing_agent.indicators"]:
        try:
            __import__(mod)
            check(OK, "Imports", f"{mod} importable")
        except Exception as e:
            check(FAIL, "Imports", f"{mod}: {e}")


# ── 9. Performance summary ────────────────────────────────────────────────────

def print_performance(trades: list) -> None:
    closed_statuses = {"time_stop", "stopped", "breakeven", "target_hit"}
    closed = [t for t in trades if t.get("status") in closed_statuses]
    open_t = [t for t in trades if t.get("status") == "entered"]
    if not closed:
        print("\n  No closed trades yet.")
        return

    wins = sum(1 for t in closed if t.get("realized_pnl", 0) > 0)
    total_rpnl = sum(t.get("realized_pnl", 0) for t in closed)
    r_mults = []
    for t in closed:
        rpnl = t.get("realized_pnl", 0)
        risk = t.get("risk_per_share", 0) * t.get("shares", 0)
        if risk:
            r_mults.append(rpnl / risk)
    avg_r = sum(r_mults) / len(r_mults) if r_mults else 0
    time_stops = sum(1 for t in closed if t.get("status") == "time_stop")
    open_pnl = sum(t.get("unrealized_pnl", 0) for t in open_t)

    print(f"\n  Closed trades : {len(closed)}  |  Wins: {wins} ({wins/len(closed)*100:.0f}%)")
    print(f"  Avg R         : {avg_r:+.2f}R")
    print(f"  Time stop rate: {time_stops}/{len(closed)} ({time_stops/len(closed)*100:.0f}%)")
    print(f"  Realized P&L  : ${total_rpnl:+.2f}")
    print(f"  Unrealized    : ${open_pnl:+.2f}  ({len(open_t)} open positions)")
    print(f"  Combined P&L  : ${total_rpnl+open_pnl:+.2f}")

    # Go-live gate status
    print(f"\n  Go-live gates:")
    print(f"    Closed trades    : {len(closed)}/30  {'✓' if len(closed) >= 30 else '✗'}")
    print(f"    Win rate         : {wins/len(closed)*100:.0f}%/40%  {'✓' if wins/len(closed) >= 0.40 else '✗'}")
    print(f"    Avg R            : {avg_r:+.2f}R / +0.3R  {'✓' if avg_r >= 0.3 else '✗'}")
    print(f"    Time stop rate   : {time_stops/len(closed)*100:.0f}%/<30%  {'✓' if time_stops/len(closed) < 0.30 else '✗'}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    now = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"  HEALTH CHECK  —  {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}")

    sync_ledger()

    if not LEDGER.exists():
        print("\n  FAIL: ledger not found after sync — cannot continue")
        return 1

    trades = json.loads(LEDGER.read_text())

    check_ledger_age(now)
    check_position_freshness(trades, now)
    check_stop_ladder(trades)
    check_time_stops(trades, now)
    check_pending_fills(trades, now)
    check_field_integrity(trades)
    check_imports()

    # Print findings grouped by section
    sections = dict.fromkeys(s for _, s, _ in findings)
    print()
    for section in sections:
        rows = [(lvl, msg) for lvl, sec, msg in findings if sec == section]
        for lvl, msg in rows:
            print(f"  [{lvl}] {section:<12} {msg}")

    # Performance summary
    print(f"\n{'─'*60}")
    print("  PERFORMANCE SUMMARY")
    print_performance(trades)
    print(f"{'='*60}\n")

    fail_count = sum(1 for lvl, _, _ in findings if lvl == FAIL)
    warn_count = sum(1 for lvl, _, _ in findings if lvl == WARN)
    if fail_count:
        print(f"  {fail_count} FAIL(s), {warn_count} WARN(s) — action required\n")
        return 1
    if warn_count:
        print(f"  0 FAILs, {warn_count} WARN(s) — review recommended\n")
        return 0
    print("  All checks passed\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
