"""Append PROPOSED options trades to the paper ledger. Never touches a live account.

Mirrors the swing agent's paper_ledger.json convention: every trade decision
is written here before anything else happens with it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_LEDGER = Path("data/paper_options_ledger.json")
CLOSED_STATUSES = {"closed", "assigned", "called_away", "rolled"}


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
        if t.get("instrument_id") == instrument_id and t.get("status") not in CLOSED_STATUSES:
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


def roll_count(instrument_id: str, path: str | Path = DEFAULT_LEDGER) -> int:
    """How many times the chain ending at instrument_id has already been
    rolled, for CLAUDE_OPTIONS.md Step 4's max_rolls_before_assignment cap."""
    by_id = {t["instrument_id"]: t for t in _load(Path(path)) if "instrument_id" in t}
    count = 0
    node = by_id.get(instrument_id)
    while node is not None and node.get("rolled_from"):
        count += 1
        node = by_id.get(node.get("rolled_from"))
    return count


def covered_call_premium_for_lot(source_instrument_id: str, path: str | Path = DEFAULT_LEDGER) -> float:
    """Sum of credit - close_debit across every covered-call trade ever
    written against one assignment lot (tagged trade["source_instrument_id"]
    == the CSP instrument_id whose assignment created the lot), regardless of
    each trade's current status. Unlike cumulative_credit (which walks a
    single rolled_from chain), this sums every covered-call chain the lot has
    ever run -- profit-take closes one chain and a fresh one starts next
    cycle, and all of them count toward the lot's own payback counter
    (premium_agent.positions.breakeven_progress_pct)."""
    total = 0.0
    for t in _load(Path(path)):
        if t.get("type") == "covered_call" and t.get("source_instrument_id") == source_instrument_id:
            total += t.get("credit", 0.0) - (t.get("close_debit") or 0.0)
    return round(total, 4)


def open_trades(path: str | Path = DEFAULT_LEDGER) -> list[dict]:
    return [t for t in _load(Path(path)) if t.get("status") not in CLOSED_STATUSES]


def deployed_collateral(path: str | Path = DEFAULT_LEDGER, symbol: str | None = None) -> float:
    """Collateral currently tied up by open CSP legs, for the Step 6 sizing caps
    (max_collateral_pct_of_equity, max_single_name_pct_of_equity). Only CSP
    legs carry a "collateral" field -- covered calls are written against
    shares already owned, not new cash, so they don't add to this.
    """
    trades = open_trades(path)
    if symbol:
        trades = [t for t in trades if t.get("symbol") == symbol]
    return round(sum(t.get("collateral", 0.0) for t in trades), 2)
