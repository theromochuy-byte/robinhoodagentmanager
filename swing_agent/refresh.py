"""Daily data refresh coordinator.

Splits the universe into batches for parallel MCP fetching, then saves
each symbol's bars to data/<SYM>_day.json and data/<SYM>_4hour.json.

Two-step workflow:
  Step 1 (Claude, parallel): call get_equity_historicals for each batch,
          results auto-saved to tool-result files by the MCP runtime.
  Step 2 (Python):           python3 -m swing_agent.refresh <result_file> <interval>
          extracts per-symbol bars and writes data/<SYM>_<interval>.json.

Or run without args to print the fetch plan (batch lists + start times).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DAY_START = "2025-06-19T00:00:00Z"
H4_START  = "2026-03-19T00:00:00Z"
BATCH_SIZE = 10


def load_universe() -> list[str]:
    return (DATA / "universe.txt").read_text().split()


def make_batches(symbols: list[str], size: int = BATCH_SIZE) -> list[list[str]]:
    return [symbols[i : i + size] for i in range(0, len(symbols), size)]


def save_batch(result_path: str | Path, interval: str) -> tuple[list[str], list[str]]:
    """Parse an MCP result file and write per-symbol data files.

    Returns (saved_symbols, not_found_symbols).
    interval must be 'day' or '4hour'.
    """
    suffix = "_day" if interval == "day" else "_4hour"
    raw = json.loads(Path(result_path).read_text())
    results = raw.get("data", {}).get("results", [])
    not_found = raw.get("data", {}).get("not_found", [])

    saved = []
    for r in results:
        sym = r["symbol"]
        bars = r.get("bars", [])
        if not bars:
            continue
        out = DATA / f"{sym}{suffix}.json"
        out.write_text(json.dumps(bars))
        saved.append(sym)

    return saved, list(not_found)


def stale_symbols(symbols: list[str]) -> list[str]:
    """Return symbols missing either _day or _4hour data files."""
    missing = []
    for sym in symbols:
        if not (DATA / f"{sym}_day.json").exists() or not (DATA / f"{sym}_4hour.json").exists():
            missing.append(sym)
    return missing


def print_plan(symbols: list[str]) -> None:
    batches = make_batches(symbols)
    n = len(batches)
    print(f"Universe: {len(symbols)} symbols  |  {n} batches of up to {BATCH_SIZE}")
    print(f"\nFetch DAILY (interval=day, start={DAY_START}):")
    for i, b in enumerate(batches, 1):
        print(f"  [{i}/{n}] {b}")
    print(f"\nFetch 4-HOUR (interval=4hour, start={H4_START}):")
    for i, b in enumerate(batches, 1):
        print(f"  [{i}/{n}] {b}")


if __name__ == "__main__":
    args = sys.argv[1:]

    if not args:
        syms = load_universe()
        missing = stale_symbols(syms)
        if missing:
            print(f"Symbols missing data files ({len(missing)}): {missing}")
            print()
            print_plan(missing)
        else:
            print_plan(syms)
        sys.exit(0)

    if len(args) == 2:
        result_file, interval = args
        if interval not in ("day", "4hour"):
            print(f"interval must be 'day' or '4hour', got: {interval}", file=sys.stderr)
            sys.exit(1)
        saved, not_found = save_batch(result_file, interval)
        print(f"Saved ({len(saved)}): {saved}")
        if not_found:
            print(f"Not found on Robinhood: {not_found}", file=sys.stderr)
        sys.exit(0)

    print("Usage:")
    print("  python3 -m swing_agent.refresh                      # print fetch plan")
    print("  python3 -m swing_agent.refresh <result.json> day    # save daily bars")
    print("  python3 -m swing_agent.refresh <result.json> 4hour  # save 4-hour bars")
    sys.exit(1)
