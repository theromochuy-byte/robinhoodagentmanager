"""Track "shares owned" state from CSP assignment -- the wheel's other leg.

Separate from data/paper_options_ledger.json (the full history of every
proposed trade) the same way swing_agent's paper_trades_live.json is
separate from its backtest ledger: this file is current state only, so
scan.py can cheaply answer "which symbols need a CSP screen (Step 1-3) vs.
a covered-call screen (Step 5)" without replaying the whole ledger.

Multiple lots per symbol (2026-08-19, barbell/payback sign-off): a symbol
can have more than one open "shares owned" lot at once now that CSPs are
opened in barbell pairs -- either leg can assign first, and if the account
later opens a fresh barbell on the same symbol, a second lot can start
while the first is still mid-payback. Each lot is keyed by
`source_instrument_id` (the CSP instrument whose assignment created it),
not by symbol, and carries its own independent payback counter per
CLAUDE_OPTIONS.md's "Barbell entry + payback" section -- there is no
account-wide or symbol-wide payback total, only per-lot ones.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from premium_agent import ledger

DEFAULT_POSITIONS = Path("data/options_positions.json")


def _load(path: Path) -> list[dict]:
    if path.exists():
        return json.loads(path.read_text())
    return []


def _save(path: Path, positions: list[dict]) -> None:
    path.write_text(json.dumps(positions, indent=2))


def open_positions(path: str | Path = DEFAULT_POSITIONS) -> list[dict]:
    return _load(Path(path))


def held_symbols(path: str | Path = DEFAULT_POSITIONS) -> set[str]:
    return {p["symbol"] for p in open_positions(path)}


def lots_for_symbol(symbol: str, path: str | Path = DEFAULT_POSITIONS) -> list[dict]:
    """All open lots for a symbol -- usually one, but a barbell can leave two
    lots of the same symbol open at once if both legs eventually assign."""
    return [p for p in open_positions(path) if p["symbol"] == symbol]


def lot_by_source(source_instrument_id: str, path: str | Path = DEFAULT_POSITIONS) -> dict | None:
    return next((p for p in open_positions(path) if p["source_instrument_id"] == source_instrument_id), None)


def capital_in_use(path: str | Path = DEFAULT_POSITIONS, symbol: str | None = None) -> float:
    """Capital tied up in assigned shares (shares x cost_basis, not live market
    price -- same convention swing_agent uses for its own capital_in_use:
    original capital committed, not mark-to-market). A covered call against
    these shares needs no separate collateral, but the shares themselves are
    real capital competing for the same cap -- this was previously invisible
    to ledger.deployed_collateral(), which only sums open CSP collateral.
    Sums across every lot, including multiple lots of the same symbol.
    """
    pos = open_positions(path)
    if symbol:
        pos = [p for p in pos if p["symbol"] == symbol]
    return round(sum(p["shares"] * p["cost_basis"] for p in pos), 2)


def add_position(
    symbol: str,
    shares: int,
    cost_basis: float,
    source_instrument_id: str,
    strike_at_assignment: float,
    path: str | Path = DEFAULT_POSITIONS,
) -> dict:
    """Record shares acquired via CSP assignment (CLAUDE_OPTIONS.md Step 4 -> 5).

    source_instrument_id is the assigned CSP's instrument_id -- the lot's
    permanent identity and the key its independent payback counter is tracked
    under (see breakeven_progress_pct). strike_at_assignment is the CSP
    strike (cost_basis before any premium collected), needed because
    cost_basis itself is a snapshot at assignment time and doesn't move as
    more covered-call premium comes in -- breakeven_progress_pct derives the
    running "how close to zero cost basis" figure from the two.
    """
    path = Path(path)
    positions = _load(path)
    position = {
        "symbol": symbol,
        "shares": shares,
        "cost_basis": cost_basis,
        "source_instrument_id": source_instrument_id,
        "strike_at_assignment": strike_at_assignment,
        "since": datetime.now(timezone.utc).isoformat(),
    }
    positions.append(position)
    _save(path, positions)
    return position


def remove_position(source_instrument_id: str, path: str | Path = DEFAULT_POSITIONS) -> dict | None:
    """Called away, or manually closed -- removes one lot, identified by its
    originating CSP instrument_id (not by symbol, since a symbol can have more
    than one open lot at once)."""
    path = Path(path)
    positions = _load(path)
    match = next((p for p in positions if p["source_instrument_id"] == source_instrument_id), None)
    if match is None:
        return None
    positions = [p for p in positions if p["source_instrument_id"] != source_instrument_id]
    _save(path, positions)
    return match


def breakeven_progress_pct(
    source_instrument_id: str,
    ledger_path: str | Path = ledger.DEFAULT_LEDGER,
    path: str | Path = DEFAULT_POSITIONS,
) -> float | None:
    """This lot's own, independent progress toward zero cost basis: the CSP
    premium collected before assignment (strike_at_assignment - cost_basis)
    plus every covered-call trade ever written against this lot since
    (ledger.covered_call_premium_for_lot), as a percentage of the original
    strike. 100% means this specific lot -- and no other -- has fully paid
    for itself. Returns None if no lot with this source_instrument_id is open.
    """
    lot = lot_by_source(source_instrument_id, path)
    if lot is None:
        return None
    strike = lot["strike_at_assignment"]
    if strike <= 0:
        return 0.0
    csp_premium = strike - lot["cost_basis"]
    call_premium = ledger.covered_call_premium_for_lot(source_instrument_id, path=ledger_path)
    return round((csp_premium + call_premium) / strike * 100, 2)


def is_paid_off(
    source_instrument_id: str,
    ledger_path: str | Path = ledger.DEFAULT_LEDGER,
    path: str | Path = DEFAULT_POSITIONS,
) -> bool:
    """True once this lot's own payback counter has reached 100% -- per
    sign-off, a paid-off lot is held indefinitely (never sold to free up
    capital) and its covered calls roll without the max_rolls_before_assignment
    cap, since the goal shifts from "resolve this position" to "keep it
    generating dividends + premium income permanently."""
    progress = breakeven_progress_pct(source_instrument_id, ledger_path, path)
    return progress is not None and progress >= 100.0
