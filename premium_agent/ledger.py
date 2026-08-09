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
