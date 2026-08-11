"""Track "shares owned" state from CSP assignment -- the wheel's other leg.

Separate from data/paper_options_ledger.json (the full history of every
proposed trade) the same way swing_agent's paper_trades_live.json is
separate from its backtest ledger: this file is current state only, so
scan.py can cheaply answer "which symbols need a CSP screen (Step 1-3) vs.
a covered-call screen (Step 5)" without replaying the whole ledger.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

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


def capital_in_use(path: str | Path = DEFAULT_POSITIONS, symbol: str | None = None) -> float:
    """Capital tied up in assigned shares (shares x cost_basis, not live market
    price -- same convention swing_agent uses for its own capital_in_use:
    original capital committed, not mark-to-market). A covered call against
    these shares needs no separate collateral, but the shares themselves are
    real capital competing for the same cap -- this was previously invisible
    to ledger.deployed_collateral(), which only sums open CSP collateral.
    """
    pos = open_positions(path)
    if symbol:
        pos = [p for p in pos if p["symbol"] == symbol]
    return round(sum(p["shares"] * p["cost_basis"] for p in pos), 2)


def add_position(symbol: str, shares: int, cost_basis: float, path: str | Path = DEFAULT_POSITIONS) -> dict:
    """Record shares acquired via CSP assignment (CLAUDE_OPTIONS.md Step 4 -> 5)."""
    path = Path(path)
    positions = _load(path)
    position = {
        "symbol": symbol,
        "shares": shares,
        "cost_basis": cost_basis,
        "since": datetime.now(timezone.utc).isoformat(),
    }
    positions.append(position)
    _save(path, positions)
    return position


def remove_position(symbol: str, path: str | Path = DEFAULT_POSITIONS) -> dict | None:
    """Called away, or manually closed -- removes the open share position for symbol."""
    path = Path(path)
    positions = _load(path)
    match = next((p for p in positions if p["symbol"] == symbol), None)
    if match is None:
        return None
    positions = [p for p in positions if p["symbol"] != symbol]
    _save(path, positions)
    return match
