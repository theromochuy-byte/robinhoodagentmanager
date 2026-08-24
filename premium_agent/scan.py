"""End-to-end runner: manage already-open legs, quality-screen the universe,
screen option chains for CSP / covered-call candidates, and propose
sizing-respecting trades to the paper ledger. Mirrors swing_agent.backtest's
role as "the end-to-end runner" -- pure Python, no network I/O, operating on
data the agent has already fetched and saved to disk. See README's "Options
premium-collection agent" section for the exact fetch procedure
(get_option_chains -> get_option_instruments -> get_option_quotes -> merge ->
save).

Expected on-disk inputs:
  data/options_config.json          -- sizing caps, delta/DTE targets, quality_screen
  data/options_universe.txt         -- candidate symbols, one or more per line, '#' comments
  data/options/universe_snapshot.json -- per symbol: {fundamentals, price,
                                          financials_annual, next_earnings_date}
                                          (get_equity_fundamentals / get_equity_quotes /
                                          get_financials / get_earnings_calendar)
  data/<SYMBOL>_day.json            -- daily bars (swing agent's existing fetch), for trend
  data/options/<SYMBOL>_<EXPIRY>.json -- merged option contract+quote records (dataio.py)
  data/options/open_trade_quotes.json -- current quotes for every instrument_id
                                          already open in the ledger (manage.py's input)
  data/options_positions.json       -- current "shares owned" lots (positions.py)
  data/paper_options_ledger.json    -- trade history / current open trades (ledger.py)
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from premium_agent import dataio, ledger, manage, positions, quality_screen, screener, trend

DATA = Path("data")
CONFIG_PATH = DATA / "options_config.json"
UNIVERSE_PATH = DATA / "options_universe.txt"
SNAPSHOT_PATH = DATA / "options" / "universe_snapshot.json"
OPTIONS_DIR = DATA / "options"
OPEN_TRADE_QUOTES_PATH = DATA / "options" / "open_trade_quotes.json"


def load_config(path: str | Path = CONFIG_PATH) -> dict:
    return json.loads(Path(path).read_text())


def load_universe(path: str | Path = UNIVERSE_PATH) -> list[str]:
    symbols: list[str] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        symbols.extend(line.split())
    return symbols


def load_snapshot(path: str | Path = SNAPSHOT_PATH) -> dict:
    if not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text())


def load_open_trade_quotes(path: str | Path = OPEN_TRADE_QUOTES_PATH) -> dict:
    if not Path(path).exists():
        return {}
    return json.loads(Path(path).read_text())


def load_symbol_contracts(symbol: str, options_dir: str | Path = OPTIONS_DIR) -> pd.DataFrame:
    return dataio.load_symbol_contracts(symbol, options_dir)


def screen_step1(
    symbol: str,
    entry: dict,
    config: dict,
    *,
    for_covered_call: bool = False,
) -> dict:
    """Step 1 gate for one symbol: quality, growth, and trend (scope differs by
    leg per CLAUDE_OPTIONS.md's resolved decision -- hard gate for covered
    calls, advisory/logged-only for CSPs).
    """
    qs_cfg = config["quality_screen"]
    fundamentals = entry.get("fundamentals", {})
    price = entry.get("price")
    if price is None:
        return {"symbol": symbol, "pass": False, "reasons": ["no price in snapshot"]}

    quality = quality_screen.screen_quality(
        fundamentals,
        price,
        min_avg_volume=qs_cfg["min_avg_volume_30d"],
        min_price=qs_cfg["min_price"],
        max_pe_ratio=qs_cfg["max_pe_ratio"],
        max_pct_of_52wk_high=qs_cfg["max_pct_of_52wk_high"],
    )

    trend_result = trend.above_moving_averages(
        symbol, data_dir=DATA, periods=tuple(qs_cfg["trend_sma_periods"])
    )
    trend_is_hard = for_covered_call and "covered_call" in qs_cfg.get("trend_gate_hard_for", [])

    growth_pct = quality_screen.trailing_eps_growth_pct(entry.get("financials_annual", []))
    min_growth = qs_cfg["min_trailing_eps_growth_pct"]
    # Missing growth data doesn't fail the gate -- same "skip when not
    # applicable" convention as the P/E check; we don't invent data.
    growth_fails = growth_pct is not None and growth_pct < min_growth

    reasons = list(quality["reasons"])
    if trend_is_hard and not trend_result["pass"]:
        reasons += trend_result["reasons"]
    if growth_fails:
        reasons.append(f"trailing_eps_growth_pct {growth_pct} < {min_growth}")

    return {
        "symbol": symbol,
        "pass": len(reasons) == 0,
        "reasons": reasons,
        "quality": quality,
        "trend": trend_result,
        "trend_advisory_only": not trend_is_hard,
        "trailing_eps_growth_pct": growth_pct,
        "price": price,
    }


def _account_state(ledger_path: str | Path, positions_path: str | Path) -> tuple[bool, bool]:
    """(has_assignment, has_open_csp) -- the two independent triggers the rest
    of this module keys off of. has_assignment (any shares held at all, in
    any lot) switches on the post-assignment budget/concurrency expansion and
    the supplemental-CSP posture. has_open_csp (any CSP leg still open,
    un-assigned) blocks opening a fresh barbell pair until the current one has
    fully resolved -- profit-take, expiry, or assignment on both legs."""
    has_assignment = len(positions.open_positions(positions_path)) > 0
    has_open_csp = any(t.get("type") == "csp" for t in ledger.open_trades(ledger_path))
    return has_assignment, has_open_csp


def _remaining_budget(
    config: dict, symbol: str, ledger_path: str | Path, positions_path: str | Path
) -> tuple[float, float]:
    """Remaining room under the total and per-symbol caps. "Capital in use"
    is CSP collateral plus the capital tied up in any assigned shares
    (positions.capital_in_use) -- an assignment ties up real money even
    though the resulting covered call needs no fresh collateral, and that
    was previously invisible here.

    Post-assignment budget expansion (2026-08-19 sign-off): once any lot is
    held, the total cap widens from max_collateral_pct_of_equity (50%) to
    post_assignment_max_collateral_pct_of_equity (100% -- i.e. total_equity
    minus capital already in use) and the per-name sub-cap tightens from 50%
    to post_assignment_max_single_name_pct_of_equity (45%). Before any
    assignment, the original caps apply unchanged.
    """
    equity = config["starting_equity"]
    has_assignment = len(positions.open_positions(positions_path)) > 0
    if has_assignment:
        total_pct = config.get("post_assignment_max_collateral_pct_of_equity", config["max_collateral_pct_of_equity"])
        symbol_pct = config.get("post_assignment_max_single_name_pct_of_equity", config["max_single_name_pct_of_equity"])
    else:
        total_pct = config["max_collateral_pct_of_equity"]
        symbol_pct = config["max_single_name_pct_of_equity"]
    total_cap = equity * total_pct
    symbol_cap = equity * symbol_pct
    used_total = ledger.deployed_collateral(path=ledger_path) + positions.capital_in_use(positions_path)
    used_symbol = ledger.deployed_collateral(path=ledger_path, symbol=symbol) + positions.capital_in_use(
        positions_path, symbol=symbol
    )
    return max(0.0, total_cap - used_total), max(0.0, symbol_cap - used_symbol)


def _effective_max_concurrent(config: dict, has_assignment: bool) -> int:
    if has_assignment:
        return config.get("post_assignment_max_concurrent_positions", config["max_concurrent_positions"])
    return config["max_concurrent_positions"]


def _screen_barbell_leg(
    leg_name: str,
    delta_range: tuple[float, float],
    leg_budget: float,
    config: dict,
    universe: list[str],
    snapshot: dict,
    options_dir: str | Path,
    ledger_path: str | Path,
    positions_path: str | Path,
    as_of: date | None,
    held: set[str],
    used_instrument_ids: set[str],
    committed_by_symbol: dict[str, float],
) -> tuple[tuple[str, "pd.Series", dict] | None, str | None]:
    """Screen the universe for one barbell leg -- first candidate that clears
    delta/DTE/liquidity/yield/collateral-budget, universe order, same
    convention the rest of this module uses. Shared by _open_barbell (both
    legs together, budget split 50/50) and _retry_barbell_leg (the one leg
    that never filled, screened again on a later cycle against whatever
    budget remains then). Returns (picked, None) or (None, skip_reason).
    """
    for symbol in universe:
        if symbol in held:
            continue
        entry = snapshot.get(symbol)
        if entry is None:
            continue
        gate = screen_step1(symbol, entry, config, for_covered_call=False)
        if not gate["pass"]:
            continue
        contracts = load_symbol_contracts(symbol, options_dir)
        if contracts.empty:
            continue
        _, remaining_symbol = _remaining_budget(config, symbol, ledger_path, positions_path)
        remaining_symbol -= committed_by_symbol.get(symbol, 0.0)
        if remaining_symbol <= 0:
            continue
        budget_here = min(leg_budget, remaining_symbol)

        candidates = screener.screen_csp(
            contracts, entry["price"],
            dte_range=tuple(config["dte_range"]), delta_range=delta_range,
            min_open_interest=config["min_open_interest"], max_spread_pct=config["max_spread_pct"],
            min_bid=config["min_bid"], min_yield_pct=config["min_yield_pct"],
            earnings_before=entry.get("next_earnings_date"), as_of=as_of,
        )
        candidates = candidates[~candidates["instrument_id"].isin(used_instrument_ids)]
        candidates = candidates[candidates["collateral"] <= budget_here]
        if candidates.empty:
            continue
        return (symbol, candidates.iloc[0], gate), None

    return None, "no candidate cleared delta/DTE/liquidity/yield/collateral-budget for this leg"


def _barbell_trade(leg_name: str, episode_id: str, picked: tuple) -> dict:
    symbol, top, gate = picked
    return {
        "symbol": symbol, "type": "csp", "instrument_id": top["instrument_id"],
        "strike": float(top["strike"]), "expiration_date": top["expiration_date"],
        "dte": int(top["dte"]), "delta": float(top["abs_delta"]), "credit": float(top["credit"]),
        "collateral": float(top["collateral"]),
        "supplemental": False,
        "barbell_leg": leg_name,
        "barbell_episode_id": episode_id,
        "return_on_collateral_pct": float(top["return_on_collateral_pct"]),
        "return_on_net_capital_pct": float(top["return_on_net_capital_pct"]),
        "annualized_roc_pct": float(top["annualized_roc_pct"]),
        "chance_of_profit_short": top.get("chance_of_profit_short"),
        "step1_gate": gate,
    }


def _open_barbell(
    config: dict,
    universe: list[str],
    snapshot: dict,
    options_dir: str | Path,
    ledger_path: str | Path,
    positions_path: str | Path,
    as_of: date | None,
    dry_run: bool,
) -> tuple[list[dict], dict[str, list[str]]]:
    """Open the account's two initial CSP legs together, splitting the
    remaining total collateral budget 50/50 (2026-08-19 barbell sign-off):
    - "threshold_of_risk" leg: config["delta_range"] (0.15-0.30), the
      existing primary CSP band -- assignment here is an accepted entry
      point into a payback cycle, not something to screen against.
    - "low_prob" leg: config["secondary_csp_delta_range"] (0.10-0.20).

    Whichever leg assigns first (either one -- order doesn't matter) starts
    its own independent payback counter on the resulting lot; the other leg
    keeps running its own CSP lifecycle (profit-take/roll/assign/expire)
    untouched by the first leg's fate. A fresh barbell only opens again once
    both legs of the current one have left the "open CSP" state (closed,
    expired, or assigned) -- see _account_state's has_open_csp.

    Both legs share one barbell_episode_id (a fresh UTC timestamp, generated
    once per call), written onto whichever leg(s) actually fill. If one leg
    fills and the other doesn't, _active_barbell_episode/_retry_barbell_leg
    (2026-08-24 sign-off) use that shared id to keep retrying just the
    missing leg on later cycles, instead of leaving its share of the budget
    idle until the filled leg eventually resolves -- see those functions'
    docstrings and CLAUDE_OPTIONS.md's barbell section for the full
    reasoning.

    Universe order (not cross-universe ranking) decides which symbol fills
    each leg, same "first candidate that clears everything" convention the
    rest of this module uses -- see propose_candidates.
    """
    remaining_total, _ = _remaining_budget(config, "", ledger_path, positions_path)
    if remaining_total <= 0:
        return [], {"barbell": ["no collateral budget remaining to open a barbell"]}

    leg_budget = remaining_total / 2
    proposed: list[dict] = []
    skipped: dict[str, list[str]] = {}
    committed_by_symbol: dict[str, float] = {}
    used_instrument_ids: set[str] = set()
    held = positions.held_symbols(positions_path)
    episode_id = datetime.now(timezone.utc).isoformat()

    legs = [
        ("threshold_of_risk", tuple(config["delta_range"])),
        ("low_prob", tuple(config["secondary_csp_delta_range"])),
    ]
    for leg_name, delta_range in legs:
        picked, skip_reason = _screen_barbell_leg(
            leg_name, delta_range, leg_budget, config, universe, snapshot,
            options_dir, ledger_path, positions_path, as_of,
            held, used_instrument_ids, committed_by_symbol,
        )
        if picked is None:
            skipped[f"barbell:{leg_name}"] = [skip_reason]
            continue

        trade = _barbell_trade(leg_name, episode_id, picked)
        if not dry_run:
            ledger.propose_trade(trade, path=ledger_path)
        proposed.append(trade)
        committed_by_symbol[trade["symbol"]] = committed_by_symbol.get(trade["symbol"], 0.0) + trade["collateral"]
        used_instrument_ids.add(trade["instrument_id"])

    return proposed, skipped


def _active_barbell_episode(ledger_path: str | Path) -> tuple[str | None, str | None]:
    """Find a barbell episode with exactly one leg filled and that leg still
    open -- the retry-eligible window (2026-08-24 sign-off, see
    CLAUDE_OPTIONS.md's barbell section). Once the filled leg itself resolves
    (closes, assigns, or expires), the episode is done and this stops
    returning it -- not because the missing leg's budget no longer matters,
    but because whatever happens next already re-attempts a low_prob-shaped
    CSP through an existing path: a resolved-clean sibling flips
    has_open_csp back to False, which lets a fresh _open_barbell run (a new
    low_prob attempt is part of that); an assigned sibling flips
    has_assignment True, which unlocks propose_candidates' supplemental-CSP
    screening (same secondary_csp_delta_range band). Retrying here as well
    would just duplicate one of those two paths.

    Returns (episode_id, missing_leg_name), or (None, None) if no episode is
    currently retry-eligible.
    """
    episodes: dict[str, dict[str, dict]] = {}
    for t in ledger.all_trades(ledger_path):
        eid = t.get("barbell_episode_id")
        leg = t.get("barbell_leg")
        if eid is None or leg is None:
            continue
        episodes.setdefault(eid, {})[leg] = t

    leg_names = {"threshold_of_risk", "low_prob"}
    for episode_id, legs in episodes.items():
        missing = leg_names - set(legs.keys())
        if len(missing) != 1:
            continue  # both legs filled, or (shouldn't happen) neither
        (filled_leg_name, filled_trade), = legs.items()
        if filled_trade.get("status") in ledger.CLOSED_STATUSES:
            continue  # sibling already resolved -- episode is done, see docstring
        return episode_id, next(iter(missing))

    return None, None


def _retry_barbell_leg(
    episode_id: str,
    leg_name: str,
    config: dict,
    universe: list[str],
    snapshot: dict,
    options_dir: str | Path,
    ledger_path: str | Path,
    positions_path: str | Path,
    as_of: date | None,
    dry_run: bool,
) -> tuple[list[dict], dict[str, list[str]]]:
    """Retry screening for the one barbell leg that never filled (see
    _active_barbell_episode), tagged with the episode's original id so it's
    still attributable to the same barbell for performance review. Sized
    against the full current _remaining_budget, not split in half again --
    nothing else is competing for it in this call, since the sibling leg's
    collateral is already deployed and counted against that budget -- and
    recomputed fresh each cycle rather than reusing the leg_budget from
    whenever the barbell first opened, consistent with how every other
    sizing check in this module re-derives state each cycle rather than
    caching it (2026-08-24 sign-off).
    """
    delta_range = tuple(
        config["delta_range"] if leg_name == "threshold_of_risk" else config["secondary_csp_delta_range"]
    )
    remaining_total, _ = _remaining_budget(config, "", ledger_path, positions_path)
    if remaining_total <= 0:
        return [], {f"barbell_retry:{leg_name}": ["no collateral budget remaining to retry this leg"]}

    held = positions.held_symbols(positions_path)
    picked, skip_reason = _screen_barbell_leg(
        leg_name, delta_range, remaining_total, config, universe, snapshot,
        options_dir, ledger_path, positions_path, as_of, held, set(), {},
    )
    if picked is None:
        return [], {f"barbell_retry:{leg_name}": [skip_reason]}

    trade = _barbell_trade(leg_name, episode_id, picked)
    if not dry_run:
        ledger.propose_trade(trade, path=ledger_path)
    return [trade], {}


def propose_candidates(
    config: dict | None = None,
    universe: list[str] | None = None,
    snapshot: dict | None = None,
    *,
    as_of: date | None = None,
    ledger_path: str | Path = ledger.DEFAULT_LEDGER,
    positions_path: str | Path = positions.DEFAULT_POSITIONS,
    options_dir: str | Path = OPTIONS_DIR,
    dry_run: bool = False,
) -> dict:
    """Screen the universe end to end and propose sizing-respecting trades.

    Held symbols (positions.py) are screened for covered calls (Step 5) --
    one proposal per open lot, since a symbol can hold more than one lot at
    once. Everything else is screened for cash-secured puts: if no CSP leg
    is currently open anywhere (_account_state's has_open_csp), a fresh
    barbell pair opens first (_open_barbell); if a barbell is already
    running but one of its two legs never filled, this retries just that
    missing leg (_active_barbell_episode / _retry_barbell_leg, 2026-08-24
    sign-off) instead of leaving its budget idle until the filled leg
    resolves. Otherwise, once a barbell is fully running (or has no
    retry-eligible leg left), unheld symbols are screened one at a time for
    supplemental low-probability CSPs -- but only once at least one
    assignment exists (has_assignment), same "opportunistic income on
    leftover equity" posture as before the barbell change. dry_run=True
    screens and ranks without writing to the ledger.
    """
    config = config or load_config()
    universe = universe if universe is not None else load_universe()
    snapshot = snapshot if snapshot is not None else load_snapshot()
    held = positions.held_symbols(positions_path)

    proposed: list[dict] = []
    skipped: dict[str, list[str]] = {}
    open_count = len(ledger.open_trades(ledger_path))
    has_assignment, has_open_csp = _account_state(ledger_path, positions_path)
    max_positions = _effective_max_concurrent(config, has_assignment)

    barbell_symbols: set[str] = set()
    if not has_open_csp and open_count < max_positions:
        barbell_trades, barbell_skipped = _open_barbell(
            config, universe, snapshot, options_dir, ledger_path, positions_path, as_of, dry_run
        )
        proposed.extend(barbell_trades)
        skipped.update(barbell_skipped)
        barbell_symbols = {t["symbol"] for t in barbell_trades}
        open_count += len(barbell_trades)
        has_assignment, has_open_csp = _account_state(ledger_path, positions_path)
        max_positions = _effective_max_concurrent(config, has_assignment)
    else:
        episode_id, missing_leg = _active_barbell_episode(ledger_path)
        if missing_leg is not None and open_count < max_positions:
            retry_trades, retry_skipped = _retry_barbell_leg(
                episode_id, missing_leg, config, universe, snapshot,
                options_dir, ledger_path, positions_path, as_of, dry_run,
            )
            proposed.extend(retry_trades)
            skipped.update(retry_skipped)
            barbell_symbols = {t["symbol"] for t in retry_trades}
            open_count += len(retry_trades)
            has_assignment, has_open_csp = _account_state(ledger_path, positions_path)
            max_positions = _effective_max_concurrent(config, has_assignment)

    for symbol in universe:
        if symbol in barbell_symbols:
            # Already proposed above as one of the two barbell legs.
            continue
        if open_count >= max_positions:
            skipped[symbol] = [f"max_concurrent_positions ({max_positions}) reached"]
            continue

        entry = snapshot.get(symbol)
        if entry is None:
            skipped[symbol] = ["no snapshot data (fundamentals/price/financials/earnings)"]
            continue

        lots = positions.lots_for_symbol(symbol, positions_path)
        if lots:
            gate = screen_step1(symbol, entry, config, for_covered_call=True)
            if not gate["pass"]:
                skipped[symbol] = gate["reasons"]
                continue
            contracts = load_symbol_contracts(symbol, options_dir)
            if contracts.empty:
                skipped[symbol] = [f"no option contract data in {options_dir}"]
                continue
            earnings_before = entry.get("next_earnings_date")
            reasons = []
            for lot in lots:
                if open_count >= max_positions:
                    reasons.append(f"lot {lot['source_instrument_id']}: max_concurrent_positions ({max_positions}) reached")
                    break
                candidates = screener.screen_covered_call(
                    contracts, lot["cost_basis"],
                    dte_range=tuple(config["dte_range"]),
                    delta_range=tuple(config.get("covered_call_delta_range", screener.DEFAULT_COVERED_CALL_DELTA_RANGE)),
                    min_open_interest=config["min_open_interest"],
                    max_spread_pct=config["max_spread_pct"],
                    min_bid=config["min_bid"],
                    min_yield_pct=config["min_yield_pct"],
                    earnings_before=earnings_before,
                    as_of=as_of,
                )
                if candidates.empty:
                    reasons.append(f"lot {lot['source_instrument_id']}: no covered-call candidate cleared delta/DTE/liquidity/yield")
                    continue
                top = candidates.iloc[0]
                trade = {
                    "symbol": symbol, "type": "covered_call", "instrument_id": top["instrument_id"],
                    "strike": float(top["strike"]), "expiration_date": top["expiration_date"],
                    "dte": int(top["dte"]), "delta": float(top["delta"]), "credit": float(top["credit"]),
                    "static_return_pct": float(top["static_return_pct"]),
                    "chance_of_profit_short": top.get("chance_of_profit_short"),
                    "source_instrument_id": lot["source_instrument_id"],
                    "paid_off": positions.is_paid_off(lot["source_instrument_id"], ledger_path, positions_path),
                    "step1_gate": gate,
                }
                if not dry_run:
                    ledger.propose_trade(trade, path=ledger_path)
                proposed.append(trade)
                open_count += 1
            if reasons:
                skipped[symbol] = reasons
            continue

        if not has_assignment:
            # Barbell already handled the no-assignment opening move above;
            # no supplemental CSPs without an assignment. Still run Step 1
            # so the skip report shows the real reason for every symbol,
            # same visibility the pre-barbell report always had.
            gate = screen_step1(symbol, entry, config, for_covered_call=False)
            skipped[symbol] = gate["reasons"] if not gate["pass"] else [
                "no assignment yet -- supplemental CSPs only open once a lot is held"
            ]
            continue

        # Supplemental CSP: something is already held somewhere, so leftover
        # equity under the (expanded, post-assignment) cap gets put to work,
        # low-probability-of-assignment band, opportunistic income only.
        contracts = load_symbol_contracts(symbol, options_dir)
        if contracts.empty:
            skipped[symbol] = [f"no option contract data in {options_dir}"]
            continue
        gate = screen_step1(symbol, entry, config, for_covered_call=False)
        if not gate["pass"]:
            skipped[symbol] = gate["reasons"]
            continue
        earnings_before = entry.get("next_earnings_date")
        csp_delta_range = tuple(config["secondary_csp_delta_range"])
        remaining_total, remaining_symbol = _remaining_budget(config, symbol, ledger_path, positions_path)
        candidates = screener.screen_csp(
            contracts,
            entry["price"],
            dte_range=tuple(config["dte_range"]),
            delta_range=csp_delta_range,
            min_open_interest=config["min_open_interest"],
            max_spread_pct=config["max_spread_pct"],
            min_bid=config["min_bid"],
            min_yield_pct=config["min_yield_pct"],
            earnings_before=earnings_before,
            as_of=as_of,
        )
        candidates = candidates[
            (candidates["collateral"] <= remaining_total) & (candidates["collateral"] <= remaining_symbol)
        ]
        if candidates.empty:
            skipped[symbol] = ["no supplemental CSP candidate cleared delta/DTE/liquidity/yield/collateral-budget"]
            continue
        top = candidates.iloc[0]
        trade = {
            "symbol": symbol, "type": "csp", "instrument_id": top["instrument_id"],
            "strike": float(top["strike"]), "expiration_date": top["expiration_date"],
            "dte": int(top["dte"]), "delta": float(top["abs_delta"]), "credit": float(top["credit"]),
            "collateral": float(top["collateral"]),
            "supplemental": True,
            "return_on_collateral_pct": float(top["return_on_collateral_pct"]),
            "return_on_net_capital_pct": float(top["return_on_net_capital_pct"]),
            "annualized_roc_pct": float(top["annualized_roc_pct"]),
            "chance_of_profit_short": top.get("chance_of_profit_short"),
            "step1_gate": gate,
        }
        if not dry_run:
            ledger.propose_trade(trade, path=ledger_path)
        proposed.append(trade)
        open_count += 1

    return {"proposed": proposed, "skipped": skipped}


def write_report(
    result: dict,
    config: dict,
    ledger_path: str | Path = ledger.DEFAULT_LEDGER,
    positions_path: str | Path = positions.DEFAULT_POSITIONS,
    as_of: date | None = None,
    management_actions: list[dict] | None = None,
) -> Path:
    """Save today's scan result to reports/options/scan_<date>.json, mirroring
    swing_agent.scanner's reports/scan_<date>.json convention -- this is what
    premium_agent.notify reads to build the email digest, decoupled from the
    ledger so the email doesn't need to re-derive "what happened today."
    """
    today = str(as_of or date.today())
    reports_dir = Path("reports") / "options"
    reports_dir.mkdir(parents=True, exist_ok=True)

    equity = config["starting_equity"]
    collateral = ledger.deployed_collateral(path=ledger_path)
    shares_capital = positions.capital_in_use(positions_path)
    capital_used = collateral + shares_capital
    has_assignment = len(positions.open_positions(positions_path)) > 0
    total_pct = config.get("post_assignment_max_collateral_pct_of_equity", config["max_collateral_pct_of_equity"]) \
        if has_assignment else config["max_collateral_pct_of_equity"]
    lots = positions.open_positions(positions_path)
    report = {
        "scan_date": today,
        "management_actions": management_actions or [],
        "proposed": result["proposed"],
        "skipped": result["skipped"],
        "open_positions": len(ledger.open_trades(ledger_path)),
        "lots": [
            {
                "symbol": lot["symbol"],
                "source_instrument_id": lot["source_instrument_id"],
                "cost_basis": lot["cost_basis"],
                "breakeven_progress_pct": positions.breakeven_progress_pct(
                    lot["source_instrument_id"], ledger_path, positions_path
                ),
                "paid_off": positions.is_paid_off(lot["source_instrument_id"], ledger_path, positions_path),
            }
            for lot in lots
        ],
        "equity": {
            "starting_equity": equity,
            "csp_collateral": collateral,
            "assigned_shares_capital": shares_capital,
            "capital_in_use": round(capital_used, 2),
            "available_capital": round(equity * total_pct - capital_used, 2),
        },
    }
    out = reports_dir / f"scan_{today}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    return out


def run_daily_cycle(config: dict | None = None, *, as_of: date | None = None, dry_run: bool = False) -> dict:
    """Manage every already-open leg (manage.simulate_management), then
    propose new candidates (propose_candidates), then write the report --
    the full daily sequence, in the order CLAUDE_OPTIONS.md Step 4 (manage
    what's open) has to happen before Step 1-3/5 (screen for what's new),
    since assignment/expiry during management changes which symbols are held.
    """
    config = config or load_config()
    snapshot = load_snapshot()
    quotes = load_open_trade_quotes()

    mgmt_actions: list[dict] = []
    open_trades_today = ledger.open_trades()
    if open_trades_today and not quotes:
        print(
            f"Warning: {len(open_trades_today)} open trade(s) exist but "
            f"{OPEN_TRADE_QUOTES_PATH} is missing -- skipping management "
            f"simulation (profit-take/roll/assignment) this cycle.",
            file=sys.stderr,
        )
    elif open_trades_today:
        mgmt_result = manage.simulate_management(config, snapshot, quotes, OPTIONS_DIR, as_of=as_of, dry_run=dry_run)
        mgmt_actions = mgmt_result["actions"]

    result = propose_candidates(config=config, snapshot=snapshot, as_of=as_of, dry_run=dry_run)
    out = write_report(result, config, as_of=as_of, management_actions=mgmt_actions)
    return {"management_actions": mgmt_actions, **result, "report_path": out}


if __name__ == "__main__":
    cycle = run_daily_cycle()
    if cycle["management_actions"]:
        print(f"Management actions ({len(cycle['management_actions'])}):")
        for a in cycle["management_actions"]:
            extra = {k: v for k, v in a.items() if k not in ("symbol", "instrument_id", "action")}
            print(f"  {a['symbol']:6s} {a['action']:20s} {extra}")
    print(f"Proposed {len(cycle['proposed'])} trade(s):")
    for t in cycle["proposed"]:
        print(f"  {t['symbol']:6s} {t['type']:12s} strike={t['strike']:<8.2f} "
              f"exp={t['expiration_date']} credit={t['credit']:.2f}")
    print(f"Skipped {len(cycle['skipped'])} symbol(s):")
    for sym, reasons in cycle["skipped"].items():
        print(f"  {sym}: {'; '.join(reasons)}")
    print(f"Report written to {cycle['report_path']}")
