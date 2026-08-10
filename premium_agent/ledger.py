"""Append PROPOSED options trades to the paper ledger. Never touches a live account.

Mirrors the swing agent's paper_ledger.json convention: every trade decision
is written here before anything else happens with it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LEDGER = Path("data/paper_options_ledger.json")


def _load(path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text())
    return []


def _save(path: Path, trades: list[dict]) -> None:
    path.write_text(json.dumps(trades, indent=2))


def propose_trade(trade: dict, path: str | Path = DEFAULT_LEDGER) -> dict:
    """Append a PROPOSED trade (CSP or covered call leg) to the ledger."""
    path = Path(path)
    trades = _load(path)
    trade = dict(trade)
    trade.setdefault("status", "PROPOSED")
    trade.setdefault("proposed_at", datetime.now(timezone.utc).isoformat())
    trades.append(trade)
    _save(path, trades)
    return trade


def update_trade(instrument_id: str, updates: dict, path: str | Path = DEFAULT_LEDGER) -> dict | None:
    """Update the most recent open trade for an instrument (e.g. on close/roll/assignment)."""
    path = Path(path)
    trades = _load(path)
    match = None
    for t in reversed(trades):
        if t.get("instrument_id") == instrument_id and t.get("status") not in (
            "closed", "assigned", "called_away", "rolled",
        ):
            match = t
            break
    if match is None:
        return None
    match.update(updates)
    _save(path, trades)
    return match


def roll_trade(old_instrument_id: str, close_debit: float, new_trade: dict, path: str | Path = DEFAULT_LEDGER) -> dict:
    """Close the open leg at old_instrument_id (paying close_debit to buy it back) and
    log new_trade as its replacement, linked via rolled_from so cumulative_credit can
    walk the whole chain.

    CLAUDE_OPTIONS.md Step 4: a roll should only be proposed when
    new_trade["credit"] - close_debit is positive (a net credit); this function
    still records a net-debit roll if asked to (it's a paper ledger, not a policy
    enforcer) but flags it via roll_net_credit / rolled_for_debit so it's visible
    on review rather than silently indistinguishable from a normal roll.
    """
    path = Path(path)
    old = update_trade(
        old_instrument_id,
        {"status": "rolled", "close_debit": close_debit, "closed_at": datetime.now(timezone.utc).isoformat()},
        path=path,
    )
    if old is None:
        raise ValueError(f"no open trade found for instrument_id={old_instrument_id!r}")

    net_credit = round(new_trade.get("credit", 0.0) - close_debit, 4)
    new_trade = dict(new_trade)
    new_trade["rolled_from"] = old_instrument_id
    new_trade["roll_net_credit"] = net_credit
    new_trade["rolled_for_debit"] = net_credit < 0
    return propose_trade(new_trade, path=path)


def cumulative_credit(instrument_id: str, path: str | Path = DEFAULT_LEDGER) -> float:
    """Net premium collected across a trade's whole roll chain (original sale plus
    every roll's net credit/debit), for the cost-basis and breakeven-progress math
    in CLAUDE_OPTIONS.md Step 4."""
    path = Path(path)
    by_id = {t["instrument_id"]: t for t in _load(path) if "instrument_id" in t}
    total = 0.0
    node = by_id.get(instrument_id)
    while node is not None:
        total += node.get("credit", 0.0) - (node.get("close_debit") or 0.0)
        node = by_id.get(node.get("rolled_from"))
    return round(total, 4)


def breakeven_progress_pct(instrument_id: str, strike: float, path: str | Path = DEFAULT_LEDGER) -> float:
    """How close cumulative premium has brought the cost basis to zero: 100% means
    enough premium has been collected across the roll chain to fully offset the
    strike (the "zero-cost-basis, no capital at risk" milestone)."""
    if strike <= 0:
        return 0.0
    return round(cumulative_credit(instrument_id, path=path) * 100 / (strike * 100) * 100, 2)
