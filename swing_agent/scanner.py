"""Live forward scanner — paper trading only, no real orders.

Scans all universe symbols for active retest setups and logs proposed
paper trades to data/paper_trades_live.json. Run once per day after close.

A setup is "live" when:
  1. A qualifying pattern broke its neckline within the last 12 4h bars.
  2. Daily bias is long at the time of the break.
  3. Pattern depth in [3%, 12%] — too shallow misses structure, too wide means a distant stop that bleeds slowly.
  4. EMA20 > SMA50 > SMA200 on daily — confirms full multi-timeframe uptrend.
  5. Stock 20-day return > SPY 20-day return — relative strength confirms leadership.
  6. Break bar volume >= 1.2× 20-bar average — high-conviction neckline break.
  7. Quality score >= MIN_QUALITY_SCORE — filters marginal setups.
  8. The retest has NOT yet triggered (still watching) OR just triggered today.
  9. At most MAX_ENTRIES_PER_DAY new positions opened per scan.

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
    purge_legacy_entries,
)

ROOT        = Path(__file__).resolve().parent.parent
DATA        = ROOT / "data"
REPORTS     = ROOT / "reports"
LIVE_LEDGER = DATA / "paper_trades_live.json"
EQUITY_FILE = DATA / "equity.json"

SESSION_UNIVERSE = DATA / "session_universe.txt"
INDICATOR_CACHE  = DATA / "indicator_cache.json"
CACHE_MAX_AGE    = 8 * 3600  # 8 hours

VIX_HIGH_THRESHOLD  = 25.0  # skip new entries when VIX is elevated
VIX_CACHE_FILE      = DATA / "vix_cache.json"
SPY_RS_CACHE_FILE   = DATA / "spy_rs_cache.json"
SPY_RS_CACHE_AGE    = 8 * 3600
SPY_REGIME_CACHE_FILE = DATA / "spy_regime_cache.json"
SPY_REGIME_CACHE_AGE  = 8 * 3600
SPY_EMA_PERIOD        = 20  # daily bars

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

# Sector momentum stack thresholds
# Score 0–3: weekly EMA20 (1pt) + daily EMA20 (1pt) + daily EMA8 (1pt)
SECTOR_SCORE_MIN      = 2   # score < 2 → skip entirely
SECTOR_SCORE_FULL     = 3   # score == 3 → full scan; score == 2 → capped entries


def _ema(closes: list[float], period: int) -> float:
    """Compute EMA of the last values in closes over the given period."""
    k = 2 / (period + 1)
    val = closes[0]
    for c in closes[1:]:
        val = c * k + val * (1 - k)
    return val


def _load_sector_etf_bias() -> dict[str, int]:
    """Return {etf: score} for all sector ETFs, using cache when fresh.

    Score 0–3 based on three momentum checks (far-to-near):
      +1  Weekly EMA(20): weekly closes above 20-week EMA  → long-term uptrend
      +1  Daily  EMA(20): daily close above 20-day EMA     → medium-term uptrend
      +1  Daily  EMA(8):  daily close above 8-day EMA      → near-term thrust

    Falls back to score=3 (allow through) when data is unavailable.
    """
    if SECTOR_ETF_CACHE_FILE.exists():
        age = time.time() - SECTOR_ETF_CACHE_FILE.stat().st_mtime
        if age < SECTOR_ETF_CACHE_AGE:
            cached = json.loads(SECTOR_ETF_CACHE_FILE.read_text())
            # Migrate old bool cache to int scores transparently
            if cached and isinstance(next(iter(cached.values())), bool):
                cached = {k: (3 if v else 0) for k, v in cached.items()}
            return cached

    etfs = set(SECTOR_ETF_MAP.values())
    result: dict[str, int] = {}
    for etf in sorted(etfs):
        try:
            # Fetch ~2 years of daily bars to derive weekly closes and all EMAs
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{etf}"
                   f"?interval=1d&range=500d")
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            closes_raw = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            timestamps = data["chart"]["result"][0]["timestamps"]
            closes_raw = [c for c in closes_raw if c is not None]
            if len(closes_raw) < 40:
                result[etf] = 3  # not enough data — allow through
                continue

            price = closes_raw[-1]
            score = 0

            # Daily EMA(20) and EMA(8)
            if len(closes_raw) >= 20:
                d_ema20 = _ema(closes_raw[-60:], 20)
                if price > d_ema20:
                    score += 1
            if len(closes_raw) >= 8:
                d_ema8 = _ema(closes_raw[-30:], 8)
                if price > d_ema8:
                    score += 1

            # Weekly EMA(20): resample daily closes to weekly (Friday close)
            import datetime as _dt
            weekly_closes: list[float] = []
            week_closes_tmp: list[float] = []
            for ts, c in zip(timestamps, closes_raw):
                if c is None:
                    continue
                dow = _dt.datetime.utcfromtimestamp(ts).weekday()  # 0=Mon, 4=Fri
                week_closes_tmp.append(c)
                if dow == 4:  # Friday — record week close
                    weekly_closes.append(week_closes_tmp[-1])
                    week_closes_tmp = []
            if week_closes_tmp:  # partial current week
                weekly_closes.append(week_closes_tmp[-1])
            if len(weekly_closes) >= 20:
                w_ema20 = _ema(weekly_closes[-40:], 20)
                if weekly_closes[-1] > w_ema20:
                    score += 1

            result[etf] = score
        except Exception:
            result[etf] = 3  # fetch failed — allow through

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


def _fetch_spy_20d_return() -> float | None:
    """Return SPY's 20-day price return, cached for 8 hours. Returns None on failure."""
    if SPY_RS_CACHE_FILE.exists():
        age = time.time() - SPY_RS_CACHE_FILE.stat().st_mtime
        if age < SPY_RS_CACHE_AGE:
            return json.loads(SPY_RS_CACHE_FILE.read_text()).get("return_20d")
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/SPY?interval=1d&range=35d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if len(closes) < 21:
            return None
        ret = (closes[-1] - closes[-21]) / closes[-21]
        SPY_RS_CACHE_FILE.write_text(json.dumps({"return_20d": ret, "fetched_at": time.time()}))
        return ret
    except Exception:
        return None


