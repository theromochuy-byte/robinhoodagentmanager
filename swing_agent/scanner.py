"""Live forward scanner — paper trading only, no real orders.

Scans all universe symbols for active retest setups and logs proposed
paper trades to data/paper_trades_live.json. Run once per day after close.

A setup is "live" when:
  1. A qualifying pattern broke its neckline within the last 12 4h bars.
  2. Daily bias is long at the time of the break.
  3. Pattern depth >= 3%.
  4. The retest has NOT yet triggered (still watching) OR just triggered today.

Outputs:
  data/paper_trades_live.json  — all open paper positions + today's new entries
  reports/scan_<date>.json     — today's watchlist + triggered entries
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .backtest import daily_bias_series, bias_asof
from .dataio import load
from .indicators import atr, ema
from .patterns import detect_double_bottom, detect_inverse_hns, detect_cup_and_handle
from .simulator import build_trade
from .watchlist import (
    upsert_watching, mark_triggered, mark_missed, expire_stale, watchlist_summary,
)

ROOT        = Path(__file__).resolve().parent.parent
DATA        = ROOT / "data"
REPORTS     = ROOT / "reports"
LIVE_LEDGER = DATA / "paper_trades_live.json"
EQUITY_FILE = DATA / "equity.json"

SESSION_UNIVERSE = DATA / "session_universe.txt"
INDICATOR_CACHE  = DATA / "indicator_cache.json"
CACHE_MAX_AGE    = 8 * 3600  # 8 hours

VIX_HIGH_THRESHOLD = 25.0  # skip new entries when VIX is elevated
VIX_CACHE_FILE     = DATA / "vix_cache.json"

# Sector ETF map: symbol prefix/membership → SPDR sector ETF
# Covers the 11 GICS sectors; unmapped symbols are allowed through (no false blocks)
SECTOR_ETF_MAP: dict[str, str] = {
    # Technology
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AVGO": "XLK", "ORCL": "XLK",
    "CRM": "XLK", "ACN": "XLK", "AMD": "XLK", "TXN": "XLK", "QCOM": "XLK",
    "INTC": "XLK", "IBM": "XLK", "NOW": "XLK", "INTU": "XLK", "AMAT": "XLK",
    "MU": "XLK", "LRCX": "XLK", "KLAC": "XLK", "SNPS": "XLK", "CDNS": "XLK",
    # Health Care
    "UNH": "XLV", "LLY": "XLV", "JNJ": "XLV", "ABBV": "XLV", "MRK": "XLV",
    "TMO": "XLV", "ABT": "XLV", "DHR": "XLV", "ISRG": "XLV", "SYK": "XLV",
    "AMGN": "XLV", "GILD": "XLV", "VRTX": "XLV", "REGN": "XLV", "BSX": "XLV",
    "MDT": "XLV", "ELV": "XLV", "CI": "XLV", "HCA": "XLV", "ZTS": "XLV",
    # Financials
    "BRK.B": "XLF", "JPM": "XLF", "V": "XLF", "MA": "XLF", "BAC": "XLF",
    "WFC": "XLF", "GS": "XLF", "MS": "XLF", "AXP": "XLF", "BLK": "XLF",
    "SCHW": "XLF", "CB": "XLF", "SPGI": "XLF", "MCO": "XLF", "USB": "XLF",
    "PNC": "XLF", "TFC": "XLF", "PRU": "XLF", "MET": "XLF", "AON": "XLF",
    # Industrials
    "GE": "XLI", "RTX": "XLI", "CAT": "XLI", "HON": "XLI", "UPS": "XLI",
    "BA": "XLI", "DE": "XLI", "MMM": "XLI", "LMT": "XLI", "NOC": "XLI",
    "GD": "XLI", "EMR": "XLI", "ETN": "XLI", "PH": "XLI", "ROK": "XLI",
    "ITW": "XLI", "CMI": "XLI", "FDX": "XLI", "UNP": "XLI", "CSX": "XLI",
    # Consumer Discretionary
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "MCD": "XLY", "NKE": "XLY",
    "SBUX": "XLY", "TJX": "XLY", "LOW": "XLY", "BKNG": "XLY", "CMG": "XLY",
    "ABNB": "XLY", "GM": "XLY", "F": "XLY", "DHI": "XLY", "PHM": "XLY",
    # Consumer Staples
    "PG": "XLP", "KO": "XLP", "PEP": "XLP", "COST": "XLP", "WMT": "XLP",
    "PM": "XLP", "MO": "XLP", "CL": "XLP", "MDLZ": "XLP", "KHC": "XLP",
    # Energy
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "SLB": "XLE", "EOG": "XLE",
    "MPC": "XLE", "PSX": "XLE", "VLO": "XLE", "OXY": "XLE", "HAL": "XLE",
    # Utilities
    "NEE": "XLU", "DUK": "XLU", "SO": "XLU", "D": "XLU", "AEP": "XLU",
    "EXC": "XLU", "SRE": "XLU", "XEL": "XLU", "ED": "XLU", "PCG": "XLU",
    # Real Estate
    "PLD": "XLRE", "AMT": "XLRE", "EQIX": "XLRE", "CCI": "XLRE", "PSA": "XLRE",
    # Communication Services
    "META": "XLC", "GOOGL": "XLC", "GOOG": "XLC", "NFLX": "XLC", "DIS": "XLC",
    "CMCSA": "XLC", "T": "XLC", "VZ": "XLC", "TMUS": "XLC", "ATVI": "XLC",
    # Materials
    "LIN": "XLB", "APD": "XLB", "SHW": "XLB", "FCX": "XLB", "NEM": "XLB",
}

SECTOR_ETF_CACHE_FILE = DATA / "sector_etf_cache.json"
SECTOR_ETF_CACHE_AGE  = 8 * 3600  # 8 hours


def _fetch_etf_price(ticker: str) -> float | None:
    """Fetch latest close for a sector ETF via Yahoo Finance."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        meta = data["chart"]["result"][0]["meta"]
        return float(meta["regularMarketPrice"])
    except Exception:
        return None


