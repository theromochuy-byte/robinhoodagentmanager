"""Persistent watchlist ledger.

Every setup that appears in a scan is tracked from first detection through
entry, exit, and close. Records price snapshots on every scan run, suggested
entry zones, all exit levels, and elapsed-time stamps at each lifecycle event.

Lifecycle states:
  watching  → setup detected, waiting for retest trigger
  triggered → retest entry fired, position is open in paper ledger
  closed    → position resolved (win / loss / open-expiry)
  expired   → setup disappeared from scan without triggering (>12 bars elapsed)
  missed    → setup triggered but was skipped (no capital)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT           = Path(__file__).resolve().parent.parent
DATA           = ROOT / "data"
WATCHLIST_FILE = DATA / "watchlist_ledger.json"


def _sid(symbol: str, pattern_type: str, break_time: str) -> str:
    return f"{symbol}__{pattern_type}__{break_time}"


def load_watchlist() -> list[dict]:
    if WATCHLIST_FILE.exists():
        raw = WATCHLIST_FILE.read_text().strip()
        if raw and raw != "PLACEHOLDER":
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    return data
            except json.JSONDecodeError:
                pass
        # File missing, corrupt, or placeholder — rebuild from scan history
        print("  [Watchlist] Ledger missing or invalid — rebuilding from scan reports...")
        return _rebuild_from_scans()
    return _rebuild_from_scans()


def _rebuild_from_scans() -> list[dict]:
    """Bootstrap the ledger by replaying all scan reports in chronological order."""
    REPORTS = ROOT / "reports"
    reports = sorted(REPORTS.glob("scan_*.json"))
    if not reports:
        save_watchlist([])
        return []

    entries: list[dict] = []
    WATCHLIST_FILE.write_text("[]")  # prevent recursion in sub-calls

    for rpt_path in reports:
        try:
            data = json.loads(rpt_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        date = data.get("scan_date", "")
        watching = data.get("watching", [])
        triggered = data.get("triggered_today", [])
        if not isinstance(watching, list) or not isinstance(triggered, list):
            continue

        for s in watching:
            if not isinstance(s, dict) or "break_time" not in s or "last_close" not in s:
                continue
            s2 = dict(s)
            if "target_1R" not in s2:
                s2["target_1R"] = round(s2["neckline"] + s2["risk_per_share"], 4)
            _upsert_into(entries, s2)

        for t in triggered:
            if not isinstance(t, dict) or "break_time" not in t:
                continue
            stub = dict(t)
            stub["last_close"] = stub.get("entry", stub.get("last_close", 0))
            stub["status"] = "watching"
            if "target_1R" not in stub:
                stub["target_1R"] = round(stub.get("entry", 0) + stub.get("risk_per_share", 0), 4)
            _upsert_into(entries, stub)
            _mark_triggered_in(entries, t["symbol"], t["type"], t["break_time"],
                               t.get("entry", 0),
                               t.get("entry_time", date + "T00:00:00+00:00"))

    # Compress: keep first_seen + one snapshot per calendar day
    for e in entries:
        ph = e.get("price_history", [])
        keepers = []
        seen_days: dict[str, dict] = {}
        for snap in ph:
            note = snap.get("note", "")
            if note in ("first_seen", "triggered", "closed_win", "closed_loss",
                        "expired", "missed_no_capital"):
                keepers.append(snap)
            else:
                seen_days[snap["time"][:10]] = snap
        keepers += list(seen_days.values())
        keepers.sort(key=lambda x: x["time"])
        e["price_history"] = keepers

    save_watchlist(entries)
    print(f"  [Watchlist] Rebuilt {len(entries)} entries from {len(reports)} scan reports.")
    return entries


def _upsert_into(entries: list[dict], setup: dict) -> None:
    sid = _sid(setup["symbol"], setup["type"], setup["break_time"])
    existing = next((e for e in entries if e["id"] == sid), None)
    now = setup.get("scanned_at", setup.get("last_bar_time", "2026-01-01T00:00:00+00:00"))
    price = setup["last_close"]
    if existing is None:
        nl = setup["neckline"]
        entries.append({
            "id":           sid,
            "symbol":       setup["symbol"],
            "type":         setup["type"],
            "neckline":     setup["neckline"],
            "break_time":   setup["break_time"],
            "first_seen":   now,
            "status":       "watching",
            "suggested_entry":      setup["neckline"],
            "suggested_entry_zone": [round(nl * 0.995, 4), round(nl * 1.005, 4)],
            "stop":         setup["stop"],
            "target_1R":    setup.get("target_1R"),
            "target_2R":    setup["target_2R"],
            "risk_per_share": setup["risk_per_share"],
            "price_history": [{"time": now, "price": price, "note": "first_seen"}],
            "trigger_time":  None, "trigger_price": None,
            "close_time":    None, "close_price":   None,
            "outcome": None, "elapsed_watching_hours": None, "elapsed_total_hours": None,
        })
    elif existing["status"] == "watching":
        existing["price_history"].append({"time": now, "price": price, "note": "update"})


def _mark_triggered_in(entries: list[dict], symbol: str, pattern_type: str,
                        break_time: str, entry_price: float, entry_time: str) -> None:
    sid = _sid(symbol, pattern_type, break_time)
    for e in entries:
        if e["id"] == sid and e["status"] == "watching":
            e["status"]        = "triggered"
            e["trigger_time"]  = entry_time
            e["trigger_price"] = entry_price
            e["elapsed_watching_hours"] = _hours_between(e["first_seen"], entry_time)
            e["price_history"].append({"time": entry_time, "price": entry_price,
                                       "note": "triggered"})
            break


def save_watchlist(entries: list[dict]) -> None:
    WATCHLIST_FILE.write_text(json.dumps(entries, indent=2))


def _hours_between(iso_a: str, iso_b: str) -> float:
    a = datetime.fromisoformat(iso_a.replace("Z", "+00:00"))
    b = datetime.fromisoformat(iso_b.replace("Z", "+00:00"))
    return round(abs((b - a).total_seconds()) / 3600, 1)


def upsert_watching(setup: dict) -> None:
    """Add a new watching setup or append a price snapshot to an existing one.

    Idempotent: called on every scan for every watching setup.
    """
    entries = load_watchlist()
    sid = _sid(setup["symbol"], setup["type"], setup["break_time"])
    existing = next((e for e in entries if e["id"] == sid), None)
    now = datetime.now(timezone.utc).isoformat()
    price = setup["last_close"]

    if existing is None:
        nl = setup["neckline"]
        entry_zone = [round(nl * 0.995, 4), round(nl * 1.005, 4)]
        entries.append({
            "id":           sid,
            "symbol":       setup["symbol"],
            "type":         setup["type"],
            "neckline":     setup["neckline"],
            "break_time":   setup["break_time"],
            "first_seen":   now,
            "status":       "watching",
            "suggested_entry":      setup["neckline"],
            "suggested_entry_zone": entry_zone,
            "stop":        setup["stop"],
            "target_1R":   setup.get("target_1R"),
            "target_2R":   setup["target_2R"],
            "risk_per_share": setup["risk_per_share"],
            "price_history": [{"time": now, "price": price, "note": "first_seen"}],
            "trigger_time":  None,
            "trigger_price": None,
            "close_time":    None,
            "close_price":   None,
            "outcome":       None,
            "elapsed_watching_hours": None,
            "elapsed_total_hours":    None,
        })
    elif existing["status"] == "watching":
        existing["price_history"].append({"time": now, "price": price, "note": "update"})

    save_watchlist(entries)


def mark_triggered(symbol: str, pattern_type: str, break_time: str,
                   entry_price: float, entry_time: str) -> None:
    """Record that a watching setup fired its retest entry."""
    entries = load_watchlist()
    sid = _sid(symbol, pattern_type, break_time)
    for e in entries:
        if e["id"] == sid and e["status"] == "watching":
            e["status"]        = "triggered"
            e["trigger_time"]  = entry_time
            e["trigger_price"] = entry_price
            e["elapsed_watching_hours"] = _hours_between(e["first_seen"], entry_time)
            e["price_history"].append({"time": entry_time, "price": entry_price,
                                       "note": "triggered"})
            break
    save_watchlist(entries)


def mark_missed(symbol: str, pattern_type: str, break_time: str,
                reason: str = "no_capital") -> None:
    """Record that a setup triggered but was skipped (no available capital)."""
    entries = load_watchlist()
    sid = _sid(symbol, pattern_type, break_time)
    now = datetime.now(timezone.utc).isoformat()
    for e in entries:
        if e["id"] == sid and e["status"] == "watching":
            e["status"] = "missed"
            e["elapsed_watching_hours"] = _hours_between(e["first_seen"], now)
            e["price_history"].append({"time": now, "price": e["price_history"][-1]["price"],
                                       "note": f"missed_{reason}"})
            break
    save_watchlist(entries)


def mark_closed(symbol: str, pattern_type: str, break_time: str,
                exit_price: float, exit_time: str, outcome: str) -> None:
    """Record that a triggered position has been resolved."""
    entries = load_watchlist()
    sid = _sid(symbol, pattern_type, break_time)
    for e in entries:
        if e["id"] == sid and e["status"] == "triggered":
            e["status"]      = "closed"
            e["close_time"]  = exit_time
            e["close_price"] = exit_price
            e["outcome"]     = outcome
            e["elapsed_total_hours"] = _hours_between(e["first_seen"], exit_time)
            e["price_history"].append({"time": exit_time, "price": exit_price,
                                       "note": f"closed_{outcome}"})
            break
    save_watchlist(entries)


def expire_stale(current_watching_ids: set[tuple[str, str, str]],
                 current_triggered_ids: set[tuple[str, str, str]]) -> list[str]:
    """Mark watching entries that no longer appear in the scan as expired.

    A setup expires when it has aged past the 12-bar window and the scanner
    stops returning it without it having triggered.
    """
    entries = load_watchlist()
    now = datetime.now(timezone.utc).isoformat()
    expired_symbols = []

    for e in entries:
        if e["status"] != "watching":
            continue
        key = (e["symbol"], e["type"], e["break_time"])
        if key not in current_watching_ids and key not in current_triggered_ids:
            e["status"] = "expired"
            e["elapsed_watching_hours"] = _hours_between(e["first_seen"], now)
            e["price_history"].append({
                "time": now,
                "price": e["price_history"][-1]["price"],
                "note": "expired",
            })
            expired_symbols.append(e["symbol"])

    save_watchlist(entries)
    return expired_symbols


def watchlist_summary() -> dict:
    """Aggregate counts and open-setup list for reporting."""
    entries = load_watchlist()
    by_status: dict[str, list] = {}
    for e in entries:
        by_status.setdefault(e["status"], []).append(e)

    closed = by_status.get("closed", [])
    wins   = [e for e in closed if e.get("outcome") == "win"]
    losses = [e for e in closed if e.get("outcome") == "loss"]

    return {
        "watching":  len(by_status.get("watching", [])),
        "triggered": len(by_status.get("triggered", [])),
        "closed":    len(closed),
        "expired":   len(by_status.get("expired", [])),
        "missed":    len(by_status.get("missed", [])),
        "wins":      len(wins),
        "losses":    len(losses),
        "win_rate":  round(len(wins) / len(closed), 3) if closed else 0.0,
        "avg_elapsed_watching_hours": (
            round(sum(e["elapsed_watching_hours"] for e in closed + by_status.get("expired", [])
                      if e.get("elapsed_watching_hours")) /
                  max(1, len([e for e in closed + by_status.get("expired", [])
                               if e.get("elapsed_watching_hours")])), 1)
        ),
        "open_setups": [
            {
                "symbol":               e["symbol"],
                "type":                 e["type"],
                "status":               e["status"],
                "suggested_entry":      e["suggested_entry"],
                "suggested_entry_zone": e["suggested_entry_zone"],
                "stop":                 e["stop"],
                "target_1R":            e["target_1R"],
                "target_2R":            e["target_2R"],
                "first_seen":           e["first_seen"],
                "latest_price":         e["price_history"][-1]["price"] if e["price_history"] else None,
                "price_snapshots":      len(e["price_history"]),
            }
            for e in entries
            if e["status"] in ("watching", "triggered")
        ],
    }
