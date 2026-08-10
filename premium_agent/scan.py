"""End-to-end runner: quality-screen the universe, screen option chains for
CSP / covered-call candidates, and propose sizing-respecting trades to the
paper ledger. Mirrors swing_agent.backtest's role as "the end-to-end
runner" -- pure Python, no network I/O, operating on data the agent has
already fetched and saved to disk. See README's "Options premium-collection
agent" section for the exact fetch procedure (get_option_chains ->
get_option_instruments -> get_option_quotes -> merge -> save).

Expected on-disk inputs:
  data/options_config.json          -- sizing caps, delta/DTE targets, quality_screen
  data/options_universe.txt         -- candidate symbols, one or more per line, '#' comments
  data/options/universe_snapshot.json -- per symbol: {fundamentals, price,
                                          financials_annual, next_earnings_date}
                                          (get_equity_fundamentals / get_equity_quotes /
                                          get_financials / get_earnings_calendar)
  data/<SYMBOL>_day.json            -- daily bars (swing agent's existing fetch), for trend
  data/options/<SYMBOL>_<EXPIRY>.json -- merged option contract+quote records (dataio.py)
  data/options_positions.json       -- current "shares owned" state (positions.py)
  data/paper_options_ledger.json    -- trade history / current open trades (ledger.py)
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from premium_agent import dataio, ledger, positions, quality_screen, screener, trend

DATA = Path("data")
CONFIG_PATH = DATA / "options_config.json"
UNIVERSE_PATH = DATA / "options_universe.txt"
SNAPSHOT_PATH = DATA / "options" / "universe_snapshot.json"
OPTIONS_DIR = DATA / "options"


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


def load_symbol_contracts(symbol: str, options_dir: str | Path = OPTIONS_DIR) -> pd.DataFrame:
    """Concatenate every data/options/<SYMBOL>_<EXPIRY>.json file for this symbol
    (one file per expiration, per dataio.py's documented convention)."""
    frames = [dataio.load(p) for p in sorted(Path(options_dir).glob(f"{symbol}_*.json"))]
    if not frames:
        return pd.DataFrame(
            columns=["instrument_id", "symbol", "expiration_date", "strike", "type",
                     "bid", "ask", "mid", "delta", "iv", "open_interest", "volume",
                     "chance_of_profit_short"]
        )
    return pd.concat(frames, ignore_index=True)


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


def _remaining_budget(config: dict, symbol: str, ledger_path: str | Path) -> tuple[float, float]:
    equity = config["starting_equity"]
    total_cap = equity * config["max_collateral_pct_of_equity"]
    symbol_cap = equity * config["max_single_name_pct_of_equity"]
    deployed_total = ledger.deployed_collateral(path=ledger_path)
    deployed_symbol = ledger.deployed_collateral(path=ledger_path, symbol=symbol)
    return max(0.0, total_cap - deployed_total), max(0.0, symbol_cap - deployed_symbol)


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

    Held symbols (positions.py) are screened for covered calls (Step 5);
    everything else is screened for cash-secured puts (Step 1-3). Candidates
    are proposed in ranked order until Step 6's collateral/position caps are
    exhausted. dry_run=True screens and ranks without writing to the ledger.
    """
    config = config or load_config()
    universe = universe if universe is not None else load_universe()
    snapshot = snapshot if snapshot is not None else load_snapshot()
    held = positions.held_symbols(positions_path)

    proposed: list[dict] = []
    skipped: dict[str, list[str]] = {}
    open_count = len(ledger.open_trades(ledger_path))
    max_positions = config["max_concurrent_positions"]

    for symbol in universe:
        if open_count >= max_positions:
            skipped[symbol] = [f"max_concurrent_positions ({max_positions}) reached"]
            continue

        is_held = symbol in held
        entry = snapshot.get(symbol)
        if entry is None:
            skipped[symbol] = ["no snapshot data (fundamentals/price/financials/earnings)"]
            continue

        gate = screen_step1(symbol, entry, config, for_covered_call=is_held)
        if not gate["pass"]:
            skipped[symbol] = gate["reasons"]
            continue

        contracts = load_symbol_contracts(symbol, options_dir)
        if contracts.empty:
            skipped[symbol] = [f"no option contract data in {options_dir}"]
            continue

        earnings_before = entry.get("next_earnings_date")

        if is_held:
            position = next(p for p in positions.open_positions(positions_path) if p["symbol"] == symbol)
            candidates = screener.screen_covered_call(
                contracts,
                position["cost_basis"],
                dte_range=tuple(config["dte_range"]),
                delta_range=tuple(config["delta_range"]),
                min_open_interest=config["min_open_interest"],
                max_spread_pct=config["max_spread_pct"],
                min_bid=config["min_bid"],
                min_yield_pct=config["min_yield_pct"],
                earnings_before=earnings_before,
                as_of=as_of,
            )
            if candidates.empty:
                skipped[symbol] = ["no covered-call candidate cleared delta/DTE/liquidity/yield"]
                continue
            top = candidates.iloc[0]
            trade = {
                "symbol": symbol, "type": "covered_call", "instrument_id": top["instrument_id"],
                "strike": float(top["strike"]), "expiration_date": top["expiration_date"],
                "dte": int(top["dte"]), "delta": float(top["delta"]), "credit": float(top["credit"]),
                "static_return_pct": float(top["static_return_pct"]),
                "chance_of_profit_short": top.get("chance_of_profit_short"),
                "step1_gate": gate,
            }
            if not dry_run:
                ledger.propose_trade(trade, path=ledger_path)
            proposed.append(trade)
            open_count += 1
            continue

        # CSP leg: respect Step 6 collateral caps before proposing.
        remaining_total, remaining_symbol = _remaining_budget(config, symbol, ledger_path)
        candidates = screener.screen_csp(
            contracts,
            entry["price"],
            dte_range=tuple(config["dte_range"]),
            delta_range=tuple(config["delta_range"]),
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
            skipped[symbol] = ["no CSP candidate cleared delta/DTE/liquidity/yield/collateral-budget"]
            continue
        top = candidates.iloc[0]
        trade = {
            "symbol": symbol, "type": "csp", "instrument_id": top["instrument_id"],
            "strike": float(top["strike"]), "expiration_date": top["expiration_date"],
            "dte": int(top["dte"]), "delta": float(top["abs_delta"]), "credit": float(top["credit"]),
            "collateral": float(top["collateral"]),
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


def write_report(result: dict, config: dict, ledger_path: str | Path = ledger.DEFAULT_LEDGER, as_of: date | None = None) -> Path:
    """Save today's scan result to reports/options/scan_<date>.json, mirroring
    swing_agent.scanner's reports/scan_<date>.json convention -- this is what
    premium_agent.notify reads to build the email digest, decoupled from the
    ledger so the email doesn't need to re-derive "what happened today."
    """
    today = str(as_of or date.today())
    reports_dir = Path("reports") / "options"
    reports_dir.mkdir(parents=True, exist_ok=True)

    equity = config["starting_equity"]
    deployed_total = ledger.deployed_collateral(path=ledger_path)
    report = {
        "scan_date": today,
        "proposed": result["proposed"],
        "skipped": result["skipped"],
        "open_positions": len(ledger.open_trades(ledger_path)),
        "equity": {
            "starting_equity": equity,
            "deployed_collateral": deployed_total,
            "available_collateral": round(
                equity * config["max_collateral_pct_of_equity"] - deployed_total, 2
            ),
        },
    }
    out = reports_dir / f"scan_{today}.json"
    out.write_text(json.dumps(report, indent=2, default=str))
    return out


if __name__ == "__main__":
    cfg = load_config()
    result = propose_candidates(config=cfg)
    print(f"Proposed {len(result['proposed'])} trade(s):")
    for t in result["proposed"]:
        print(f"  {t['symbol']:6s} {t['type']:12s} strike={t['strike']:<8.2f} "
              f"exp={t['expiration_date']} credit={t['credit']:.2f}")
    print(f"Skipped {len(result['skipped'])} symbol(s):")
    for sym, reasons in result["skipped"].items():
        print(f"  {sym}: {'; '.join(reasons)}")
    out = write_report(result, cfg)
    print(f"Report written to {out}")
