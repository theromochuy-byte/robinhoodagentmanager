"""Parse Robinhood option contract+quote records into a clean DataFrame.

The agent (in Claude Code) resolves an underlying with get_option_chains, lists
strikes in the target expiration window with get_option_instruments, batches
get_option_quotes for those instrument ids, merges each instrument with its
quote into one record, and saves the list to
data/options/<SYMBOL>_<EXPIRY>.json. This module does no network I/O, which
keeps the screening logic testable and offline.

Expected raw record shape (one dict per contract, as saved by the agent):
{
  "id": "...", "chain_symbol": "AAPL", "expiration_date": "2026-09-18",
  "strike_price": "60.0000", "type": "put",
  "quote": {
    "bid_price": "0.00", "ask_price": "0.03", "mark_price": "0.015",
    "delta": "-0.000221", "implied_volatility": "1.538172",
    "open_interest": 93, "volume": 0,
    "chance_of_profit_short": "0.998678", "chance_of_profit_long": "0.001322"
  }
}
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def _float_or_none(v) -> float | None:
    if v in (None, ""):
        return None
    return float(v)


def contracts_to_df(raw: list[dict]) -> pd.DataFrame:
    rows = []
    for c in raw:
        q = c.get("quote", {})
        bid = float(q.get("bid_price") or 0)
        ask = float(q.get("ask_price") or 0)
        mid = round((bid + ask) / 2, 4) if (bid and ask) else _float_or_none(q.get("mark_price"))
        rows.append(
            {
                "instrument_id": c["id"],
                "symbol": c["chain_symbol"],
                "expiration_date": c["expiration_date"],
                "strike": float(c["strike_price"]),
                "type": c["type"],
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "delta": _float_or_none(q.get("delta")),
                "iv": _float_or_none(q.get("implied_volatility")),
                "open_interest": int(q.get("open_interest") or 0),
                "volume": int(q.get("volume") or 0),
                "chance_of_profit_short": _float_or_none(q.get("chance_of_profit_short")),
            }
        )
    return pd.DataFrame(rows)


def load(path: str | Path) -> pd.DataFrame:
    with open(path) as f:
        raw = json.load(f)
    return contracts_to_df(raw)


def load_symbol_contracts(symbol: str, options_dir: str | Path) -> pd.DataFrame:
    """Concatenate every data/options/<SYMBOL>_<EXPIRY>.json file for this symbol
    (one file per expiration, per this module's documented convention). Shared
    by scan.py (new-candidate screening) and manage.py (roll-candidate lookup
    for already-open legs) so both read the same on-disk data the same way."""
    frames = [load(p) for p in sorted(Path(options_dir).glob(f"{symbol}_*.json"))]
    if not frames:
        return pd.DataFrame(
            columns=["instrument_id", "symbol", "expiration_date", "strike", "type",
                     "bid", "ask", "mid", "delta", "iv", "open_interest", "volume",
                     "chance_of_profit_short"]
        )
    return pd.concat(frames, ignore_index=True)
