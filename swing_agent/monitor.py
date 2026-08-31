"""Open paper-position monitor.

Reads paper_trades_live.json, compares each open position's stop and 2R target
against current quotes, and marks exits. Updates the ledger in place.

Workflow:
  1. python3 -m swing_agent.monitor --symbols
     → prints comma-separated list of open symbols (feed to get_equity_quotes)

  2. python3 -m swing_agent.monitor --check '{"RTX":185.0,"USB":58.5,...}'
     → checks each open position, prints a status table, updates ledger

  3. python3 -m swing_agent.monitor --status
     → pretty-prints current open positions without fetching quotes
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT        = Path(__file__).resolve().parent.parent
DATA        = ROOT / "data"
LIVE_LEDGER = DATA / "paper_trades_live.json"


def _load() -> list[dict]:
    if LIVE_LEDGER.exists():
        return json.loads(LIVE_LEDGER.read_text())
    return []


def _save(trades: list[dict]) -> None:
    LIVE_LEDGER.write_text(json.dumps(trades, indent=2))


def open_positions(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("status") == "entered"]


def check_exits(trades: list[dict], quotes: dict[str, float]) -> list[dict]:
    """Delegate to run_daily.check_exits — single source of truth for exit logic.

    Returns the full updated trade list (closes + still-open).
    """
    from swing_agent.run_daily import check_exits as _check
    closes, still_open, _ = _check(quotes)
    return closes + still_open


CLOSED_STATUSES = ("stopped", "breakeven", "win_2r", "target_hit")

def print_status(trades: list[dict], quotes: dict[str, float] | None = None) -> None:
    open_t   = [t for t in trades if t.get("status") == "entered"]
    closed_t = [t for t in trades if t.get("status") in CLOSED_STATUSES]

    print(f"\n{'='*70}")
    print(f"PAPER POSITION MONITOR  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*70}")

    if open_t:
        print(f"\nOPEN POSITIONS ({len(open_t)})")
        header = f"  {'SYM':<6} {'TYPE':<14} {'ENTRY':>8} {'STOP':>8} {'2R':>8} {'LAST':>8} {'UNRL PnL':>10}  BE"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for t in open_t:
            last = quotes.get(t["symbol"], t.get("last_price", float("nan"))) if quotes else t.get("last_price", float("nan"))
            unrl = t.get("unrealized_pnl", "")
            unrl_str = f"${unrl:+.2f}" if isinstance(unrl, (int, float)) else "—"
            last_str = f"{last:.2f}" if isinstance(last, (int, float)) else "—"
            be_flag  = "✓" if t.get("touched_1r") else " "
            print(f"  {t['symbol']:<6} {t['type']:<14} {t['entry']:>8.2f} {t['stop']:>8.2f} "
                  f"{t['target_2R']:>8.2f} {last_str:>8} {unrl_str:>10}  {be_flag}")
    else:
        print("\n  No open positions.")

    if closed_t:
        print(f"\nCLOSED ({len(closed_t)})")
        for t in closed_t:
            status = t["status"]
            label  = {"stopped": "LOSS", "breakeven": "BE  ", "win_2r": "W-2R", "target_hit": "WIN "}.get(status, status)
            pnl    = t.get("realized_pnl", 0)
            print(f"  {t['symbol']:<6} {t['type']:<14} {label}  "
                  f"exit=${t.get('exit_price', '?')}  P&L=${pnl:+.2f}")

    total_open_pnl   = sum(t.get("unrealized_pnl", 0) for t in open_t if isinstance(t.get("unrealized_pnl"), (int, float)))
    total_closed_pnl = sum(t.get("realized_pnl",   0) for t in closed_t)
    print(f"\nUnrealized P&L: ${total_open_pnl:+.2f}  |  Realized: ${total_closed_pnl:+.2f}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] == "--status":
        trades = _load()
        print_status(trades)
        sys.exit(0)

    if args[0] == "--symbols":
        trades = _load()
        open_syms = list({t["symbol"] for t in open_positions(trades)})
        if open_syms:
            print(",".join(sorted(open_syms)))
        else:
            print("No open positions.")
        sys.exit(0)

    if args[0] == "--check" and len(args) == 2:
        try:
            quotes = json.loads(args[1])
        except json.JSONDecodeError as e:
            print(f"Invalid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        trades = _load()
        trades = check_exits(trades, quotes)
        _save(trades)
        print_status(trades, quotes)
        sys.exit(0)

    print("Usage:")
    print("  python3 -m swing_agent.monitor --status")
    print("  python3 -m swing_agent.monitor --symbols")
    print('  python3 -m swing_agent.monitor --check \'{"RTX":190.0,"USB":60.0}\'')
    sys.exit(1)
