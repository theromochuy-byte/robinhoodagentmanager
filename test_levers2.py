"""
test_levers2.py — Test 6 new filters (L1–L6) individually on top of F1+F4 baseline.

Baseline (already in source):
  - F1: retest-only entries
  - F4: min pattern depth >= 3%
  - equity=1500, risk_pct=0.02

Each lever is monkey-patched independently; source files are NOT modified.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

# ── project root on path ──────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import pandas as pd

# ── load symbols ──────────────────────────────────────────────────────────────
SYMBOLS = (ROOT / "data" / "universe.txt").read_text().split()
EQUITY = 1500.0
RISK_PCT = 0.02


# ── helpers to (re)import a clean copy of a module ───────────────────────────
def fresh_import(name: str):
    """Force a fresh import of a swing_agent sub-module."""
    if name in sys.modules:
        del sys.modules[name]
    return importlib.import_module(name)


def reload_all():
    """Reload all swing_agent modules so monkey-patches don't bleed between runs."""
    for key in list(sys.modules):
        if key.startswith("swing_agent"):
            del sys.modules[key]
    import swing_agent.indicators   # noqa: F401
    import swing_agent.dataio       # noqa: F401
    import swing_agent.patterns     # noqa: F401
    import swing_agent.simulator    # noqa: F401
    import swing_agent.backtest     # noqa: F401


# ── baseline runner (F1 + F4 already in source) ──────────────────────────────
def run_baseline() -> dict:
    reload_all()
    import swing_agent.backtest as bt
    from swing_agent.simulator import compound_ledger, summarize
    all_trades: list[dict] = []
    for sym in SYMBOLS:
        all_trades.extend(bt.run_symbol(sym, EQUITY, RISK_PCT))
    all_trades.sort(key=lambda t: t["entry_time"])
    all_trades = compound_ledger(all_trades, EQUITY, RISK_PCT)
    return _stats(all_trades, "Baseline(F1+F4)")


# ── shared stats helper ───────────────────────────────────────────────────────
def _stats(all_trades: list[dict], label: str) -> dict:
    from swing_agent.simulator import summarize
    closed = [t for t in all_trades if t["outcome"] != "open"]
    open_t = [t for t in all_trades if t["outcome"] == "open"]
    s = summarize(closed)
    final_eq = round(EQUITY + sum(t["pnl_dollars"] for t in closed), 2)
    gain_pct = round((final_eq - EQUITY) / EQUITY * 100, 2)
    return {
        "label": label,
        "trades": s["trades"],
        "win_pct": round(s["win_rate"] * 100, 1),
        "exp_R": s["expectancy_R"],
        "total_R": s["total_R"],
        "final_eq": final_eq,
        "gain_pct": gain_pct,
        "open": len(open_t),
    }


# ── run a variant given a custom run_symbol function ─────────────────────────
def run_variant(label: str, custom_run_symbol) -> dict:
    reload_all()
    from swing_agent.simulator import compound_ledger
    all_trades: list[dict] = []
    for sym in SYMBOLS:
        all_trades.extend(custom_run_symbol(sym, EQUITY, RISK_PCT))
    all_trades.sort(key=lambda t: t["entry_time"])
    all_trades = compound_ledger(all_trades, EQUITY, RISK_PCT)
    return _stats(all_trades, label)


