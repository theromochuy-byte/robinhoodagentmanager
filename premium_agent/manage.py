"""Simulate daily management of already-open CSP and covered-call legs:
profit-take, ITM rolls (net-credit only, capped at
max_rolls_before_assignment -- uncapped once a lot has reached zero cost
basis), worthless expiration, and assignment/call-away. This is
CLAUDE_OPTIONS.md Step 4 (CSP) and the equivalent covered-call rules under
Step 5, run once per cycle against every trade premium_agent.ledger.open_trades
returns -- before propose_candidates screens for anything new.

Pure Python, no network I/O, same convention as the rest of premium_agent:
operates on data the agent (in Claude Code) already fetched and saved this
cycle:
  - data/options/open_trade_quotes.json -- {instrument_id: {"mark_price": ...,
    "delta": ...}}, one entry per instrument_id currently open in the ledger
    (get_option_quotes on those ids).
  - data/options/universe_snapshot.json -- underlying prices (already fetched
    for Step 1; reused here for the ITM check instead of a second fetch).
  - data/options/<SYMBOL>_<EXPIRY>.json -- same merged chain+quote files
    Step 1-3/5 candidate screening reads, reused here to find roll targets
    at later expirations already fetched this cycle.

If a currently-open instrument_id has no entry in open_trade_quotes.json,
that leg is skipped (logged, not guessed) -- same "say so and skip" rule as
everywhere else in this project.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from premium_agent import dataio, ledger, positions, screener

DEFAULT_QUOTES_PATH = Path("data/options/open_trade_quotes.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _find_roll_candidate(
    trade: dict,
    config: dict,
    options_dir: str | Path,
    as_of: date | None,
    *,
    is_call: bool,
    cost_basis: float | None = None,
    underlying_price: float | None = None,
    delta_range: tuple[float, float],
    used_instrument_ids: set[str],
):
    """Best same-symbol, later-expiration, net-credit roll target already
    fetched this cycle -- or None if none clears the same delta/DTE/liquidity/
    yield gates screen_csp/screen_covered_call apply to a fresh entry. Only
    considers expirations strictly later than the trade being rolled, since
    the whole point of a roll is buying more time."""
    contracts = dataio.load_symbol_contracts(trade["symbol"], options_dir)
    if contracts.empty:
        return None
    contracts = contracts[contracts["expiration_date"] > trade["expiration_date"]]
    contracts = contracts[~contracts["instrument_id"].isin(used_instrument_ids)]
    if contracts.empty:
        return None

    if is_call:
        candidates = screener.screen_covered_call(
            contracts, cost_basis,
            dte_range=tuple(config["dte_range"]), delta_range=delta_range,
            min_open_interest=config["min_open_interest"], max_spread_pct=config["max_spread_pct"],
            min_bid=config["min_bid"], min_yield_pct=config["min_yield_pct"], as_of=as_of,
        )
    else:
        candidates = screener.screen_csp(
            contracts, underlying_price,
            dte_range=tuple(config["dte_range"]), delta_range=delta_range,
            min_open_interest=config["min_open_interest"], max_spread_pct=config["max_spread_pct"],
            min_bid=config["min_bid"], min_yield_pct=config["min_yield_pct"], as_of=as_of,
            require_otm=False,
        )
    if candidates.empty:
        return None
    return candidates.iloc[0]


def credit_dividends(
    snapshot: dict,
    *,
    positions_path: str | Path = positions.DEFAULT_POSITIONS,
    as_of: date | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """TESTING (2026-09-04, DRIP conversation "Option A"): for every open
    lot, check its symbol's fundamentals in this cycle's universe_snapshot.json
    for a dividend whose payable_date has arrived (payable_date <= as_of) --
    if so, credit dividend_per_share * shares to that lot via
    positions.credit_dividend, which is itself idempotent per payable_date so
    running this every cycle never double-counts the same payment. Skips (and
    says so, same "say so and skip" rule as everywhere else) when the
    snapshot has no fundamentals for the lot's symbol, or when
    dividend_per_share/payable_date is missing -- never invents a payment
    that wasn't actually fetched. Not yet wired into CLAUDE_OPTIONS.md as a
    signed-off rule -- see positions.credit_dividend's docstring.
    """
    as_of = as_of or date.today()
    actions: list[dict] = []
    for lot in positions.open_positions(positions_path):
        symbol = lot["symbol"]
        source_iid = lot["source_instrument_id"]
        entry = snapshot.get(symbol)
        fundamentals = (entry or {}).get("fundamentals", {})
        payable_date = fundamentals.get("payable_date")
        dividend_per_share = fundamentals.get("dividend_per_share")

        if entry is None or payable_date is None or dividend_per_share is None:
            actions.append({"symbol": symbol, "source_instrument_id": source_iid,
                             "action": "skipped", "reason": "no dividend schedule in universe_snapshot.json"})
            continue

        try:
            payable = date.fromisoformat(payable_date)
        except ValueError:
            actions.append({"symbol": symbol, "source_instrument_id": source_iid,
                             "action": "skipped", "reason": f"unparseable payable_date {payable_date!r}"})
            continue

        if payable > as_of:
            actions.append({"symbol": symbol, "source_instrument_id": source_iid,
                             "action": "no_action", "reason": f"payable_date {payable_date} not reached yet"})
            continue

        amount = float(dividend_per_share) * lot["shares"]
        if dry_run:
            already = payable_date in lot.get("dividend_dates_credited", [])
            actions.append({"symbol": symbol, "source_instrument_id": source_iid,
                             "action": "skipped" if already else "credited_dividend",
                             "payable_date": payable_date, "amount": amount})
            continue

        credited = positions.credit_dividend(source_iid, payable_date, amount, path=positions_path)
        actions.append({"symbol": symbol, "source_instrument_id": source_iid,
                         "action": "credited_dividend" if credited else "skipped",
                         "reason": None if credited else f"payable_date {payable_date} already credited",
                         "payable_date": payable_date, "amount": amount if credited else None})
    return actions


def simulate_management(
    config: dict,
    snapshot: dict,
    quotes: dict,
    options_dir: str | Path,
    *,
    ledger_path: str | Path = ledger.DEFAULT_LEDGER,
    positions_path: str | Path = positions.DEFAULT_POSITIONS,
    as_of: date | None = None,
    dry_run: bool = False,
) -> dict:
    """Walk every open ledger trade and apply profit-take / roll / expire /
    assign per CLAUDE_OPTIONS.md Step 4. Returns {"actions": [...]} -- one
    entry per open trade, including "no_action" and "skipped" so a cycle's
    full management pass is visible on review, not just the trades that moved.
    """
    as_of = as_of or date.today()
    actions: list[dict] = []
    used_instrument_ids: set[str] = set()

    for trade in ledger.open_trades(ledger_path):
        iid = trade.get("instrument_id")
        symbol = trade["symbol"]
        quote = quotes.get(iid)
        entry = snapshot.get(symbol)
        used_instrument_ids.add(iid)

        if quote is None or quote.get("mark_price") is None:
            actions.append({"symbol": symbol, "instrument_id": iid, "action": "skipped",
                             "reason": "no current quote in open_trade_quotes.json"})
            continue
        if entry is None or entry.get("price") is None:
            actions.append({"symbol": symbol, "instrument_id": iid, "action": "skipped",
                             "reason": "no underlying price in universe_snapshot.json"})
            continue

        mark = float(quote["mark_price"])
        underlying_price = float(entry["price"])
        dte = screener.days_to_expiration(trade["expiration_date"], as_of)
        is_call = trade["type"] == "covered_call"
        is_itm = (underlying_price > trade["strike"]) if is_call else (underlying_price < trade["strike"])

        lot = positions.lot_by_source(trade.get("source_instrument_id"), positions_path) if is_call else None
        paid_off = (
            positions.is_paid_off(trade["source_instrument_id"], ledger_path, positions_path)
            if is_call and lot is not None else False
        )

        # 1. Profit-take: contract has decayed to profit_take_pct of the
        # credit received -- buy it back, keep the rest as realized gain.
        if mark <= config["profit_take_pct"] * trade["credit"]:
            if not dry_run:
                ledger.update_trade(iid, {
                    "status": "closed", "close_debit": mark, "closed_at": _now(),
                    "close_reason": "profit_take",
                }, path=ledger_path)
            actions.append({"symbol": symbol, "instrument_id": iid, "action": "closed_profit_take",
                             "mark_price": mark, "credit": trade["credit"]})
            continue

        # 2. ITM and inside the roll-decision window.
        if is_itm and dte <= config["roll_dte_trigger"]:
            rolls_so_far = ledger.roll_count(iid, ledger_path)
            roll_cap = None if paid_off else config["max_rolls_before_assignment"]
            under_cap = roll_cap is None or rolls_so_far < roll_cap

            candidate = None
            if under_cap and (not is_call or lot is not None):
                delta_range = (
                    tuple(config.get("covered_call_delta_range", screener.DEFAULT_COVERED_CALL_DELTA_RANGE))
                    if is_call else
                    tuple(config["secondary_csp_delta_range"] if trade.get("supplemental") else config["delta_range"])
                )
                candidate = _find_roll_candidate(
                    trade, config, options_dir, as_of, is_call=is_call,
                    cost_basis=lot["cost_basis"] if lot else None,
                    underlying_price=underlying_price, delta_range=delta_range,
                    used_instrument_ids=used_instrument_ids,
                )
                if candidate is not None and float(candidate["credit"]) <= mark:
                    candidate = None  # would be a debit roll -- CLAUDE_OPTIONS.md Step 4: net credit only

            if candidate is not None:
                new_trade = {
                    "symbol": symbol, "type": trade["type"], "instrument_id": candidate["instrument_id"],
                    "strike": float(candidate["strike"]), "expiration_date": candidate["expiration_date"],
                    "dte": int(candidate["dte"]),
                    "delta": float(candidate["abs_delta"] if not is_call else candidate["delta"]),
                    "credit": float(candidate["credit"]),
                    "supplemental": trade.get("supplemental", False),
                }
                if is_call:
                    new_trade["source_instrument_id"] = trade["source_instrument_id"]
                    new_trade["static_return_pct"] = float(candidate["static_return_pct"])
                else:
                    new_trade["collateral"] = float(candidate["collateral"])
                    new_trade["return_on_collateral_pct"] = float(candidate["return_on_collateral_pct"])
                    new_trade["return_on_net_capital_pct"] = float(candidate["return_on_net_capital_pct"])
                    new_trade["annualized_roc_pct"] = float(candidate["annualized_roc_pct"])
                if not dry_run:
                    ledger.roll_trade(iid, close_debit=mark, new_trade=new_trade, path=ledger_path)
                used_instrument_ids.add(new_trade["instrument_id"])
                actions.append({"symbol": symbol, "instrument_id": iid, "action": "rolled",
                                 "rolled_to": new_trade["instrument_id"], "net_credit": new_trade["credit"] - mark})
                continue

            # No net-credit roll available, or roll cap reached: accept assignment /
            # call-away. A paid-off lot only lands here if no net-credit roll
            # exists at all -- getting called away realizes the full gain rather
            # than being avoidable, which doesn't contradict "hold indefinitely."
            if is_call:
                if not dry_run:
                    ledger.update_trade(iid, {
                        "status": "called_away", "close_debit": mark, "closed_at": _now(),
                    }, path=ledger_path)
                    positions.remove_position(trade["source_instrument_id"], path=positions_path)
                actions.append({"symbol": symbol, "instrument_id": iid, "action": "called_away",
                                 "rolls_used": rolls_so_far, "paid_off": paid_off})
            else:
                cost_basis = trade["strike"] - ledger.cumulative_credit(iid, path=ledger_path)
                if not dry_run:
                    ledger.update_trade(iid, {"status": "assigned", "assigned_at": _now()}, path=ledger_path)
                    positions.add_position(
                        symbol, 100, cost_basis, source_instrument_id=iid,
                        strike_at_assignment=trade["strike"], path=positions_path,
                    )
                actions.append({"symbol": symbol, "instrument_id": iid, "action": "assigned",
                                 "cost_basis": cost_basis, "rolls_used": rolls_so_far})
            continue

        # 3. Expired worthless (not ITM, no DTE left) -- full credit kept, no shares.
        if dte <= 0 and not is_itm:
            if not dry_run:
                ledger.update_trade(iid, {
                    "status": "closed", "close_debit": 0.0, "closed_at": _now(),
                    "close_reason": "expired_otm",
                }, path=ledger_path)
            actions.append({"symbol": symbol, "instrument_id": iid, "action": "expired_otm",
                             "credit_kept": trade["credit"]})
            continue

        # 4. Nothing to do today.
        actions.append({"symbol": symbol, "instrument_id": iid, "action": "no_action",
                         "mark_price": mark, "dte": dte, "itm": is_itm})

    return {"actions": actions}