def _load_sector_etf_bias() -> dict[str, bool]:
    """Return {etf: is_bullish} for all sector ETFs, using cache when fresh.

    Bullish = ETF price > its 20-bar SMA (approximated via Yahoo Finance 60-day daily bars).
    Falls back to allowing all sectors when the fetch fails.
    """
    if SECTOR_ETF_CACHE_FILE.exists():
        age = time.time() - SECTOR_ETF_CACHE_FILE.stat().st_mtime
        if age < SECTOR_ETF_CACHE_AGE:
            return json.loads(SECTOR_ETF_CACHE_FILE.read_text())

    etfs = set(SECTOR_ETF_MAP.values())
    result: dict[str, bool] = {}
    for etf in sorted(etfs):
        try:
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{etf}?interval=1d&range=60d"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            if len(closes) < 20:
                result[etf] = True  # not enough data — allow through
                continue
            price = closes[-1]
            sma20 = sum(closes[-20:]) / 20
            result[etf] = price > sma20
        except Exception:
            result[etf] = True  # fetch failed — allow through
    SECTOR_ETF_CACHE_FILE.write_text(json.dumps(result))
    return result


def _fetch_vix() -> float | None:
    """Fetch current VIX from Yahoo Finance JSON endpoint. Returns None on failure."""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        result = {"vix": float(price), "fetched_at": time.time()}
        VIX_CACHE_FILE.write_text(json.dumps(result))
        return float(price)
    except Exception:
        return None


def _load_vix() -> float | None:
    """Return cached VIX if fresh (< 4 hours), else fetch live."""
    if VIX_CACHE_FILE.exists():
        age = time.time() - VIX_CACHE_FILE.stat().st_mtime
        if age < 4 * 3600:
            return json.loads(VIX_CACHE_FILE.read_text()).get("vix")
    return _fetch_vix()


def _load_session_cache() -> dict[str, dict]:
    """Load live indicator cache written by Robinhood MCP session fetch.
    Returns {symbol: {ema20_daily, atr14_4hour}} or {} if missing/stale.
    """
    if not INDICATOR_CACHE.exists():
        return {}
    age = time.time() - INDICATOR_CACHE.stat().st_mtime
    if age > CACHE_MAX_AGE:
        return {}
    data = json.loads(INDICATOR_CACHE.read_text())
    return data.get("symbols", {})


def _load_session_universe() -> list[str] | None:
    """Load options-filtered symbol list from session. Returns None if missing/stale."""
    if not SESSION_UNIVERSE.exists():
        return None
    age = time.time() - SESSION_UNIVERSE.stat().st_mtime
    if age > CACHE_MAX_AGE:
        return None
    return SESSION_UNIVERSE.read_text().split()

STARTING_EQUITY = 2500.0


def _load_equity() -> dict:
    """Load equity state, creating it from scratch if missing."""
    if EQUITY_FILE.exists():
        return json.loads(EQUITY_FILE.read_text())
    return {"starting_equity": STARTING_EQUITY, "capital_in_use": 0.0}


def _save_equity(state: dict) -> None:
    EQUITY_FILE.write_text(json.dumps(state, indent=2))