def _fetch_spy_ema_bias() -> bool | None:
    """Return True if SPY close > 20-day EMA (bull regime), False if not, None on failure.
    Result cached for 8 hours to avoid repeated Yahoo fetches.
    """
    if SPY_REGIME_CACHE_FILE.exists():
        age = time.time() - SPY_REGIME_CACHE_FILE.stat().st_mtime
        if age < SPY_REGIME_CACHE_AGE:
            val = json.loads(SPY_REGIME_CACHE_FILE.read_text()).get("bull_regime")
            if val is not None:
                return bool(val)
    try:
        bars_needed = SPY_EMA_PERIOD + 5
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/SPY"
               f"?interval=1d&range={bars_needed}d")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        closes = [c for c in closes if c is not None]
        if len(closes) < SPY_EMA_PERIOD:
            return None
        # Compute EMA(20) using standard multiplier
        k = 2 / (SPY_EMA_PERIOD + 1)
        ema_val = sum(closes[:SPY_EMA_PERIOD]) / SPY_EMA_PERIOD
        for c in closes[SPY_EMA_PERIOD:]:
            ema_val = c * k + ema_val * (1 - k)
        bull = closes[-1] > ema_val
        SPY_REGIME_CACHE_FILE.write_text(
            json.dumps({"bull_regime": bull, "spy_close": closes[-1],
                        "spy_ema20": round(ema_val, 4), "fetched_at": time.time()})
        )
        return bull
    except Exception:
        return None


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

from swing_agent.config import (
    STARTING_EQUITY, ENTRY_TIMEFRAME, FRESHNESS_BARS,
    MIN_QUALITY_SCORE, MAX_ENTRIES_PER_DAY,
)


def _load_equity() -> dict:
    """Load equity state, creating it from scratch if missing."""
    if EQUITY_FILE.exists():
        return json.loads(EQUITY_FILE.read_text())
    return {"starting_equity": STARTING_EQUITY, "capital_in_use": 0.0}


def _save_equity(state: dict) -> None:
    EQUITY_FILE.write_text(json.dumps(state, indent=2))


