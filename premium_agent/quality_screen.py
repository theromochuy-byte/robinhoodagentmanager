"""Step 1 candidate screen: is this a fundamentally sound, liquid stock trading
at a discount, that we'd genuinely want to own at the strike?

Thresholds are adapted from a wheel-strategy walkthrough the user supplied
(liquid, fairly-valued, pulled back from its highs) and mapped onto the
fields Robinhood's get_equity_fundamentals actually returns. The source used
a PEG ratio filter for growth-at-a-reasonable-price; this MCP doesn't expose
PEG, so a "within X% of the 52-week high" check stands in for it instead
-- see CLAUDE_OPTIONS.md Step 1 for the full rationale.

No network I/O: callers pass in the get_equity_fundamentals record for one
symbol plus the current price (from get_equity_quotes).
"""
from __future__ import annotations


def _float_or_none(v) -> float | None:
    if v in (None, ""):
        return None
    return float(v)


def screen_quality(
    fundamentals: dict,
    current_price: float,
    *,
    min_avg_volume: float = 2_000_000,
    min_price: float = 50.0,
    max_pe_ratio: float = 30.0,
    max_pct_of_52wk_high: float = 0.90,
) -> dict:
    """Returns {"pass": bool, "reasons": [...]} -- reasons lists every failed check."""
    reasons: list[str] = []

    avg_vol = _float_or_none(fundamentals.get("average_volume_30_days")) or _float_or_none(fundamentals.get("average_volume")) or 0.0
    if avg_vol < min_avg_volume:
        reasons.append(f"avg_volume_30d {avg_vol:,.0f} < {min_avg_volume:,.0f}")

    if current_price < min_price:
        reasons.append(f"price {current_price:.2f} < {min_price:.2f}")

    pe = _float_or_none(fundamentals.get("pe_ratio"))
    if pe is not None and pe > max_pe_ratio:
        reasons.append(f"pe_ratio {pe:.1f} > {max_pe_ratio}")

    high_52 = _float_or_none(fundamentals.get("high_52_weeks"))
    if high_52 is not None:
        ceiling = high_52 * max_pct_of_52wk_high
        if current_price > ceiling:
            reasons.append(
                f"price {current_price:.2f} > {max_pct_of_52wk_high:.0%} of 52wk high {high_52:.2f} (not 'on sale')"
            )

    return {
        "pass": len(reasons) == 0,
        "reasons": reasons,
        "pe_ratio": pe,
        "avg_volume_30d": avg_vol,
        "high_52_weeks": high_52,
    }


def trailing_eps_growth_pct(annual_financials: list[dict]) -> float | None:
    """YoY growth of the two most recent annual periods from get_financials.

    A second wheel-strategy source (same supplier) screens on *forward*
    EPS growth estimates (next year, next 5 years) alongside a trailing
    growth number. Robinhood's get_financials has no forward estimates and
    no per-share EPS at all -- only revenue/gross_profit/net_income/net_margin
    by period. This computes trailing net-income growth as the closest
    available proxy for the source's "recent EPS growth" check; it is NOT a
    stand-in for the forward-looking numbers, which this MCP cannot supply
    (see CLAUDE_OPTIONS.md Known gaps).

    annual_financials: the `financials` list for one symbol from
    get_financials(period="annual"), most-recent-first.
    """
    if len(annual_financials) < 2:
        return None
    latest = _float_or_none(annual_financials[0].get("net_income"))
    prior = _float_or_none(annual_financials[1].get("net_income"))
    if latest is None or not prior:
        return None
    return round((latest - prior) / abs(prior) * 100, 2)


def dividend_info(fundamentals: dict) -> dict:
    """Pulled in for full-cycle P&L: put credit + call credit + dividends + share gain/loss."""
    return {
        "dividend_yield": _float_or_none(fundamentals.get("dividend_yield")),
        "dividend_per_share": _float_or_none(fundamentals.get("dividend_per_share")),
        "distribution_frequency": fundamentals.get("distribution_frequency"),
        "ex_dividend_date": fundamentals.get("ex_dividend_date"),
        "payable_date": fundamentals.get("payable_date"),
    }