# ══════════════════════════════════════════════════════════════════════════════
# L1: Resistance clearance
# Skip if a prior structure high exists between start-of-bars and break_index
# that is above neckline but below entry + 1.5 * risk_per_share (sits below 1.5R)
# ══════════════════════════════════════════════════════════════════════════════
def run_L1(symbol, equity, risk_pct):
    import swing_agent.backtest as bt
    from swing_agent.simulator import build_trade, build_retest_trade, resolve_trade

    daily_path = bt.DATA / f"{symbol}_day.json"
    intraday_path = bt.DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not intraday_path.exists():
        return []
    from swing_agent.dataio import load
    from swing_agent.indicators import atr
    from swing_agent.patterns import detect_double_bottom, detect_inverse_hns

    daily = load(daily_path, symbol)
    intraday = load(intraday_path, symbol)
    if len(daily) < 30 or len(intraday) < 30:
        return []

    bias = bt.daily_bias_series(daily)
    atr_series = atr(intraday, 14)
    patterns = detect_double_bottom(intraday) + detect_inverse_hns(intraday)
    patterns.sort(key=lambda p: p["break_index"])

    trades = []
    open_until: dict[str, int] = {}
    for p in patterns:
        if not bt.bias_asof(bias, p["break_time"]):
            continue
        if (p["neckline"] - p["stop_basis"]) / p["neckline"] < 0.03:
            continue
        ptype = p["type"]
        if p["break_index"] <= open_until.get(ptype, -1):
            continue
        trade = build_trade(intraday, p, atr_series, equity, risk_pct)
        if trade is None:
            continue

        # ── L1 filter ────────────────────────────────────────────────────────
        bi = p["break_index"]
        neckline = p["neckline"]
        window = intraday.loc[:bi - 1]
        above_neck = window[window["high"] > neckline]
        if len(above_neck) > 0:
            prior_high = float(above_neck["high"].max())
            r1_5 = trade["entry"] + 1.5 * trade["risk_per_share"]
            if prior_high < r1_5:
                # resistance sits below 1.5R — skip
                resolved_a = resolve_trade(intraday, dict(trade) | {"entry_style": "breakout"}, target_key="target_2R")
                if resolved_a["outcome"] == "open":
                    open_until[ptype] = len(intraday)
                else:
                    idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
                    open_until[ptype] = idx[0] if idx else len(intraday)
                continue
        # ─────────────────────────────────────────────────────────────────────

        resolved_a = resolve_trade(intraday, dict(trade) | {"entry_style": "breakout"}, target_key="target_2R")
        if resolved_a["outcome"] == "open":
            open_until[ptype] = len(intraday)
        else:
            idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
            open_until[ptype] = idx[0] if idx else len(intraday)

        trade_b = build_retest_trade(intraday, trade, atr_series)
        if trade_b is not None:
            resolved_b = resolve_trade(intraday, trade_b, target_key="target_2R")
            resolved_b["symbol"] = symbol
            trades.append(resolved_b)
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# L2: Retest bar candlestick quality
# Trigger bar must have: body >= 50% of range AND close in upper 30% of range
# ══════════════════════════════════════════════════════════════════════════════
def run_L2(symbol, equity, risk_pct):
    import swing_agent.backtest as bt
    from swing_agent.simulator import build_trade, resolve_trade
    from swing_agent.dataio import load
    from swing_agent.indicators import atr
    from swing_agent.patterns import detect_double_bottom, detect_inverse_hns

    daily_path = bt.DATA / f"{symbol}_day.json"
    intraday_path = bt.DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not intraday_path.exists():
        return []

    daily = load(daily_path, symbol)
    intraday = load(intraday_path, symbol)
    if len(daily) < 30 or len(intraday) < 30:
        return []

    bias = bt.daily_bias_series(daily)
    atr_series = atr(intraday, 14)
    patterns = detect_double_bottom(intraday) + detect_inverse_hns(intraday)
    patterns.sort(key=lambda p: p["break_index"])

    def build_retest_trade_L2(df, trade, atr_series):
        neckline = trade["neckline"]
        start = trade["entry_index"] + 1
        shares_equity_risk = trade["risk_per_share"] * trade["shares"]
        in_pullback = False
        for i in range(start, len(df)):
            lo = float(df.loc[i, "low"])
            hi = float(df.loc[i, "high"])
            c = float(df.loc[i, "close"])
            o = float(df.loc[i, "open"])
            if lo <= neckline * 1.005:
                in_pullback = True
            if in_pullback and c >= neckline and c > o:
                bar_range = hi - lo
                if bar_range > 0:
                    body_ratio = (c - o) / bar_range
                    close_pos = (c - lo) / bar_range
                    if body_ratio < 0.5 or close_pos < 0.7:
                        continue  # L2: candlestick quality filter
                entry_b = c
                a = float(atr_series.iloc[i])
                stop_b = trade["stop"]
                risk_b = entry_b - stop_b
                if risk_b <= 0:
                    return None
                shares_b = shares_equity_risk / risk_b
                r2_b = entry_b + 2 * risk_b
                t = dict(trade)
                t.update({
                    "entry_style": "retest",
                    "entry_time": str(df.loc[i, "begins_at"]),
                    "entry_index": i,
                    "entry": round(entry_b, 4),
                    "risk_per_share": round(risk_b, 4),
                    "target_primary": round(r2_b, 4),
                    "target_1R": round(entry_b + risk_b, 4),
                    "target_2R": round(r2_b, 4),
                    "shares": round(shares_b, 4),
                    "atr": round(a, 4),
                })
                return t
            if hi > neckline * 1.03 and not in_pullback:
                return None
        return None

    trades = []
    open_until: dict[str, int] = {}
    for p in patterns:
        if not bt.bias_asof(bias, p["break_time"]):
            continue
        if (p["neckline"] - p["stop_basis"]) / p["neckline"] < 0.03:
            continue
        ptype = p["type"]
        if p["break_index"] <= open_until.get(ptype, -1):
            continue
        trade = build_trade(intraday, p, atr_series, equity, risk_pct)
        if trade is None:
            continue
        resolved_a = resolve_trade(intraday, dict(trade) | {"entry_style": "breakout"}, target_key="target_2R")
        if resolved_a["outcome"] == "open":
            open_until[ptype] = len(intraday)
        else:
            idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
            open_until[ptype] = idx[0] if idx else len(intraday)
        trade_b = build_retest_trade_L2(intraday, trade, atr_series)
        if trade_b is not None:
            resolved_b = resolve_trade(intraday, trade_b, target_key="target_2R")
            resolved_b["symbol"] = symbol
            trades.append(resolved_b)
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# L3: Pattern age timeout — max 12 bars after breakout to find retest
# ══════════════════════════════════════════════════════════════════════════════
def run_L3(symbol, equity, risk_pct):
    import swing_agent.backtest as bt
    from swing_agent.simulator import build_trade, resolve_trade
    from swing_agent.dataio import load
    from swing_agent.indicators import atr
    from swing_agent.patterns import detect_double_bottom, detect_inverse_hns

    daily_path = bt.DATA / f"{symbol}_day.json"
    intraday_path = bt.DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not intraday_path.exists():
        return []

    daily = load(daily_path, symbol)
    intraday = load(intraday_path, symbol)
    if len(daily) < 30 or len(intraday) < 30:
        return []

    bias = bt.daily_bias_series(daily)
    atr_series = atr(intraday, 14)
    patterns = detect_double_bottom(intraday) + detect_inverse_hns(intraday)
    patterns.sort(key=lambda p: p["break_index"])

    MAX_RETEST_BARS = 12

    def build_retest_trade_L3(df, trade, atr_series):
        neckline = trade["neckline"]
        start = trade["entry_index"] + 1
        shares_equity_risk = trade["risk_per_share"] * trade["shares"]
        in_pullback = False
        for i in range(start, len(df)):
            if i - start > MAX_RETEST_BARS:  # L3: timeout
                return None
            lo = float(df.loc[i, "low"])
            hi = float(df.loc[i, "high"])
            c = float(df.loc[i, "close"])
            o = float(df.loc[i, "open"])
            if lo <= neckline * 1.005:
                in_pullback = True
            if in_pullback and c >= neckline and c > o:
                entry_b = c
                a = float(atr_series.iloc[i])
                stop_b = trade["stop"]
                risk_b = entry_b - stop_b
                if risk_b <= 0:
                    return None
                shares_b = shares_equity_risk / risk_b
                r2_b = entry_b + 2 * risk_b
                t = dict(trade)
                t.update({
                    "entry_style": "retest",
                    "entry_time": str(df.loc[i, "begins_at"]),
                    "entry_index": i,
                    "entry": round(entry_b, 4),
                    "risk_per_share": round(risk_b, 4),
                    "target_primary": round(r2_b, 4),
                    "target_1R": round(entry_b + risk_b, 4),
                    "target_2R": round(r2_b, 4),
                    "shares": round(shares_b, 4),
                    "atr": round(a, 4),
                })
                return t
            if hi > neckline * 1.03 and not in_pullback:
                return None
        return None

    trades = []
    open_until: dict[str, int] = {}
    for p in patterns:
        if not bt.bias_asof(bias, p["break_time"]):
            continue
        if (p["neckline"] - p["stop_basis"]) / p["neckline"] < 0.03:
            continue
        ptype = p["type"]
        if p["break_index"] <= open_until.get(ptype, -1):
            continue
        trade = build_trade(intraday, p, atr_series, equity, risk_pct)
        if trade is None:
            continue
        resolved_a = resolve_trade(intraday, dict(trade) | {"entry_style": "breakout"}, target_key="target_2R")
        if resolved_a["outcome"] == "open":
            open_until[ptype] = len(intraday)
        else:
            idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
            open_until[ptype] = idx[0] if idx else len(intraday)
        trade_b = build_retest_trade_L3(intraday, trade, atr_series)
        if trade_b is not None:
            resolved_b = resolve_trade(intraday, trade_b, target_key="target_2R")
            resolved_b["symbol"] = symbol
            trades.append(resolved_b)
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# L4: Neckline slope on inverse H&S
# Skip if peak2_high < peak1_high by more than 2% (declining neckline)
# ══════════════════════════════════════════════════════════════════════════════
def run_L4(symbol, equity, risk_pct):
    import swing_agent.backtest as bt
    from swing_agent.simulator import build_trade, build_retest_trade, resolve_trade
    from swing_agent.dataio import load
    from swing_agent.indicators import atr
    from swing_agent.patterns import detect_double_bottom, detect_inverse_hns

    daily_path = bt.DATA / f"{symbol}_day.json"
    intraday_path = bt.DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not intraday_path.exists():
        return []

    daily = load(daily_path, symbol)
    intraday = load(intraday_path, symbol)
    if len(daily) < 30 or len(intraday) < 30:
        return []

    bias = bt.daily_bias_series(daily)
    atr_series = atr(intraday, 14)
    patterns = detect_double_bottom(intraday) + detect_inverse_hns(intraday)
    patterns.sort(key=lambda p: p["break_index"])

    trades = []
    open_until: dict[str, int] = {}
    for p in patterns:
        if not bt.bias_asof(bias, p["break_time"]):
            continue
        if (p["neckline"] - p["stop_basis"]) / p["neckline"] < 0.03:
            continue
        ptype = p["type"]
        if p["break_index"] <= open_until.get(ptype, -1):
            continue

        # ── L4 filter (inverse_hns only) ─────────────────────────────────────
        if p["type"] == "inverse_hns":
            pvts = p["pivots"]
            ls = pvts["left_shoulder"]
            head = pvts["head"]
            rs = pvts["right_shoulder"]
            # peak1: highest high between left_shoulder and head
            win1 = intraday.loc[ls["index"]:head["index"]]
            peak1_high = float(win1["high"].max()) if len(win1) else 0.0
            # peak2: highest high between head and right_shoulder
            win2 = intraday.loc[head["index"]:rs["index"]]
            peak2_high = float(win2["high"].max()) if len(win2) else 0.0
            if peak1_high > 0 and (peak2_high - peak1_high) / peak1_high < -0.02:
                # neckline declining more than 2% — skip
                trade_tmp = build_trade(intraday, p, atr_series, equity, risk_pct)
                if trade_tmp is not None:
                    resolved_a = resolve_trade(intraday, dict(trade_tmp) | {"entry_style": "breakout"}, target_key="target_2R")
                    if resolved_a["outcome"] == "open":
                        open_until[ptype] = len(intraday)
                    else:
                        idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
                        open_until[ptype] = idx[0] if idx else len(intraday)
                continue
        # ─────────────────────────────────────────────────────────────────────

        trade = build_trade(intraday, p, atr_series, equity, risk_pct)
        if trade is None:
            continue
        resolved_a = resolve_trade(intraday, dict(trade) | {"entry_style": "breakout"}, target_key="target_2R")
        if resolved_a["outcome"] == "open":
            open_until[ptype] = len(intraday)
        else:
            idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
            open_until[ptype] = idx[0] if idx else len(intraday)
        trade_b = build_retest_trade(intraday, trade, atr_series)
        if trade_b is not None:
            resolved_b = resolve_trade(intraday, trade_b, target_key="target_2R")
            resolved_b["symbol"] = symbol
            trades.append(resolved_b)
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# L5: ATR ratio filter — retest ATR must be >= 75% of breakout ATR
# ══════════════════════════════════════════════════════════════════════════════
def run_L5(symbol, equity, risk_pct):
    import swing_agent.backtest as bt
    from swing_agent.simulator import build_trade, resolve_trade
    from swing_agent.dataio import load
    from swing_agent.indicators import atr
    from swing_agent.patterns import detect_double_bottom, detect_inverse_hns

    daily_path = bt.DATA / f"{symbol}_day.json"
    intraday_path = bt.DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not intraday_path.exists():
        return []

    daily = load(daily_path, symbol)
    intraday = load(intraday_path, symbol)
    if len(daily) < 30 or len(intraday) < 30:
        return []

    bias = bt.daily_bias_series(daily)
    atr_series = atr(intraday, 14)
    patterns = detect_double_bottom(intraday) + detect_inverse_hns(intraday)
    patterns.sort(key=lambda p: p["break_index"])

    def build_retest_trade_L5(df, trade, atr_series):
        neckline = trade["neckline"]
        start = trade["entry_index"] + 1
        shares_equity_risk = trade["risk_per_share"] * trade["shares"]
        breakout_atr = trade["atr"]
        in_pullback = False
        for i in range(start, len(df)):
            lo = float(df.loc[i, "low"])
            hi = float(df.loc[i, "high"])
            c = float(df.loc[i, "close"])
            o = float(df.loc[i, "open"])
            if lo <= neckline * 1.005:
                in_pullback = True
            if in_pullback and c >= neckline and c > o:
                # L5: ATR ratio check
                current_atr = float(atr_series.iloc[i])
                if breakout_atr > 0 and (current_atr / breakout_atr) < 0.75:
                    continue  # ATR too contracted
                entry_b = c
                a = current_atr
                stop_b = trade["stop"]
                risk_b = entry_b - stop_b
                if risk_b <= 0:
                    return None
                shares_b = shares_equity_risk / risk_b
                r2_b = entry_b + 2 * risk_b
                t = dict(trade)
                t.update({
                    "entry_style": "retest",
                    "entry_time": str(df.loc[i, "begins_at"]),
                    "entry_index": i,
                    "entry": round(entry_b, 4),
                    "risk_per_share": round(risk_b, 4),
                    "target_primary": round(r2_b, 4),
                    "target_1R": round(entry_b + risk_b, 4),
                    "target_2R": round(r2_b, 4),
                    "shares": round(shares_b, 4),
                    "atr": round(a, 4),
                })
                return t
            if hi > neckline * 1.03 and not in_pullback:
                return None
        return None

    trades = []
    open_until: dict[str, int] = {}
    for p in patterns:
        if not bt.bias_asof(bias, p["break_time"]):
            continue
        if (p["neckline"] - p["stop_basis"]) / p["neckline"] < 0.03:
            continue
        ptype = p["type"]
        if p["break_index"] <= open_until.get(ptype, -1):
            continue
        trade = build_trade(intraday, p, atr_series, equity, risk_pct)
        if trade is None:
            continue
        resolved_a = resolve_trade(intraday, dict(trade) | {"entry_style": "breakout"}, target_key="target_2R")
        if resolved_a["outcome"] == "open":
            open_until[ptype] = len(intraday)
        else:
            idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
            open_until[ptype] = idx[0] if idx else len(intraday)
        trade_b = build_retest_trade_L5(intraday, trade, atr_series)
        if trade_b is not None:
            resolved_b = resolve_trade(intraday, trade_b, target_key="target_2R")
            resolved_b["symbol"] = symbol
            trades.append(resolved_b)
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# L6: Right shoulder higher-low magnitude >= 0.5% above head
# ══════════════════════════════════════════════════════════════════════════════
def run_L6(symbol, equity, risk_pct):
    import swing_agent.backtest as bt
    from swing_agent.simulator import build_trade, build_retest_trade, resolve_trade
    from swing_agent.dataio import load
    from swing_agent.indicators import atr
    from swing_agent.patterns import detect_double_bottom, _dedupe, _confirm_break
    from swing_agent.indicators import pivots, swing_points

    daily_path = bt.DATA / f"{symbol}_day.json"
    intraday_path = bt.DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not intraday_path.exists():
        return []

    daily = load(daily_path, symbol)
    intraday = load(intraday_path, symbol)
    if len(daily) < 30 or len(intraday) < 30:
        return []

    bias = bt.daily_bias_series(daily)
    atr_series = atr(intraday, 14)

    # patched detect_inverse_hns with L6 check
    def detect_inverse_hns_L6(df, left=3, right=3, shoulder_tol=0.06, min_sep=3, max_span=90):
        marked = swing_points(df, left, right)
        lows = pivots(marked, "low")
        highs = pivots(marked, "high")
        patterns = []
        for i in range(len(lows)):
            for j in range(i + 1, len(lows)):
                for k in range(j + 1, len(lows)):
                    ls, head, rs = lows[i], lows[j], lows[k]
                    if rs["index"] - ls["index"] > max_span:
                        continue
                    if (head["index"] - ls["index"]) < min_sep or (rs["index"] - head["index"]) < min_sep:
                        continue
                    if not (head["price"] < ls["price"] and head["price"] < rs["price"]):
                        continue
                    if abs(rs["price"] - ls["price"]) / ls["price"] > shoulder_tol:
                        continue
                    # ── L6: right shoulder higher-low magnitude ───────────────
                    if (rs["price"] - head["price"]) / head["price"] < 0.005:
                        continue
                    # ─────────────────────────────────────────────────────────
                    peak1 = [h for h in highs if ls["index"] < h["index"] < head["index"]]
                    peak2 = [h for h in highs if head["index"] < h["index"] < rs["index"]]
                    if not peak1 or not peak2:
                        continue
                    neckline = max(max(p["price"] for p in peak1), max(p["price"] for p in peak2))
                    brk = _confirm_break(df, neckline, rs["index"])
                    if not brk:
                        continue
                    patterns.append({
                        "type": "inverse_hns",
                        "neckline": neckline,
                        "stop_basis": rs["price"],
                        "break_index": brk["break_index"],
                        "break_time": brk["break_time"],
                        "pivots": {
                            "left_shoulder": ls,
                            "head": head,
                            "right_shoulder": rs,
                            "neckline_price": neckline,
                        },
                    })
        return _dedupe(patterns)

    patterns = detect_double_bottom(intraday) + detect_inverse_hns_L6(intraday)
    patterns.sort(key=lambda p: p["break_index"])

    trades = []
    open_until: dict[str, int] = {}
    for p in patterns:
        if not bt.bias_asof(bias, p["break_time"]):
            continue
        if (p["neckline"] - p["stop_basis"]) / p["neckline"] < 0.03:
            continue
        ptype = p["type"]
        if p["break_index"] <= open_until.get(ptype, -1):
            continue
        trade = build_trade(intraday, p, atr_series, equity, risk_pct)
        if trade is None:
            continue
        resolved_a = resolve_trade(intraday, dict(trade) | {"entry_style": "breakout"}, target_key="target_2R")
        if resolved_a["outcome"] == "open":
            open_until[ptype] = len(intraday)
        else:
            idx = intraday.index[intraday["begins_at"] == resolved_a["exit_time"]].tolist()
            open_until[ptype] = idx[0] if idx else len(intraday)
        trade_b = build_retest_trade(intraday, trade, atr_series)
        if trade_b is not None:
            resolved_b = resolve_trade(intraday, trade_b, target_key="target_2R")
            resolved_b["symbol"] = symbol
            trades.append(resolved_b)
    return trades


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════
def print_table(results: list[dict]):
    header = f"{'Variant':<22} | {'Trades':>6} | {'Win%':>5} | {'Exp(R)':>6} | {'Total R':>7} | {'Final Eq':>9} | {'Gain%':>6} | {'Open':>4}"
    sep = "-" * len(header)
    print()
    print(sep)
    print(header)
    print(sep)
    for r in results:
        flag = ""
        # flag variants that improve win rate over baseline AND have > 40 closed trades
        baseline_win = results[0]["win_pct"]
        baseline_exp = results[0]["exp_R"]
        if r["label"] != results[0]["label"]:
            if r["win_pct"] > baseline_win and r["trades"] > 40:
                flag = " <-- WIN RATE UP, N>40"
        print(
            f"{r['label']:<22} | {r['trades']:>6} | {r['win_pct']:>5.1f} | "
            f"{r['exp_R']:>6.3f} | {r['total_R']:>7.2f} | "
            f"{r['final_eq']:>9.2f} | {r['gain_pct']:>6.1f} | {r['open']:>4}{flag}"
        )
    print(sep)
    print()


if __name__ == "__main__":
    results = []

    print("\n=== BASELINE (F1+F4) ===")
    results.append(run_baseline())

    print("\n=== L1: Resistance clearance ===")
    results.append(run_variant("L1: Resistance", run_L1))

    print("\n=== L2: Candle quality ===")
    results.append(run_variant("L2: Candle quality", run_L2))

    print("\n=== L3: Age timeout (12 bars) ===")
    results.append(run_variant("L3: Age timeout", run_L3))

    print("\n=== L4: Neckline slope (iHnS) ===")
    results.append(run_variant("L4: Neckline slope", run_L4))

    print("\n=== L5: ATR ratio >=0.75 ===")
    results.append(run_variant("L5: ATR ratio", run_L5))

    print("\n=== L6: RS higher-low >=0.5% ===")
    results.append(run_variant("L6: RS magnitude", run_L6))

    print_table(results)