def _recompute_equity() -> dict:
    """Recompute capital_in_use from the live ledger and save.

    Counts both entered positions (at actual fill price) and pending_fill
    positions (at signal_price estimate) so available_equity stays accurate
    between the evening scan and next morning's open resolution.
    """
    ledger = _load_live_ledger()
    in_use = sum(
        t["entry"] * t.get("shares", 0)
        for t in ledger
        if t.get("status") == "entered"
    ) + sum(
        t.get("signal_price", 0) * t.get("shares", 0)
        for t in ledger
        if t.get("status") == "pending_fill"
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
    freshness = (FRESHNESS_BARS - setup.get("bars_since_break", 0)) / FRESHNESS_BARS
    return round(depth * 0.5 + freshness * 0.5, 4)


def scan_symbol(
    symbol: str,
    equity: float,
    risk_pct: float = 0.02,
    indicator_cache: dict | None = None,
    spy_20d_return: float | None = None,
) -> dict:
    """Scan one symbol. Returns dict with 'watching' and 'triggered' lists.

    indicator_cache: optional {ema20_daily, atr14_4hour} for this symbol,
    fetched live from Robinhood API. When provided, replaces bar-computed values.
    spy_20d_return: optional SPY 20-day return for relative-strength filter.
    """
    daily_path = DATA / f"{symbol}_day.json"
    h4_path    = DATA / f"{symbol}_{ENTRY_TIMEFRAME}.json"
    if not daily_path.exists() or not h4_path.exists():
        return {"watching": [], "triggered": []}

    daily = load(daily_path, symbol)
    h4    = load(h4_path, symbol)
    if len(daily) < 30 or len(h4) < 30:
        return {"watching": [], "triggered": []}

    live_ema = indicator_cache.get("ema20_daily") if indicator_cache else None
    live_atr = indicator_cache.get(f"atr14_{ENTRY_TIMEFRAME}") if indicator_cache else None

    # Bias: prefer live EMA from Robinhood; fall back to bar-computed series
    # Both paths enforce: close > 20 EMA AND 20 EMA > 50 SMA (confirmed uptrend)
    if live_ema is not None:
        last_close_daily = float(daily.iloc[-1]["close"])
        last_low_daily   = float(daily.iloc[-1]["low"])
        sma50_daily      = float(daily["close"].rolling(50).mean().iloc[-1])
        sma200_daily     = float(daily["close"].rolling(200).mean().iloc[-1]) if len(daily) >= 200 else 0.0
        current_bias     = (
            (last_close_daily > live_ema)
            and (last_low_daily > live_ema)
            and (live_ema > sma50_daily)
            and (sma50_daily > sma200_daily)  # EMA stack: 20 > 50 > 200
        )
        bias = None  # will use current_bias for asof check
    else:
        bias = daily_bias_series(daily)
        # EMA stack filter for fallback path: 20 EMA > SMA50 > SMA200
        if len(daily) >= 200:
            ema20_d  = float(ema(daily["close"], 20).iloc[-1])
            sma50_d  = float(daily["close"].rolling(50).mean().iloc[-1])
            sma200_d = float(daily["close"].rolling(200).mean().iloc[-1])
            if not (ema20_d > sma50_d > sma200_d):
                return {"watching": [], "triggered": []}

    # Relative strength vs SPY: stock must have outperformed over last 20 days
    if spy_20d_return is not None and len(daily) >= 21:
        stock_20d = (float(daily.iloc[-1]["close"]) - float(daily.iloc[-21]["close"])) / float(daily.iloc[-21]["close"])
        if stock_20d < spy_20d_return:
            return {"watching": [], "triggered": []}

    atr_series = atr(h4, 14)
    ema9_series = ema(h4["close"], 9)
    patterns   = detect_inverse_hns(h4) + detect_cup_and_handle(h4)  # double_bottom suspended
    patterns.sort(key=lambda p: p["break_index"])

    last_bar   = len(h4) - 1
    neckline   = None
    watching   = []
    triggered  = []

    for p in patterns:
        bi = p["break_index"]
        # only patterns whose break is within the last FRESHNESS_BARS bars
        if bi < last_bar - (FRESHNESS_BARS - 1):
            continue
        # Bias check: live EMA uses current daily bias; fallback uses series
        if bias is None:
            if not current_bias:
                continue
        elif not bias_asof(bias, p["break_time"]):
            continue

        # Pre-check: pattern structure depth (neckline to stop_basis, no ATR buffer).
        # This rejects obviously shallow or obviously wide patterns before building
        # the full trade. The effective stop (stop_basis - 1.5×ATR) is checked again
        # below after build_trade, using the actual stop level.
        struct_depth = (p["neckline"] - p["stop_basis"]) / p["neckline"]
        if struct_depth < 0.03 or struct_depth > 0.15:  # wider pre-filter to not over-block
            continue

        # Volume confirmation: break bar must have >= 1.2× its 20-bar average volume
        if bi >= 20 and "volume" in h4.columns:
            avg_vol = float(h4["volume"].rolling(20).mean().iloc[bi])
            if avg_vol > 0 and not pd.isna(avg_vol):
                if float(h4.loc[bi, "volume"]) < 1.2 * avg_vol:
                    continue

        trade = build_trade(h4, p, atr_series, equity, risk_pct,
                            atr_override=live_atr)
        if trade is None:
            continue

        # Definitive depth gate: use the actual ATR-buffered stop, not stop_basis.
        # This is the number that goes in the ledger and drives real risk.
        depth = (p["neckline"] - trade["stop"]) / p["neckline"]
        if depth < 0.03 or depth > 0.12:
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
            # Record the 4H close that confirmed the retest as signal_price.
            # The actual entry price is NOT known yet — it will be the next
            # morning's opening quote, resolved by resolve_pending_fills().
            setup["signal_price"] = round(last_close, 4)
            setup["signal_time"]  = str(h4.loc[last_bar, "begins_at"])
            setup["status"]       = "pending_fill"
            setup["quality_score"] = _quality_score(setup)
            if setup["quality_score"] < MIN_QUALITY_SCORE:
                setup["status"]      = "watching"
                setup["skip_reason"] = "low_quality"
                watching.append(setup)
            else:
                triggered.append(setup)
        else:
            setup["status"] = "watching"
            setup["quality_score"] = _quality_score(setup)
            watching.append(setup)

    return {"watching": watching, "triggered": triggered}


def run_scan(symbols: list[str], risk_pct: float = 0.02) -> dict:
    REPORTS.mkdir(exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Remove stale legacy watchlist entries (no scanner data) on first run of the day
    purge_legacy_entries()

    # Session overrides: options-filtered universe and live indicator cache
    session_syms = _load_session_universe()
    if session_syms is not None:
        original_count = len(symbols)
        symbols = [s for s in session_syms if s in set(symbols)]
        print(f"  [Session] Options pre-filter: {original_count} → {len(symbols)} symbols")

    indicator_cache = _load_session_cache()
    if indicator_cache:
        print(f"  [Session] Live indicator cache loaded for {len(indicator_cache)} symbols")

    # Sector ETF momentum stack — score 0-3 (weekly EMA20 + daily EMA20 + daily EMA8)
    sector_bias = _load_sector_etf_bias()
    for score_label, threshold in [(3, "Strong (3/3)"), (2, "Moderate (2/3)"),
                                    (1, "Weak (1/3)"), (0, "Bearish (0/3)")]:
        etfs_at = sorted(e for e, s in sector_bias.items() if s == score_label)
        if etfs_at:
            print(f"  [Sector] {threshold}: {', '.join(etfs_at)}")
    original_count = len(symbols)
    symbols = [
        s for s in symbols
        if sector_bias.get(SECTOR_ETF_MAP.get(s, ""), SECTOR_SCORE_FULL) >= SECTOR_SCORE_MIN
    ]
    filtered_out = original_count - len(symbols)
    if filtered_out:
        print(f"  [Sector] Filtered {filtered_out} symbol(s) with sector score < {SECTOR_SCORE_MIN} "
              f"({original_count} → {len(symbols)})")

    # Tag each symbol with its sector score for downstream priority
    symbol_sector_score: dict[str, int] = {
        s: sector_bias.get(SECTOR_ETF_MAP.get(s, ""), SECTOR_SCORE_FULL)
        for s in symbols
    }

    # Relative strength benchmark: SPY 20-day return
    spy_return = _fetch_spy_20d_return()
    if spy_return is not None:
        print(f"  [RS] SPY 20d return: {spy_return*100:.1f}% — stocks must beat this to qualify")
    else:
        print("  [RS] SPY return unavailable — relative strength filter bypassed")

    # VIX market context gate — skip new entries when volatility is elevated
    vix = _load_vix()
    high_vix = vix is not None and vix >= VIX_HIGH_THRESHOLD
    if vix is not None:
        flag = " ⚠ HIGH VIX — new entries blocked" if high_vix else ""
        print(f"  [VIX] {vix:.1f}{flag}")
    else:
        print("  [VIX] unavailable — proceeding without gate")
        high_vix = False

    # SPY regime gate — skip new entries when SPY is below its 20-day EMA
    spy_bull = _fetch_spy_ema_bias()
    bear_regime = spy_bull is False  # None (unavailable) passes through
    if spy_bull is True:
        print("  [Regime] SPY above 20-day EMA — bull regime confirmed")
    elif spy_bull is False:
        print("  [Regime] SPY below 20-day EMA ⚠ BEAR REGIME — new entries blocked")
    else:
        print("  [Regime] SPY EMA unavailable — regime gate bypassed")

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
                             indicator_cache=indicator_cache.get(sym),
                             spy_20d_return=spy_return)
        sec_score = symbol_sector_score.get(sym, SECTOR_SCORE_FULL)
        for s in result["watching"] + result["triggered"]:
            s["sector_score"] = sec_score
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
    # Dedup key uses signal_time (the 4H bar that confirmed the retest).
    # For legacy entered trades that predate this change, fall back to entry_time.
    existing_keys = {
        (t["symbol"], t.get("signal_time", t.get("entry_time"))) for t in ledger
    }
    # Block a second signal on any symbol that's already entered or awaiting fill.
    symbols_entered = {
        t["symbol"] for t in ledger
        if t.get("status") in ("entered", "pending_fill")
    }
    new_entries   = []
    skipped       = []

    already_open = [
        t for t in all_triggered
        if (t["symbol"], t.get("signal_time")) in existing_keys
    ]
    new_candidates = sorted(
        [t for t in all_triggered
         if (t["symbol"], t.get("signal_time")) not in existing_keys],
        key=lambda t: (t.get("sector_score", SECTOR_SCORE_FULL), t.get("quality_score", 0)),
        reverse=True,
    )
    if new_candidates:
        top = new_candidates[0]
        print(f"  [Rank] {len(new_candidates)} new trigger(s) ranked by sector score then "
              f"quality score — top: {top['symbol']} {top['type']} "
              f"sector={top.get('sector_score', SECTOR_SCORE_FULL)}/3 "
              f"score={top.get('quality_score', 0):.4f}")

    entries_today = 0
    for t in already_open + new_candidates:
        if (t["symbol"], t.get("signal_time")) in existing_keys:
            # already in ledger — update watchlist state
            mark_triggered(t["symbol"], t["type"], t["break_time"],
                           t["signal_price"], t["signal_time"])
            continue
        if t["symbol"] in symbols_entered:
            skipped.append({"symbol": t["symbol"], "type": t["type"],
                            "quality_score": t.get("quality_score", 0),
                            "skip_reason": "one_per_symbol"})
            mark_missed(t["symbol"], t["type"], t["break_time"], reason="one_per_symbol")
            continue
        if high_vix:
            skipped.append({"symbol": t["symbol"], "type": t["type"],
                            "quality_score": t.get("quality_score", 0),
                            "skip_reason": "high_vix"})
            mark_missed(t["symbol"], t["type"], t["break_time"], reason="high_vix")
            continue
        if bear_regime:
            skipped.append({"symbol": t["symbol"], "type": t["type"],
                            "quality_score": t.get("quality_score", 0),
                            "skip_reason": "bear_regime"})
            mark_missed(t["symbol"], t["type"], t["break_time"], reason="bear_regime")
            continue
        # Capital pre-check uses signal_price * shares as a rough estimate.
        # Actual cost is recomputed at fill time from the morning quote.
        est_cost = round(t["signal_price"] * t.get("shares", 0), 2)
        if est_cost > available:
            skipped.append({"symbol": t["symbol"], "type": t["type"],
                            "est_cost": est_cost, "available": round(available, 2),
                            "quality_score": t.get("quality_score", 0),
                            "skip_reason": "no_capital"})
            mark_missed(t["symbol"], t["type"], t["break_time"], reason="no_capital")
            continue
        if entries_today >= MAX_ENTRIES_PER_DAY:
            skipped.append({"symbol": t["symbol"], "type": t["type"],
                            "quality_score": t.get("quality_score", 0),
                            "skip_reason": "daily_cap"})
            mark_missed(t["symbol"], t["type"], t["break_time"], reason="daily_cap")
            continue
        # Moderate sector (2/3) — cap at 1 entry per day regardless of global cap
        if t.get("sector_score", SECTOR_SCORE_FULL) < SECTOR_SCORE_FULL and entries_today >= 1:
            skipped.append({"symbol": t["symbol"], "type": t["type"],
                            "quality_score": t.get("quality_score", 0),
                            "sector_score": t.get("sector_score"),
                            "skip_reason": "sector_cap"})
            mark_missed(t["symbol"], t["type"], t["break_time"], reason="sector_cap")
            continue
        new_entries.append(t)
        entries_today += 1
        available = round(available - est_cost, 2)
        symbols_entered.add(t["symbol"])
        mark_triggered(t["symbol"], t["type"], t["break_time"],
                       t["signal_price"], t["signal_time"])

    ledger.extend(new_entries)
    _save_live_ledger(ledger)

    # Recompute and save final equity state
    final_state = _recompute_equity()

    wl_summary = watchlist_summary()
    report = {
        "scan_date":       today,
        "vix":             vix,
        "high_vix":        high_vix,
        "sector_scores":   sector_bias,
        "watching":        sorted(all_watching, key=lambda x: x["bars_since_break"]),
        "triggered_today": sorted(all_triggered,
                                   key=lambda x: (x.get("sector_score", SECTOR_SCORE_FULL),
                                                  x.get("quality_score", 0)), reverse=True),
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

    # Write always-current scan summary — single file, overwritten every run
    spy_regime_str = "bull" if spy_bull is True else ("bear" if spy_bull is False else "unknown")
    watching_ranked = sorted(
        all_watching,
        key=lambda x: (x.get("sector_score", SECTOR_SCORE_FULL), x.get("quality_score", 0)),
        reverse=True,
    )
    summary = {
        "as_of":          datetime.now(timezone.utc).isoformat(),
        "scan_date":      today,
        "regime": {
            "spy_ema20":  spy_regime_str,
            "vix":        vix,
            "high_vix":   high_vix,
        },
        "sector_scores":  sector_bias,
        "watching": [
            {
                "symbol":        s["symbol"],
                "type":          s["type"],
                "sector_score":  s.get("sector_score", SECTOR_SCORE_FULL),
                "quality_score": round(s.get("quality_score", 0), 4),
                "bars_since_break": s.get("bars_since_break"),
                "neckline":      s.get("neckline"),
                "last_price":    s.get("last_close"),
                "stop":          s.get("stop"),
                "target_1R":     s.get("target_1R"),
                "target_2R":     s.get("target_2R"),
            }
            for s in watching_ranked
        ],
        "triggered_today": [
            {
                "symbol":        t["symbol"],
                "type":          t["type"],
                "sector_score":  t.get("sector_score", SECTOR_SCORE_FULL),
                "quality_score": round(t.get("quality_score", 0), 4),
                "signal_price":  t.get("signal_price"),
                "stop":          t.get("stop"),
                "target_1R":     t.get("target_1R"),
                "target_2R":     t.get("target_2R"),
            }
            for t in report["triggered_today"]
        ],
        "new_entries":    len(new_entries),
        "skipped":        skipped,
        "open_positions": report["total_open"],
        "equity":         report["equity"],
    }
    SCAN_SUMMARY_FILE = DATA / "scan_summary.json"
    SCAN_SUMMARY_FILE.write_text(json.dumps(summary, indent=2))
    print(f"  [Summary] scan_summary.json updated ({len(all_watching)} watching, "
          f"{len(report['triggered_today'])} triggered)")

    return report


if __name__ == "__main__":
    import sys as _sys
    from swing_agent.fetch_yf import _load_universe, fetch_daily, fetch_4hour, save as yf_save

    syms = _load_universe()

    # Skip data refresh when invoked via run_daily (avoids double-fetch).
    # Pass --refresh to force a refresh when running scanner standalone.
    if "--refresh" in _sys.argv:
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
            entry = s.get('entry') or s.get('signal_price', 'pending')
            print(f"  {s['symbol']:<6} {s['type']:<14} "
                  f"entry={entry} stop={s['stop']} 2R={s.get('target_2R', 'n/a')} "
                  f"shares={s.get('shares', 'n/a')} score={s.get('quality_score', 0):.4f}")
    else:
        print(f"\n  No entries triggered today.")
