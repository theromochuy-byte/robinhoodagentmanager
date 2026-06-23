"""Daily automation pipeline: fetch → monitor → scan → commit.

Designed to be called by GitHub Actions on a schedule. Detects which run
it is (morning / midday / evening) from --mode and skips the full data
refresh for intraday checks.

Modes:
  morning   9:35 AM ET  full refresh (daily + 4h bars) + monitor + scan
  midday    1:00 PM ET  quotes-only monitor check
  evening   4:30 PM ET  full refresh + monitor + scan + final commit

Usage:
  python3 -m swing_agent.run_daily --mode morning
  python3 -m swing_agent.run_daily --mode midday
  python3 -m swing_agent.run_daily --mode evening
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    return result.stdout


def full_refresh():
    print("=== DATA REFRESH ===")
    run([sys.executable, "-m", "swing_agent.fetch", "--daily", "--intraday"])


def monitor_check():
    print("=== MONITOR CHECK ===")
    from swing_agent.fetch import _login, _load_open_symbols, fetch_quotes
    rh = _login()
    syms = _load_open_symbols()
    if not syms:
        print("No open positions.")
        return
    quotes = fetch_quotes(rh, syms)
    print(f"Live quotes: {quotes}")
    quotes_json = json.dumps(quotes)
    run([sys.executable, "-m", "swing_agent.monitor", "--check", quotes_json])


def run_scan():
    print("=== DAILY SCAN ===")
    run([sys.executable, "-m", "swing_agent.scanner"])


def git_commit_push(mode: str):
    print("=== COMMITTING ===")
    subprocess.run(["git", "add", "data/", "reports/"], cwd=ROOT)
    msg = f"auto: {mode} run — data refresh, monitor, scan"
    subprocess.run(["git", "commit", "-m", msg], cwd=ROOT)
    subprocess.run(
        ["git", "push", "-u", "origin", "claude/wonderful-cerf-sa7c0d"],
        cwd=ROOT,
    )


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--mode" not in args:
        print("Usage: python3 -m swing_agent.run_daily --mode [morning|midday|evening]")
        sys.exit(1)

    mode = args[args.index("--mode") + 1]

    if mode == "morning":
        full_refresh()
        monitor_check()
        run_scan()
        git_commit_push(mode)

    elif mode == "midday":
        monitor_check()
        git_commit_push(mode)

    elif mode == "evening":
        full_refresh()
        monitor_check()
        run_scan()
        git_commit_push(mode)

    else:
        print(f"Unknown mode: {mode}. Use morning, midday, or evening.", file=sys.stderr)
        sys.exit(1)
