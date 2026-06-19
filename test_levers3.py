"""test_levers3.py — Test 4 levers individually on top of baseline.

Baseline: F1 (retest-only), F4 (min pattern depth >= 3%), L3 (max 12 bars retest window).

Lever A: Tighter retest zone (0.2% instead of 0.5%)
Lever B: 6-bar timeout instead of 12
Lever C: Minimum 4% room to 2R target
Lever D: Daily EMA slope confirmation (EMA rising over last 3 bars)
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from swing_agent.backtest import bias_asof
from swing_agent.dataio import load
from swing_agent.indicators import atr, ema
from swing_agent.patterns import detect_double_bottom, detect_inverse_hns
from swing_agent.simulator import build_retest_trade, build_trade, compound_ledger, resolve_trade, summarize

DATA = ROOT / "data"
UNIVERSE = DATA / "universe.txt"


def load_symbols() -> list[str]:
    with open(UNIVERSE) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def daily_bias_series_base(daily: pd.DataFrame, ema_period: int = 20) -> pd.Series:
    """Standard daily bias: close > EMA and low > EMA."""
    e = ema(daily["close"], ema_period)
    bias = (daily["close"] > e) & (daily["low"] > e)
    return pd.Series(bias.values, index=pd.to_datetime(daily["begins_at"]))


def daily_bias_series_lever_d(daily: pd.DataFrame, ema_period: int = 20) -> pd.Series:
    """Lever D: also require EMA is rising over last 3 bars."""
    e = ema(daily["close"], ema_period)
    e_vals = e.values
    n = len(e_vals)
    bias_list = []
    for i in range(n):
        base_cond = bool((daily["close"].iloc[i] > e_vals[i]) and (daily["low"].iloc[i] > e_vals[i]))
        if base_cond and i >= 3:
            slope_cond = bool(e_vals[i] > e_vals[i - 3])
        else:
            slope_cond = base_cond
        bias_list.append(base_cond and slope_cond)
    return pd.Series(bias_list, index=pd.to_datetime(daily["begins_at"]))


def run_variant(
    symbols: list[str],
    equity: float,
    risk_pct: float,
    retest_zone: float = 1.005,
    retest_window: int = 12,
    min_room_to_2r: float = 0.0,
    use_ema_slope: bool = False,
) -> list[dict]:
    all_trades: list[dict] = []

    for symbol in symbols:
        daily_path = DATA / f"{symbol}_day.json"
        h4_path = DATA / f"{symbol}_4hour.json"
        if not daily_path.exists() or not h4_path.exists():
            continue

        daily = load(daily_path, symbol)
        h4 = load(h4_path, symbol)
        if len(daily) < 30 or len(h4) < 30:
            continue

        if use_ema_slope:
            daily_bias = daily_bias_series_lever_d(daily)
        else:
            daily_bias = daily_bias_series_base(daily)

        atr_series = atr(h4, 14)
        patterns = detect_double_bottom(h4) + detect_inverse_hns(h4)
        patterns.sort(key=lambda p: p["break_index"])

        open_until: dict[str, int] = {}
        for p in patterns:
            if not bias_asof(daily_bias, p["break_time"]):
                continue
            # F4: min pattern depth >= 3%
            if (p["neckline"] - p["stop_basis"]) / p["neckline"] < 0.03:
                continue
            ptype = p["type"]
            if p["break_index"] <= open_until.get(ptype, -1):
                continue

            trade = build_trade(h4, p, atr_series, equity, risk_pct)
            if trade is None:
                continue

            # Lever C: skip if not enough room to 2R target
            if min_room_to_2r > 0:
                if (trade["target_2R"] - trade["entry"]) / trade["entry"] < min_room_to_2r:
                    resolved_a = resolve_trade(h4, dict(trade) | {"entry_style": "breakout"}, target_key="target_2R")
                    if resolved_a["outcome"] == "open":
                        open_until[ptype] = len(h4)
                    else:
                        idx = h4.index[h4["begins_at"] == resolved_a["exit_time"]].tolist()
                        open_until[ptype] = idx[0] if idx else len(h4)
                    continue

            # Track open_until via breakout trade
            resolved_a = resolve_trade(h4, dict(trade) | {"entry_style": "breakout"}, target_key="target_2R")
            if resolved_a["outcome"] == "open":
                open_until[ptype] = len(h4)
            else:
                idx = h4.index[h4["begins_at"] == resolved_a["exit_time"]].tolist()
                open_until[ptype] = idx[0] if idx else len(h4)

            # F1: retest-only — inline modified build_retest_trade
            neckline = trade["neckline"]
            start = trade["entry_index"] + 1
            shares_equity_risk = trade["risk_per_share"] * trade["shares"]

            in_pullback = False
            trade_b = None
            for i in range(start, min(start + retest_window, len(h4))):
                lo = float(h4.loc[i, "low"])
                hi = float(h4.loc[i, "high"])
                c = float(h4.loc[i, "close"])
                o = float(h4.loc[i, "open"])

                if lo <= neckline * retest_zone:
                    in_pullback = True

                if in_pullback and c >= neckline and c > o:
                    entry_b = c
                    stop_b = trade["stop"]
                    risk_b = entry_b - stop_b
                    if risk_b <= 0:
                        break
                    shares_b = shares_equity_risk / risk_b
                    r1_b = entry_b + risk_b
                    r2_b = entry_b + 2 * risk_b
                    t = dict(trade)
                    t.update({
                        "entry_style": "retest",
                        "entry_time": str(h4.loc[i, "begins_at"]),
                        "entry_index": i,
                        "entry": round(entry_b, 4),
                        "risk_per_share": round(risk_b, 4),
                        "target_primary": round(r2_b, 4),
                        "target_1R": round(r1_b, 4),
                        "target_2R": round(r2_b, 4),
                        "shares": round(shares_b, 4),
                        "atr": round(float(atr_series.iloc[i]), 4),
                    })
                    trade_b = t
                    break

                if hi > neckline * 1.03 and not in_pullback:
                    break

            if trade_b is not None:
                resolved_b = resolve_trade(h4, trade_b, target_key="target_2R")
                resolved_b["symbol"] = symbol
                resolved_b["timeframe"] = "4h"
                all_trades.append(resolved_b)

    all_trades.sort(key=lambda t: t["entry_time"])
    all_trades = compound_ledger(all_trades, equity, risk_pct)
    return all_trades


def compute_stats(trades: list[dict], equity: float) -> dict:
    stats = summarize(trades)
    closed = [t for t in trades if t["outcome"] != "open"]
    final_eq = round(equity + sum(t["pnl_dollars"] for t in closed), 2)
    gain_pct = round((final_eq - equity) / equity * 100, 2)
    return {
        "trades": stats["trades"],
        "win_pct": round(stats["win_rate"] * 100, 1),
        "exp_r": stats["expectancy_R"],
        "total_r": stats["total_R"],
        "final_eq": final_eq,
        "gain_pct": gain_pct,
        "open": stats["open_trades"],
    }


def main():
    symbols = load_symbols()
    print(f"Universe: {len(symbols)} symbols")

    EQUITY = 1500.0
    RISK_PCT = 0.02

    variants = [
        ("Baseline",            dict()),
        ("Lever A (zone 0.2%)", dict(retest_zone=1.002)),
        ("Lever B (6-bar)",     dict(retest_window=6)),
        ("Lever C (4% to 2R)", dict(min_room_to_2r=0.04)),
        ("Lever D (EMA slope)", dict(use_ema_slope=True)),
    ]

    results = []
    for name, kwargs in variants:
        print(f"Running {name}...")
        trades = run_variant(symbols, EQUITY, RISK_PCT, **kwargs)
        stats = compute_stats(trades, EQUITY)
        results.append((name, stats))
        print(f"  -> {stats['trades']} trades, {stats['win_pct']}% win, {stats['exp_r']}R exp")

    header = f"{'Variant':<26} | {'Trades':>6} | {'Win%':>5} | {'Exp(R)':>6} | {'Total R':>7} | {'Final Eq':>9} | {'Gain%':>6} | {'Open':>4}"
    sep = "-" * len(header)
    print(f"\n{sep}")
    print(header)
    print(sep)
    for name, s in results:
        print(
            f"{name:<26} | {s['trades']:>6} | {s['win_pct']:>5} | {s['exp_r']:>6} | "
            f"{s['total_r']:>7} | {s['final_eq']:>9} | {s['gain_pct']:>6} | {s['open']:>4}"
        )
    print(sep)


if __name__ == "__main__":
    main()
