"""Screen option contracts for cash-secured put and covered call candidates.

Rules implemented here follow CLAUDE_OPTIONS.md Step 3 (CSP) and Step 5
(covered call): delta band, DTE window, liquidity floor, and max bid/ask
spread. Callers apply the earnings-avoidance and IV-richness gates (Steps 1-2)
before calling these, since those need data this module doesn't own
(earnings calendar, underlying historicals).
"""
from __future__ import annotations

from datetime import date

import pandas as pd

DEFAULT_DTE_RANGE = (21, 45)
DEFAULT_DELTA_RANGE = (0.15, 0.30)
DEFAULT_MIN_OPEN_INTEREST = 100
DEFAULT_MAX_SPREAD_PCT = 0.15
DEFAULT_MIN_BID = 0.05
DEFAULT_MIN_YIELD_PCT = 1.0  # credit as a % of collateral/basis over the DTE window


def days_to_expiration(expiration_date: str, as_of: date | None = None) -> int:
    as_of = as_of or date.today()
    return (date.fromisoformat(expiration_date) - as_of).days


def _liquidity_filter(df: pd.DataFrame, min_open_interest: float, max_spread_pct: float, min_bid: float) -> pd.DataFrame:
    df = df[df["bid"] >= min_bid]
    df = df[df["open_interest"] >= min_open_interest]
    spread = df["ask"] - df["bid"]
    df = df[spread <= max_spread_pct * df["mid"].clip(lower=0.01)]
    return df


def screen_csp(
    contracts: pd.DataFrame,
    underlying_price: float,
    *,
    dte_range: tuple[int, int] = DEFAULT_DTE_RANGE,
    delta_range: tuple[float, float] = DEFAULT_DELTA_RANGE,
    min_open_interest: int = DEFAULT_MIN_OPEN_INTEREST,
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
    min_bid: float = DEFAULT_MIN_BID,
    min_yield_pct: float = DEFAULT_MIN_YIELD_PCT,
    earnings_before: str | None = None,
    as_of: date | None = None,
) -> pd.DataFrame:
    """Rank cash-secured put candidates by annualized return on collateral.

    earnings_before: an expiration_date (YYYY-MM-DD) cutoff; contracts expiring
    on or after that date are dropped, since we don't hold naked CSPs through
    an earnings report in the v1 base case.
    min_yield_pct: minimum credit as a percentage of collateral (e.g. 1.0 means
    the credit must be at least 1% of the cash-secured collateral) -- the floor
    CLAUDE_OPTIONS.md Step 3 sets so a technically-in-band contract still has
    to pay enough to be worth tying up the collateral.
    """
    puts = contracts[contracts["type"] == "put"].copy()
    puts = puts[puts["strike"] < underlying_price]
    puts["dte"] = puts["expiration_date"].apply(lambda d: days_to_expiration(d, as_of))
    puts = puts[(puts["dte"] >= dte_range[0]) & (puts["dte"] <= dte_range[1])]
    puts["abs_delta"] = puts["delta"].abs()
    puts = puts[(puts["abs_delta"] >= delta_range[0]) & (puts["abs_delta"] <= delta_range[1])]
    puts = _liquidity_filter(puts, min_open_interest, max_spread_pct, min_bid)
    if earnings_before:
        puts = puts[puts["expiration_date"] < earnings_before]

    puts["credit"] = puts["mid"]
    puts["collateral"] = puts["strike"] * 100
    puts["return_on_collateral_pct"] = (puts["credit"] * 100 / puts["collateral"] * 100).round(3)
    puts = puts[puts["return_on_collateral_pct"] >= min_yield_pct]
    puts["annualized_roc_pct"] = (puts["return_on_collateral_pct"] / puts["dte"].clip(lower=1) * 365).round(1)

    # Net-capital view (a third source's convention): cash actually tied up
    # is collateral minus the credit received, since that credit is yours to
    # keep regardless of outcome. Logged alongside return_on_collateral_pct
    # (gross) rather than replacing it -- both are legitimate, and comparing
    # them is more useful than picking one.
    puts["net_cash_required"] = puts["collateral"] - puts["credit"] * 100
    puts["return_on_net_capital_pct"] = (
        puts["credit"] * 100 / puts["net_cash_required"].clip(lower=0.01) * 100
    ).round(3)

    return puts.sort_values("annualized_roc_pct", ascending=False).reset_index(drop=True)


def screen_covered_call(
    contracts: pd.DataFrame,
    cost_basis: float,
    *,
    dte_range: tuple[int, int] = DEFAULT_DTE_RANGE,
    delta_range: tuple[float, float] = DEFAULT_DELTA_RANGE,
    min_open_interest: int = DEFAULT_MIN_OPEN_INTEREST,
    max_spread_pct: float = DEFAULT_MAX_SPREAD_PCT,
    min_bid: float = DEFAULT_MIN_BID,
    min_yield_pct: float = DEFAULT_MIN_YIELD_PCT,
    earnings_before: str | None = None,
    as_of: date | None = None,
) -> pd.DataFrame:
    """Rank covered call candidates against assigned shares.

    Never sells a strike below cost_basis, per CLAUDE_OPTIONS.md Step 5.
    min_yield_pct: minimum credit as a percentage of cost basis, same floor as
    the CSP leg (CLAUDE_OPTIONS.md Step 5).
    """
    calls = contracts[contracts["type"] == "call"].copy()
    calls = calls[calls["strike"] >= cost_basis]
    calls["dte"] = calls["expiration_date"].apply(lambda d: days_to_expiration(d, as_of))
    calls = calls[(calls["dte"] >= dte_range[0]) & (calls["dte"] <= dte_range[1])]
    calls = calls[(calls["delta"] >= delta_range[0]) & (calls["delta"] <= delta_range[1])]
    calls = _liquidity_filter(calls, min_open_interest, max_spread_pct, min_bid)
    if earnings_before:
        calls = calls[calls["expiration_date"] < earnings_before]

    calls["credit"] = calls["mid"]
    calls["static_return_pct"] = (calls["credit"] * 100 / (cost_basis * 100) * 100).round(3)
    calls = calls[calls["static_return_pct"] >= min_yield_pct]
    return calls.sort_values("delta").reset_index(drop=True)