def _recompute_equity() -> dict:
    """Recompute capital_in_use from the live ledger and save."""
    ledger = _load_live_ledger()
    in_use = sum(
        t["entry"] * t.get("shares", 0)
        for t in ledger
        if t.get("status") == "entered"
    )
    state = _load_equity()
    state["capital_in_use"] = round(in_use, 2)
    state["available_equity"] = round(state["starting_equity"] - in_use, 2)
    _save_equity(state)
    return state


def _load_live_ledger() -> list[dict]:
    if LIVE_LEDGER.exists():
        return json.loads(LIVE_LEDGER.read_text())
    return []


def _save_live_ledger(trades: list[dict]) -> None:
    LIVE_LEDGER.write_text(json.dumps(trades, indent=2))


def _quality_score(setup: dict) -> float:
    """Score a triggered setup for capital-allocation priority (higher = better).

    Two equally-weighted components, each normalised to [0, 1]:
      pattern_depth  — (neckline - stop) / neckline; already >= 0.03 by scanner filter.
                       Deeper pattern = more room to run before the stop is threatened.
      freshness      — (12 - bars_since_break) / 12; peaks at 1 when the break just
                       happened, decays to ~0 at bar 11. Newer breaks retest sooner
                       and have less time to fail before the 12-bar window closes.
    """
    depth     = (setup["neckline"] - setup["stop"]) / setup["neckline"]
    freshness = (12 - setup.get("bars_since_break", 0)) / 12
    return round(depth * 0.5 + freshness * 0.5, 4)


def scan_symbol(
    symbol: str,
    equity: float,
    risk_pct: float = 0.02,
    indicator_cache: dict | None = None,
) -> dict:
    """Scan one symbol. Returns dict with 'watching' and 'triggered' lists.

    indicator_cache: optional {ema20_daily, atr14_4hour} for this symbol,
    fetched live from Robinhood API. When provided, replaces bar-computed values.
    """
    daily_path = DATA / f"{symbol}_day.json"
    h4_path    = DATA / f"{symbol}_4hour.json"
    if not daily_path.exists() or not h4_path.exists():
        return {"watching": [], "triggered": []}

    daily = load(daily_path, symbol)
    h4    = load(h4_path, symbol)
    if len(daily) < 30 or len(h4) < 30:
        return {"watching": [], "triggered": []}

    live_ema = indicator_cache.get("ema20_daily") if indicator_cache else None
    live_atr = indicator_cache.get("atr14_4hour") if indicator_cache else None

    # Bias: prefer live EMA from Robinhood; fall back to bar-computed series
    # Both paths enforce: close > 20 EMA AND 20 EMA > 50 SMA (confirmed uptrend)
    if live_ema is not None:
        last_close_daily = float(daily.iloc[-1]["close"])
        last_low_daily   = float(daily.iloc[-1]["low"])
        sma50_daily      = float(daily["close"].rolling(50).mean().iloc[-1])
        current_bias     = (
            (last_close_daily > live_ema)
            and (last_low_daily > live_ema)
            and (live_ema > sma50_daily)
        )
        bias = None  # will use current_bias for asof check
    else:
        bias = daily_bias_series(daily)

    atr_series = atr(h4, 14)
    ema9_series = ema(h4["close"], 9)
    patterns   = detect_double_bottom(h4) + detect_inverse_hns(h4) + detect_cup_and_handle(h4)
    patterns.sort(key=lambda p: p["break_index"])

    last_bar   = len(h4) - 1
    neckline   = None
    watching   = []
    triggered  = []

    for p in patterns:
        bi = p["break_index"]
        # only patterns whose break is within the last 12 bars
        if bi < last_bar - 11:
            continue
        # Bias check: live EMA uses current daily bias; fallback uses series
        if bias is None:
            if not current_bias:
                continue
        elif not bias_asof(bias, p["break_time"]):
            continue

        if (p["neckline"] - p["stop_basis"]) / p["neckline"] < 0.03:
            continue

        trade = build_trade(h4, p, atr_series, equity, risk_pct,
                            atr_override=live_atr)
        if trade is None:
            continue

        neckline   = p["neckline"]
        bars_since = last_bar - bi
        last_close = float(h4.loc[last_bar, "close"])
        last_low   = float(h4.loc[last_bar, "low"])
        last_open  = float(h4.loc[last_bar, "open"])

        # check if we're in a pullback zone already
        in_pullback = any(
            float(h4.loc[i, "low"]) <= neckline * 1.005
            for i in range(bi + 1, last_bar + 1)
        )

        # 9 EMA on 4h: price should be in the "bull area" (above the 9 EMA) at trigger
        ema9_now = float(ema9_series.iloc[last_bar])

        # check if today's bar IS the retest trigger
        triggered_today = (
            in_pullback
            and last_close >= neckline
            and last_close > last_open
            and last_close >= ema9_now  # short-term momentum intact (Noah's bull area)
        )

        setup = {
            "symbol":        symbol,
            "type":          p["type"],
            "neckline":      round(neckline, 4),
            "stop":          round(trade["stop"], 4),
            "target_1R":     round(trade["target_1R"], 4),
            "target_2R":     round(trade["target_2R"], 4),
            "risk_per_share": round(trade["risk_per_share"], 4),
            "shares":        round(trade["shares"], 4),
            "break_time":    str(p["break_time"]),
            "bars_since_break": bars_since,
            "in_pullback":   in_pullback,
            "last_close":    round(last_close, 4),
            "last_bar_time": str(h4.loc[last_bar, "begins_at"]),
            "scanned_at":    datetime.now(timezone.utc).isoformat(),
        }

        if triggered_today:
            setup["entry"]       = round(last_close, 4)
            setup["entry_time"]  = str(h4.loc[last_bar, "begins_at"])
            setup["status"]      = "entered"
            setup["quality_score"] = _quality_score(setup)
            triggered.append(setup)
        else:
            setup["status"] = "watching"
            watching.append(setup)

    return {"watching": watching, "triggered": triggered}


