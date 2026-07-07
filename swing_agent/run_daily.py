"""Daily automation pipeline: fetch → exit-check → scan → notify → commit.

Designed to be called by GitHub Actions on a schedule. Uses yfinance for
data (no Robinhood auth required). Sends an email digest via notify.py.

Modes:
  morning   9:35 AM ET  full refresh + exit check + scan + notify
  midday    1:00 PM ET  quotes-only exit check + notify (proximity alerts)
  evening   4:30 PM ET  full refresh + exit check + scan + notify + commit

Usage:
  python3 -m swing_agent.run_daily --mode morning
  python3 -m swing_agent.run_daily --mode midday
  python3 -m swing_agent.run_daily --mode evening
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT        = Path(__file__).resolve().parent.parent
DATA        = ROOT / "data"
LIVE_LEDGER = DATA / "paper_trades_live.json"


def _load_ledger() -> list[dict]:
    if LIVE_LEDGER.exists():
        return json.loads(LIVE_LEDGER.read_text())
    return []


def _save_ledger(trades: list[dict]) -> None:
    LIVE_LEDGER.write_text(json.dumps(trades, indent=2))


# ---------------------------------------------------------------------------
# Data refresh
# ---------------------------------------------------------------------------

def full_refresh() -> None:
    print("=== DATA REFRESH (yfinance) ===")
    from swing_agent.fetch_yf import (
        _load_universe, fetch_daily, fetch_4hour, save
    )
    syms = _load_universe()

    print(f"  Fetching daily bars for {len(syms)} symbols...")
    day_data = fetch_daily(syms)
    saved_d  = save(day_data, "_day")
    print(f"  Saved {len(saved_d)} daily files.")

    print(f"  Fetching 4-hour bars for {len(syms)} symbols...")
    h4_data  = fetch_4hour(syms)
    saved_h  = save(h4_data, "_4hour")
    print(f"  Saved {len(saved_h)} 4-hour files.")


# ---------------------------------------------------------------------------
# Exit checker — applies closing quotes to open positions
# ---------------------------------------------------------------------------

def check_exits(quotes: dict[str, float]) -> tuple[list[dict], list[dict]]:
    """Check open positions against quotes. Returns (closes, still_open).

    A position closes when:
      - quote <= stop  → stopped out (stop takes priority on same bar)
      - quote >= 2R    → target hit
    """
    trades  = _load_ledger()
    closes  = []
    updated = []
    now     = datetime.now(timezone.utc).isoformat()

    for t in trades:
        if t.get("status") != "entered":
            updated.append(t)
            continue

        sym   = t["symbol"]
        price = quotes.get(sym)
        if price is None:
            updated.append(t)
            continue

        stop   = t["stop"]
        target = t["target_2R"]

        if price <= stop:
            t["status"]       = "stopped"
            t["exit_price"]   = price
            t["exit_reason"]  = "stop"
            t["exit_time"]    = now
            t["realized_pnl"] = round((price - t["entry"]) * t.get("shares", 0), 2)
            closes.append(t)
        elif price >= target:
            t["status"]       = "target_hit"
            t["exit_price"]   = price
            t["exit_reason"]  = "2R"
            t["exit_time"]    = now
            t["realized_pnl"] = round((price - t["entry"]) * t.get("shares", 0), 2)
            closes.append(t)
        else:
            t["last_price"]    = price
            t["unrealized_pnl"] = round((price - t["entry"]) * t.get("shares", 0), 2)
            t["checked_at"]    = now
            updated.append(t)

    # Persist closes + open remainder
    _save_ledger(closes + updated)
    return closes, updated


# ---------------------------------------------------------------------------
# Scanner wrapper — returns only trades added THIS run
# ---------------------------------------------------------------------------

def run_scan() -> list[dict]:
    """Run scanner; return list of new trade dicts entered today."""
    print("=== DAILY SCAN ===")
    before = {id(t) for t in _load_ledger() if t.get("status") == "entered"}

    subprocess.run(
        [sys.executable, "-m", "swing_agent.scanner"],
        cwd=ROOT, check=False,
    )

    after_trades = _load_ledger()
    new_entries  = [
        t for t in after_trades
        if t.get("status") == "entered" and id(t) not in before
    ]
    # id() won't survive reload — compare by (symbol, type, entry_time) instead
    # Re-derive: any trade with entry_date == today
    today = str(date.today())
    new_entries = [
        t for t in after_trades
        if t.get("status") == "entered"
        and (t.get("entry_date", "") == today
             or (t.get("entry_time", "") or "").startswith(today))
    ]
    print(f"  {len(new_entries)} new entries today.")
    return new_entries


# ---------------------------------------------------------------------------
# Git commit + push
# ---------------------------------------------------------------------------

def git_commit_push(mode: str) -> None:
    print("=== COMMITTING ===")
    subprocess.run(["git", "add", "data/", "reports/"], cwd=ROOT)
    result = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=ROOT
    )
    if result.returncode == 0:
        print("  Nothing to commit.")
        return
    today = str(date.today())
    msg = f"auto: {mode} run {today} — data refresh, exit check, scan"
    subprocess.run(["git", "commit", "-m", msg], cwd=ROOT, check=True)
    subprocess.run(
        ["git", "push", "-u", "origin", "claude/wonderful-cerf-sa7c0d"],
        cwd=ROOT, check=True,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--mode" not in args:
        print("Usage: python3 -m swing_agent.run_daily --mode [morning|midday|evening]")
        sys.exit(1)

    mode = args[args.index("--mode") + 1]
    if mode not in ("morning", "midday", "evening"):
        print(f"Unknown mode: {mode}. Use morning, midday, or evening.", file=sys.stderr)
        sys.exit(1)

    from swing_agent.fetch_yf import _load_open_symbols, fetch_quotes
    from swing_agent.notify   import send_digest

    new_entries: list[dict] = []
    closes:      list[dict] = []

    if mode in ("morning", "evening"):
        full_refresh()

    # Always fetch live quotes for open positions
    open_syms = _load_open_symbols()
    quotes: dict[str, float] = {}
    if open_syms:
        print(f"  Fetching live quotes for {len(open_syms)} open symbols...")
        quotes = fetch_quotes(open_syms)
        closes, _ = check_exits(quotes)
        if closes:
            print(f"  {len(closes)} positions closed.")

    if mode in ("morning", "evening"):
        new_entries = run_scan()

    # Send email digest
    print("=== SENDING EMAIL DIGEST ===")
    send_digest(new_entries, closes, quotes)

    if mode == "evening":
        git_commit_push(mode)
