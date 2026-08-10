"""Candlestick reversal recognition: hammer and bullish engulfing.

New to this codebase -- added for PROPOSAL_SWING_MA_CROSSOVER.md's Step 5
(candlestick confirmation on a pullback). Operates on a single OHLC
DataFrame row (or a pair, for the engulfing pattern) by index.
"""
from __future__ import annotations

import pandas as pd


def _body(df: pd.DataFrame, i: int) -> float:
    return abs(df.loc[i, "close"] - df.loc[i, "open"])


def _range(df: pd.DataFrame, i: int) -> float:
    return df.loc[i, "high"] - df.loc[i, "low"]


def _lower_wick(df: pd.DataFrame, i: int) -> float:
    return min(df.loc[i, "open"], df.loc[i, "close"]) - df.loc[i, "low"]


def _upper_wick(df: pd.DataFrame, i: int) -> float:
    return df.loc[i, "high"] - max(df.loc[i, "open"], df.loc[i, "close"])


def is_hammer(df: pd.DataFrame, i: int, min_lower_wick_ratio: float = 2.0, max_upper_wick_ratio: float = 0.5) -> bool:
    """Small body, long lower wick, little/no upper wick -- a reversal signal
    at the bottom of a pullback.
    """
    rng = _range(df, i)
    if rng <= 0:
        return False
    body = _body(df, i)
    lower = _lower_wick(df, i)
    upper = _upper_wick(df, i)
    if body == 0:
        body = rng * 0.001  # doji-bodied hammer still needs a genuinely long wick
    return lower >= min_lower_wick_ratio * body and upper <= max_upper_wick_ratio * body


def is_bullish_engulfing(df: pd.DataFrame, i: int) -> bool:
    """Prior bar bearish, this bar bullish, and this bar's body fully engulfs
    the prior bar's body."""
    if i < 1:
        return False
    prev_open, prev_close = df.loc[i - 1, "open"], df.loc[i - 1, "close"]
    this_open, this_close = df.loc[i, "open"], df.loc[i, "close"]
    prev_bearish = prev_close < prev_open
    this_bullish = this_close > this_open
    engulfs = this_open <= prev_close and this_close >= prev_open
    return prev_bearish and this_bullish and engulfs