def run_scan(symbols: list[str], risk_pct: float = 0.02) -> dict:
    REPORTS.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Session overrides: options-filtered universe and live indicator cache
    session_syms = _load_session_universe()
    if session_syms is not None:
        original_count = len(symbols)
        symbols = [s for s in session_syms if s in set(symbols)]
        print(f"  [Session] Options pre-filter: {original_count} → {len(symbols)} symbols")

    indicator_cache = _load_session_cache()
    if indicator_cache:
        print(f"  [Session] Live indicator cache loaded for {len(indicator_cache)} symbols")

    # Sector ETF pre-filter — only scan symbols in bullish sectors
    sector_bias = _load_sector_etf_bias()
    bearish_etfs = [e for e, bull in sector_bias.items() if not bull]
    if bearish_etfs:
        print(f"  [Sector] Bearish ETFs: {', '.join(sorted(bearish_etfs))}")
    original_count = len(symbols)
    symbols = [
        s for s in symbols
        if sector_bias.get(SECTOR_ETF_MAP.get(s, ""), True)  # unmapped symbols pass through
    ]
    filtered_out = original_count - len(symbols)
    if filtered_out:
        print(f"  [Sector] Filtered {filtered_out} symbol(s) in bearish sectors "
              f"({original_count} → {len(symbols)})")

    # VIX market context gate — skip new entries when volatility is elevated
    vix = _load_vix()
    high_vix = vix is not None and vix >= VIX_HIGH_THRESHOLD
    if vix is not None:
        flag = " ⚠ HIGH VIX — new entries blocked" if high_vix else ""
        print(f"  [VIX] {vix:.1f}{flag}")
    else:
        print("  [VIX] unavailable — proceeding without gate")
        high_vix = False

    # Recompute equity state from ledger before scanning
    equity_state = _recompute_equity()
    available    = equity_state["available_equity"]
    starting     = equity_state["starting_equity"]
    print(f"  Equity: ${starting:.2f} starting  |  "
          f"${equity_state['capital_in_use']:.2f} in use  |  "
          f"${available:.2f} available")

    all_watching  = []
    all_triggered = []

    for sym in symbols:
        # Use full starting equity for position sizing (risk % of starting capital)
        # but gate entry on available equity
        result = scan_symbol(sym, starting, risk_pct,
                             indicator_cache=indicator_cache.get(sym))
        all_watching.extend(result["watching"])
        all_triggered.extend(result["triggered"])

    # ── Watchlist ledger: upsert every watching setup ──────────────────────────
    for s in all_watching:
        upsert_watching(s)

    # Expire setups that aged out of the 12-bar window this scan
    watching_ids  = {(s["symbol"], s["type"], s["break_time"]) for s in all_watching}
    triggered_ids = {(s["symbol"], s["type"], s["break_time"]) for s in all_triggered}
    expired = expire_stale(watching_ids, triggered_ids)
    if expired:
        print(f"  [Watchlist] Expired {len(expired)} stale setup(s): {', '.join(expired)}")

    # ── Merge triggered entries — only add if we have enough capital ────────────────────────
    # Sort by quality score descending so the best setups get capital first.
    # Existing ledger entries (already opened) are processed first to avoid
    # double-counting them against available equity.
    ledger = _load_live_ledger()
    existing_keys = {(t["symbol"], t.get("entry_time")) for t in ledger}
    new_entries   = []
    skipped       = []

    already_open  = [t for t in all_triggered if (t["symbol"], t.get("entry_time")) in existing_keys]
    new_candidates = sorted(
        [t for t in all_triggered if (t["symbol"], t.get("entry_time")) not in existing_keys],
        key=lambda t: t.get("quality_score", 0),
        reverse=True,
    )
    if new_candidates:
        top = new_candidates[0]
        print(f"  [Rank] {len(new_candidates)} new trigger(s) ranked by quality score — "
              f"top: {top['symbol']} {top['type']} score={top.get('quality_score', 0):.4f}")

    for t in already_open + new_candidates:
        if (t["symbol"], t.get("entry_time")) in existing_keys:
            # already in ledger — still update watchlist state if not yet done
            mark_triggered(t["symbol"], t["type"], t["break_time"],
                           t["entry"], t["entry_time"])
            continue
        if high_vix:
            skipped.append({"symbol": t["symbol"], "type": t["type"],
                            "cost": round(t["entry"] * t.get("shares", 0), 2),
                            "available": round(available, 2),
                            "quality_score": t.get("quality_score", 0),
                            "skip_reason": "high_vix"})
            mark_missed(t["symbol"], t["type"], t["break_time"], reason="high_vix")
            continue
        cost = round(t["entry"] * t.get("shares", 0), 2)
        if cost > available:
            skipped.append({"symbol": t["symbol"], "type": t["type"],
                            "cost": cost, "available": round(available, 2),
                            "quality_score": t.get("quality_score", 0),
                            "skip_reason": "no_capital"})
            mark_missed(t["symbol"], t["type"], t["break_time"], reason="no_capital")
            continue
        new_entries.append(t)
        available = round(available - cost, 2)
        mark_triggered(t["symbol"], t["type"], t["break_time"],
                       t["entry"], t["entry_time"])

    ledger.extend(new_entries)
    _save_live_ledger(ledger)

    # Recompute and save final equity state
    final_state = _recompute_equity()

    wl_summary = watchlist_summary()
    report = {
        "scan_date":       today,
        "vix":             vix,
        "high_vix":        high_vix,
        "sector_bias":     sector_bias,
        "watching":        sorted(all_watching, key=lambda x: x["bars_since_break"]),
        "triggered_today": sorted(all_triggered,
                                   key=lambda x: x.get("quality_score", 0), reverse=True),
        "new_entries":     len(new_entries),
        "skipped":         skipped,
        "total_open":      len([t for t in ledger if t.get("status") == "entered"]),
        "equity": {
            "starting":   final_state["starting_equity"],
            "in_use":     final_state["capital_in_use"],
            "available":  final_state["available_equity"],
        },
        "watchlist": wl_summary,
    }
    report_path = REPORTS / f"scan_{today}.json"
    report_path.write_text(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    from swing_agent.fetch_yf import _load_universe, fetch_daily, fetch_4hour, save as yf_save

    syms = _load_universe()
    print("=== DATA REFRESH ===")
    daily_data = fetch_daily(syms)
    yf_save(daily_data, "_day")
    h4_data = fetch_4hour(syms)
    yf_save(h4_data, "_4hour")
    print(f"  Refreshed {len(syms)} symbols\n")

    result = run_scan(syms)

    print(f"\n{'='*60}")
    print(f"SCAN: {result['scan_date']}  |  {len(syms)} symbols")
    print(f"{'='*60}")
    print(f"Watching (retest pending):  {len(result['watching'])}")
    print(f"Triggered today (entered):  {len(result['triggered_today'])}")
    print(f"Total open paper positions: {result['total_open']}")

    if result["watching"]:
        print(f"\n--- WATCHLIST (neckline retest pending) ---")
        for s in result["watching"]:
            pullback = "IN ZONE" if s["in_pullback"] else "waiting"
            print(f"  {s['symbol']:<6} {s['type']:<14} "
                  f"neckline={s['neckline']} stop={s['stop']} 2R={s['target_2R']} "
                  f"bars_since_break={s['bars_since_break']} [{pullback}]")

    if result["triggered_today"]:
        print(f"\n--- ENTRIES TODAY ---")
        for s in result["triggered_today"]:
            print(f"  {s['symbol']:<6} {s['type']:<14} "
                  f"entry={s['entry']} stop={s['stop']} 2R={s['target_2R']} "
                  f"shares={s['shares']} score={s.get('quality_score', 0):.4f}")
    else:
        print(f"\n  No entries triggered today.")
