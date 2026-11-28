#!/usr/bin/env python3
"""Side-by-side portfolio backtest: current pattern setup vs. uptrend-pullback setup.

Both strategies run through the same account simulator (paper/backtest only):
  - $STARTING_EQUITY, RISK_PCT of current equity per trade, 25% notional cap per position
  - at most MAX_OPEN positions at once and MAX_ENTRIES_PER_DAY new entries per day
  - SLIPPAGE per side charged against every fill (Robinhood has no commission)
Market regime at entry comes from an equal-weight index of the universe:
  bull = index > 50 SMA and 50 SMA rising; bear = below and falling; chop = otherwise.

Inputs: reports/backtest_<date>.json (pattern trades) and the pullback scanner.
Output: reports/strategy_comparison.json
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from run_pullback_benchmark import scan as pullback_scan
from swing_agent.config import MAX_ENTRIES_PER_DAY, RISK_PCT, STARTING_EQUITY
from swing_agent.dataio import bars_to_df
from swing_agent.indicators import ema

CACHE = ROOT / "data" / "hist_cache"
REPORTS = ROOT / "reports"
PATTERN_REPORT = REPORTS / "backtest_2026-11-25.json"
WINDOW_START = "2025-12-22"   # first 1H bar in the cache — pattern backtest can't start earlier
MAX_OPEN = 4
SLIPPAGE = 0.0005
NOTIONAL_CAP = 0.25


def load_daily() -> dict[str, pd.DataFrame]:
    out = {}
    for p in CACHE.glob("*_day.json"):
        sym = p.stem.split("_")[0]
        df = bars_to_df(json.load(open(p)), sym)
        df["date"] = pd.to_datetime(df["begins_at"]).dt.strftime("%Y-%m-%d")
        out[sym] = df.set_index("date")
    return out


def regime_series(daily: dict[str, pd.DataFrame]) -> tuple[pd.Series, pd.Series]:
    closes = pd.DataFrame({s: d["close"] for s, d in daily.items()}).sort_index()
    idx = (closes / closes.bfill().iloc[0]).mean(axis=1)
    sma50 = idx.rolling(50).mean()
    rising = sma50.diff(10) > 0
    reg = pd.Series("chop", index=idx.index)
    reg[(idx > sma50) & rising] = "bull"
    reg[(idx < sma50) & ~rising] = "bear"
    return reg, idx


def asof(series: pd.Series, when: str):
    d = when[:10]
    prior = series[series.index <= d]
    return prior.iloc[-1] if len(prior) else None


def current_setup_trades(daily, idx) -> list[dict]:
    """Reconstruct the live scanner: inverse H&S + cup-and-handle, retest entry, plus the
    EMA-stack (20 > 50 > 200) and 20-day relative-strength filters the live scanner applies."""
    raw = json.load(open(PATTERN_REPORT))["trades"]
    keep = []
    for t in raw:
        if t["type"] not in ("inverse_hns", "cup_and_handle") or t.get("entry_style") != "retest":
            continue
        d = daily[t["symbol"]]
        upto = d[d.index <= t["entry_time"][:10]]
        if len(upto) < 200:
            continue
        c = upto["close"]
        e20, s50, s200 = ema(c, 20).iloc[-1], c.rolling(50).mean().iloc[-1], c.rolling(200).mean().iloc[-1]
        if not (e20 > s50 > s200):
            continue
        i_up = idx[idx.index <= t["entry_time"][:10]]
        if len(c) > 21 and len(i_up) > 21:
            if c.iloc[-1] / c.iloc[-21] - 1 < i_up.iloc[-1] / i_up.iloc[-21] - 1:
                continue
        keep.append({**t, "side": "long", "r": t["r_multiple"], "rank": -t.get("quality_score", 0)})
    return keep


def hns_short_trades() -> list[dict]:
    raw = json.load(open(PATTERN_REPORT))["trades"]
    return [{**t, "side": "short", "r": t["r_multiple"], "rank": -t.get("quality_score", 0)}
            for t in raw if t["type"] == "hns" and t.get("entry_style") == "retest"]


def funnel_trades(keep) -> list[dict]:
    """Pattern retest trades from reports/filter_funnel.json that pass `keep`, one open per stock."""
    from filter_funnel import ok, one_at_a_time
    rows = json.load(open(REPORTS / "filter_funnel.json"))
    picked = one_at_a_time([r for r in rows if keep(r, ok)])
    return [{**r, "side": "long", "rank": 0} for r in picked]


RELAXED = {
    "patterns_live_filters": lambda r, ok: all(ok(r, g) for g in ("pattern", "bias", "stack", "rs", "depth", "qs")),
    "patterns_relaxed": lambda r, ok: ok(r, "pattern") and ok(r, "depth"),
    "patterns_relaxed_uptrend": lambda r, ok: ok(r, "pattern") and ok(r, "depth") and ok(r, "bias"),
    "patterns_relaxed_no_uptrend": lambda r, ok: ok(r, "pattern") and ok(r, "depth") and not ok(r, "bias"),
}


def at_close(trades: list[dict]) -> list[dict]:
    """Daily-bar trades fill at the close; stamp them 20:00Z so they sort after same-day 1H bars."""
    return [{**t, "entry_time": t["entry_time"][:10] + "T20:00:00Z",
             "exit_time": t["exit_time"][:10] + "T20:00:00Z"} for t in trades]


def pullback_trades(side: str, since: str | None) -> list[dict]:
    syms = sorted(p.stem.split("_")[0] for p in CACHE.glob("*_day.json"))
    out = []
    for s in syms:
        for t in pullback_scan(s, "ours", "classic", since, side):
            t["rank"] = t["rsi2"] if side == "long" else -t["rsi2"]
            out.append(t)
    return out


def simulate(trades: list[dict], regime: pd.Series, start: str) -> dict:
    trades = sorted((t for t in trades if t["entry_time"][:10] >= start),
                    key=lambda t: (t["entry_time"], t["rank"]))
    equity, peak, max_dd = STARTING_EQUITY, STARTING_EQUITY, 0.0
    open_pos: list[dict] = []
    taken, skipped = [], 0
    per_day: dict[str, int] = defaultdict(int)

    def close_until(when: str):
        nonlocal equity, peak, max_dd
        for p in sorted([p for p in open_pos if p["exit_time"] <= when], key=lambda p: p["exit_time"]):
            equity += p["pnl"]
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak)
            open_pos.remove(p)

    for t in trades:
        close_until(t["entry_time"])
        day = t["entry_time"][:10]
        if len(open_pos) >= MAX_OPEN or per_day[day] >= MAX_ENTRIES_PER_DAY:
            skipped += 1
            continue
        risk_ps, entry = t["risk_per_share"], t["entry"]
        if risk_ps <= 0:
            continue
        shares = min(equity * RISK_PCT / risk_ps, equity * NOTIONAL_CAP / entry)
        cost_r = 2 * SLIPPAGE * entry / risk_ps
        net_r = t["r"] - cost_r
        pos = {**t, "net_r": round(net_r, 3), "pnl": round(net_r * risk_ps * shares, 2),
               "dollar_risk": round(risk_ps * shares, 2), "regime": asof(regime, t["entry_time"])}
        open_pos.append(pos)
        taken.append(pos)
        per_day[day] += 1
    close_until("9999")

    def stats(ts: list[dict]) -> dict:
        n = len(ts)
        if not n:
            return {"n": 0}
        wins = [t for t in ts if t["net_r"] > 0]
        gross_win = sum(t["pnl"] for t in wins)
        gross_loss = -sum(t["pnl"] for t in ts if t["net_r"] <= 0)
        return {"n": n, "win_pct": round(100 * len(wins) / n, 1),
                "avg_r_gross": round(sum(t["r"] for t in ts) / n, 3),
                "avg_r_net": round(sum(t["net_r"] for t in ts) / n, 3),
                "pnl": round(sum(t["pnl"] for t in ts), 2),
                "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None}

    months = max(1e-9, (pd.Timestamp(trades[-1]["entry_time"][:10]) - pd.Timestamp(start)).days / 30.44) if trades else 1
    curve, eq = [], STARTING_EQUITY
    for t in sorted(taken, key=lambda t: t["exit_time"]):
        eq += t["pnl"]
        curve.append([t["exit_time"][:10], round(eq, 2)])
    return {
        "overall": stats(taken),
        "by_regime": {r: stats([t for t in taken if t["regime"] == r]) for r in ("bull", "chop", "bear")},
        "by_side": {s: stats([t for t in taken if t["side"] == s]) for s in ("long", "short")},
        "signals": len(trades), "skipped_capacity": skipped,
        "trades_per_month": round(len(taken) / months, 1), "months": round(months, 1),
        "start_equity": STARTING_EQUITY, "end_equity": round(equity, 2),
        "return_pct": round(100 * (equity / STARTING_EQUITY - 1), 1),
        "max_drawdown_pct": round(100 * max_dd, 1), "equity_curve": curve,
    }


def live_ledger() -> dict:
    rows = json.load(open(ROOT / "data" / "paper_trades_live.json"))
    closed = [r for r in rows if r.get("status") not in ("voided", "open", "watching") and r.get("exit_price")]
    rs = []
    for r in closed:
        risk = (r.get("risk_per_share") or 0) * (r.get("shares") or 0)
        if risk and r.get("realized_pnl") is not None:
            rs.append(r["realized_pnl"] / risk)
    n = len(rs)
    return {"n": n, "win_pct": round(100 * sum(x > 0 for x in rs) / n, 1) if n else 0,
            "avg_r": round(sum(rs) / n, 3) if n else 0,
            "pnl": round(sum(r.get("realized_pnl") or 0 for r in closed), 2),
            "statuses": {s: sum(1 for r in closed if r["status"] == s) for s in {r["status"] for r in closed}}}


def main() -> None:
    daily = load_daily()
    regime, idx = regime_series(daily)
    cur = current_setup_trades(daily, idx)
    hns = hns_short_trades()
    pb_long_all = pullback_trades("long", None)
    pb_short_all = pullback_trades("short", None)
    full_start = min(t["entry_time"][:10] for t in pb_long_all)

    runs = {
        "current": simulate(cur, regime, WINDOW_START),
        "current_plus_hns_short": simulate(cur + hns, regime, WINDOW_START),
        "pullback_long": simulate(pb_long_all, regime, WINDOW_START),
        "pullback_long_short": simulate(pb_long_all + pb_short_all, regime, WINDOW_START),
        "pullback_long_2y": simulate(pb_long_all, regime, full_start),
        "pullback_long_short_2y": simulate(pb_long_all + pb_short_all, regime, full_start),
    }
    for name, keep in RELAXED.items():
        runs[name] = simulate(funnel_trades(keep), regime, WINDOW_START)
    runs["pullback_plus_patterns_relaxed"] = simulate(
        at_close(pb_long_all) + funnel_trades(RELAXED["patterns_relaxed"]), regime, WINDOW_START)
    regime_days = regime[regime.index >= full_start].value_counts().to_dict()
    regime_days_window = regime[regime.index >= WINDOW_START].value_counts().to_dict()
    out = {"settings": {"start_equity": STARTING_EQUITY, "risk_pct": RISK_PCT, "max_open": MAX_OPEN,
                        "max_entries_per_day": MAX_ENTRIES_PER_DAY, "slippage_per_side": SLIPPAGE,
                        "window_start": WINDOW_START, "full_start": full_start},
           "regime_days_2y": regime_days, "regime_days_window": regime_days_window,
           "index_curve": [[d, round(v, 4)] for d, v in idx[idx.index >= full_start].items()],
           "live_ledger": live_ledger(), "runs": runs}
    (REPORTS / "strategy_comparison.json").write_text(json.dumps(out, indent=1))

    print(f"Regime days (2y): {regime_days}   (window): {regime_days_window}")
    print(f"Live paper ledger: {out['live_ledger']}")
    for name, r in runs.items():
        o = r["overall"]
        print(f"\n{name}: n={o.get('n')} win%={o.get('win_pct')} avgR net={o.get('avg_r_net')} "
              f"(gross {o.get('avg_r_gross')}) PF={o.get('profit_factor')} return={r['return_pct']}% "
              f"maxDD={r['max_drawdown_pct']}% trades/mo={r['trades_per_month']} "
              f"signals={r['signals']} skipped={r['skipped_capacity']}")
        for k, v in {**r["by_regime"], **r["by_side"]}.items():
            if v.get("n"):
                print(f"    {k:6s} n={v['n']:4d} win%={v['win_pct']:5.1f} avgR={v['avg_r_net']:+.3f} pnl=${v['pnl']:+.0f}")


if __name__ == "__main__":
    main()
