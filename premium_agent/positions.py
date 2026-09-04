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
        "dividends_received": 0.0,
        "dividend_dates_credited": [],
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


def credit_dividend(
    source_instrument_id: str,
    payable_date: str,
    amount: float,
    path: str | Path = DEFAULT_POSITIONS,
) -> bool:
    """Credit one dividend payment to a lot's payback counter, keyed by
    payable_date so the same payment never gets counted twice across daily
    cycles (manage.credit_dividends calls this once per lot per cycle, and
    payable_date stays the same across every one of those calls until the
    next ex-dividend date rolls around).

    TESTING (2026-09-04): dividends are folded into breakeven_progress_pct
    as a third income stream alongside CSP/covered-call premium -- "Option
    A" from the DRIP conversation, not the full DRIP share-count model
    ("Option B"). Whether the dividend was actually taken as cash or DRIP'd
    in the real brokerage account, this credits the same dollar amount either
    way; it does not model DRIP's actual mechanism (buying more shares at
    the payment-date price). Revisit after we've seen how this tracks in
    practice -- CLAUDE_OPTIONS.md Step 5 doesn't have a signed-off dividend
    rule yet, this is exploratory.

    amount is a *total* dollar amount for the lot (dividend_per_share x
    shares -- see manage.credit_dividends), stored on the lot as-is under
    dividends_received. breakeven_progress_pct divides it back down to
    per-share before folding it into its per-share premium ratio -- don't
    pass a per-share amount here, it would get double-divided.

    Returns True if credited, False if this payable_date was already
    credited for this lot (or the lot doesn't exist).
    """
    path = Path(path)
    positions = _load(path)
    for p in positions:
        if p["source_instrument_id"] != source_instrument_id:
            continue
        credited = p.setdefault("dividend_dates_credited", [])
        if payable_date in credited:
            return False
        p["dividends_received"] = p.get("dividends_received", 0.0) + amount
        credited.append(payable_date)
        _save(path, positions)
        return True
    return False


def breakeven_progress_pct(
    source_instrument_id: str,
    ledger_path: str | Path = ledger.DEFAULT_LEDGER,
    path: str | Path = DEFAULT_POSITIONS,
) -> float | None:
    """This lot's own, independent progress toward zero cost basis: the CSP
    premium collected before assignment (strike_at_assignment - cost_basis)
    plus every covered-call trade ever written against this lot since
    (ledger.covered_call_premium_for_lot), plus dividends credited to this
    lot since assignment (credit_dividend -- testing as of 2026-09-04, see
    that function's docstring), as a percentage of the original strike. 100%
    means this specific lot -- and no other -- has fully paid for itself.
    Returns None if no lot with this source_instrument_id is open.

    Units note: csp_premium and call_premium are both per-share dollar
    amounts (the ledger stores `credit` per-share, standard options
    convention) -- the whole ratio is share-count-independent by
    construction. dividends_received on the lot is stored as a *total*
    dollar amount (dividend_per_share x shares, see credit_dividend), so it
    has to be divided back down to per-share here before joining the same
    ratio, or it would swamp the percentage for any lot with shares > 1.
    """
    lot = lot_by_source(source_instrument_id, path)
    if lot is None:
        return None
    strike = lot["strike_at_assignment"]
    if strike <= 0:
        return 0.0
    csp_premium = strike - lot["cost_basis"]
    call_premium = ledger.covered_call_premium_for_lot(source_instrument_id, path=ledger_path)
    shares = lot.get("shares") or 1
    dividends_per_share = lot.get("dividends_received", 0.0) / shares
    return round((csp_premium + call_premium + dividends_per_share) / strike * 100, 2)


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
