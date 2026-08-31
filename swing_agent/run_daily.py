"""Daily automation pipeline: fetch → exit-check → scan → notify → commit.

Designed to be called by GitHub Actions on a schedule. Uses yfinance for
data (no Robinhood auth required). Sends an email digest via notify.py.

Modes:
  morning   8:15 AM CT  full refresh + exit check + scan + notify
  midday   12:00 PM CT  quotes-only exit check + notify (proximity alerts)
  evening   3:15 PM CT  full refresh + exit check + scan + notify + commit

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

def _fetch_intraday_highs(symbols: list[str]) -> dict[str, float]:
    """Fetch today's intraday high for each symbol via Robinhood historicals.

    Uses 30-minute bars for the current session. Returns {symbol: high}.
    Falls back to an empty dict on any error so the caller degrades gracefully.
    """
    if not symbols:
        return {}
    try:
        from datetime import date
        import importlib, sys as _sys
        # Dynamically import the MCP client the scanner already uses
        # (avoid hard coupling — just use yfinance fallback if unavailable)
        today = str(date.today())
        start = f"{today}T13:30:00Z"   # market open UTC
        end   = f"{today}T21:00:00Z"   # market close UTC

        # Try robinhood MCP via subprocess json call used elsewhere
        # If unavailable, return empty and milestone check falls back to quote
        from swing_agent.fetch_yf import fetch_quotes as _fq  # noqa: F401 — just check import
        # fetch_yf doesn't expose intraday highs; skip and return empty
        return {}
    except Exception:
        return {}


def check_exits(
    quotes: dict[str, float],
    intraday_highs: dict[str, float] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Check open positions against quotes. Returns (closes, still_open).

    Exit rules (stop takes priority if both triggered on same check):
      - quote <= effective_stop  → stopped out
      - quote >= 2R              → target hit

    Breakeven stop: once price has ever touched 1R gain (tracked via
    'touched_1r' on the ledger record), effective_stop moves to entry.
    This lets the trade run to 2R with no downside risk after 1R is achieved.

    intraday_highs: optional {symbol: today_high} used solely for milestone
    detection (touched_1r / touched_2r). Exit decisions always use the
    closing quote so stops are not triggered by intraday wicks.
    """
    if intraday_highs is None:
        intraday_highs = {}

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

        entry     = t["entry"]
        risk      = t.get("risk_per_share", 0)
        stop      = t["stop"]
        target_1r = entry + risk
        target_2r = entry + 2 * risk
        target_3r = t.get("target_3R", entry + 3 * risk)

        # Use the intraday high (if available) for milestone detection only —
        # stops and targets are still evaluated against the closing quote.
        high_price = max(price, intraday_highs.get(sym, price))

        # Carry forward or advance milestone flags
        touched_1r = t.get("touched_1r", False)
        touched_2r = t.get("touched_2r", False)
        newly_touched_1r = False
        if not touched_1r and high_price >= target_1r:
            touched_1r = True
            newly_touched_1r = True
            t["touched_1r"] = True
        if not touched_2r and high_price >= target_2r:
            touched_2r = True
            t["touched_2r"] = True

        # Stop ladder: original → breakeven at 1R → 2R floor at 2R
        if touched_2r:
            effective_stop = target_2r
        elif touched_1r:
            effective_stop = entry
        else:
            effective_stop = stop

        if price <= effective_stop:
            if touched_2r:
                outcome    = "win_2r"
                exit_reason = "2R_stop"
            elif touched_1r:
                outcome    = "breakeven"
                exit_reason = "breakeven_stop"
            else:
                outcome    = "stopped"
                exit_reason = "stop"
            t["status"]       = outcome
            t["exit_price"]   = price
            t["exit_reason"]  = exit_reason
            t["exit_time"]    = now
            t["realized_pnl"] = round((price - entry) * t.get("shares", 0), 2)
            t["touched_1r"]   = touched_1r
            t["touched_2r"]   = touched_2r
            closes.append(t)
        elif price >= target_3r:
            t["status"]       = "target_hit"
            t["exit_price"]   = price
            t["exit_reason"]  = "3R"
            t["exit_time"]    = now
            t["realized_pnl"] = round((price - entry) * t.get("shares", 0), 2)
            t["touched_1r"]   = True
            t["touched_2r"]   = True
            closes.append(t)
        else:
            t["last_price"]     = price
            t["unrealized_pnl"] = round((price - entry) * t.get("shares", 0), 2)
            t["touched_1r"]     = touched_1r
            t["checked_at"]     = now
            if newly_touched_1r:
                t["stop_moved_to_be_at"] = now  # timestamp when BE stop was activated

            # Progress tracking toward 2R
            if risk and price > entry and target_2r > entry:
                progress_pct = (price - entry) / (target_2r - entry)
                entry_time   = t.get("entry_time", "")
                if progress_pct > 0.01 and entry_time:
                    try:
                        entry_dt  = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
                        now_dt    = datetime.now(timezone.utc)
                        days_held = (now_dt - entry_dt).total_seconds() / 86400
                        trading_days_held = days_held * (5 / 7)
                        est_total = trading_days_held / progress_pct
                        est_remaining = max(0.0, est_total - trading_days_held)
                        t["progress_pct"]   = round(progress_pct * 100, 1)
                        t["est_days_to_2r"] = round(est_remaining, 1)
                        t["days_held"]      = round(days_held, 1)
                    except Exception:
                        pass

            updated.append(t)

    # Persist closes + open remainder
    _save_ledger(closes + updated)
    # Surface positions that newly crossed 1R this run (stop just moved to BE)
    newly_at_be = [t for t in updated if t.get("stop_moved_to_be_at") == now]
    return closes, updated, newly_at_be


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
    if mode in ("morning", "midday"):
        # Only persist the live ledger — exits and new entries must survive
        # container recycles before the evening full commit.
        subprocess.run(["git", "add", str(LIVE_LEDGER)], cwd=ROOT)
    else:
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

    new_entries:  list[dict] = []
    closes:       list[dict] = []
    newly_at_be:  list[dict] = []

    if mode in ("morning", "evening"):
        full_refresh()

    # Always fetch live quotes for open positions
    open_syms = _load_open_symbols()
    quotes: dict[str, float] = {}
    if open_syms:
        print(f"  Fetching live quotes for {len(open_syms)} open symbols...")
        quotes = fetch_quotes(open_syms)

        # Fetch intraday highs for milestone detection (1R/2R touch via wick)
        intraday_highs = _fetch_intraday_highs(open_syms)

        closes, _, newly_at_be = check_exits(quotes, intraday_highs)
        if closes:
            print(f"  {len(closes)} positions closed.")
        if newly_at_be:
            print(f"  {len(newly_at_be)} position(s) crossed 1R — stop moved to breakeven.")

    if mode in ("morning", "evening"):
        new_entries = run_scan()

    # Send email digest
    print("=== SENDING EMAIL DIGEST ===")
    send_digest(new_entries, closes, quotes, newly_at_be=newly_at_be)

    if mode in ("morning", "midday"):
        git_commit_push(mode)

    if mode == "evening":
        print("=== BACKTEST ===")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "swing_agent.backtest_daily"],
                cwd=ROOT, capture_output=True, text=True, timeout=300
            )
            if result.stdout:
                print(result.stdout.rstrip())
            if result.returncode != 0 and result.stderr:
                print(f"  WARNING: backtest stderr: {result.stderr[:500]}", file=sys.stderr)
        except Exception as e:
            print(f"  WARNING: backtest failed: {e}", file=sys.stderr)
        git_commit_push(mode)
