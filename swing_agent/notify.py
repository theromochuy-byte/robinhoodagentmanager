"""Email notification digest for the paper trading pipeline.

Sends a single email per run summarising:
  - New entries logged today
  - Positions closed (stop or 2R hit)
  - Near-stop alerts  (price within NEAR_PCT % of stop)
  - Near-2R alerts    (price within NEAR_PCT % of 2R target)

Environment variables (set as GitHub Actions secrets):
  NOTIFY_EMAIL_FROM      sender address (Gmail recommended)
  NOTIFY_EMAIL_PASSWORD  Gmail App Password (not your login password)
  NOTIFY_EMAIL_TO        recipient address
  NOTIFY_SMTP_HOST       optional, default smtp.gmail.com
  NOTIFY_SMTP_PORT       optional, default 587
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ROOT        = Path(__file__).resolve().parent.parent
DATA        = ROOT / "data"
EQUITY_FILE = DATA / "equity.json"

NEAR_PCT = 0.05   # alert when price is within 5% of stop or 2R


def _pct_gap(price: float, level: float) -> float:
    """Fractional distance from price to level."""
    return abs(price - level) / abs(level) if level else 999


def build_digest(
    new_entries: list[dict],
    closes: list[dict],
    quotes: dict[str, float],
    scan_date: str,
) -> dict:
    """Build the digest payload from pipeline results.

    Args:
        new_entries: trade dicts added to ledger today.
        closes: trade dicts whose stop or 2R was hit today
                (should have 'exit_reason' and 'exit_price' set).
        quotes: {symbol: price} for all open positions.
        scan_date: YYYY-MM-DD string.

    Returns dict with keys: new_entries, closes, near_stop, near_2r,
    realized_pnl, unrealized_pnl, equity, open_count.
    """
    # Load full ledger to compute proximity alerts and P&L
    ledger_path = DATA / "paper_trades_live.json"
    if not ledger_path.exists():
        return {"new_entries": new_entries, "closes": closes,
                "near_stop": [], "near_2r": [], "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "equity": {"starting_equity": 1500.0, "capital_in_use": 0.0, "available_equity": 1500.0},
                "open_count": 0}

    trades = json.loads(ledger_path.read_text())
    open_trades = [t for t in trades if t.get("status") == "entered"]

    near_stop = []
    near_2r   = []
    unrealized = 0.0

    for t in open_trades:
        sym   = t["symbol"]
        price = quotes.get(sym)
        if price is None:
            continue
        entry  = t["entry"]
        stop   = t["stop"]
        target = t["target_2R"]
        shares = t.get("shares", 0)
        variant = t.get("stop_variant", "")

        unrealized += (price - entry) * shares

        if _pct_gap(price, stop) <= NEAR_PCT:
            near_stop.append({
                "symbol": sym, "type": t["type"], "variant": variant,
                "price": price, "stop": stop,
                "gap_pct": round(_pct_gap(price, stop) * 100, 1),
            })

        if target > entry and _pct_gap(price, target) <= NEAR_PCT:
            near_2r.append({
                "symbol": sym, "type": t["type"], "variant": variant,
                "price": price, "target_2R": target,
                "gap_pct": round(_pct_gap(price, target) * 100, 1),
            })

    realized = sum(
        t.get("realized_pnl", 0.0)
        for t in trades
        if t.get("status") in ("stopped", "target_hit")
    )

    # Load equity state
    equity = {"starting_equity": 1500.0, "capital_in_use": 0.0, "available_equity": 1500.0}
    if EQUITY_FILE.exists():
        equity = json.loads(EQUITY_FILE.read_text())

    # Attach time-to-2R estimates from the ledger to near_2r items
    near_2r_with_eta = []
    for item in near_2r:
        sym = item["symbol"]
        trade = next((t for t in open_trades if t["symbol"] == sym
                      and t.get("target_2R") == item["target_2R"]), {})
        item["est_days_to_2r"] = trade.get("est_days_to_2r")
        item["progress_pct"]   = trade.get("progress_pct")
        near_2r_with_eta.append(item)

    return {
        "new_entries":    new_entries,
        "closes":         closes,
        "near_stop":      sorted(near_stop,         key=lambda x: x["gap_pct"]),
        "near_2r":        sorted(near_2r_with_eta,  key=lambda x: x["gap_pct"]),
        "realized_pnl":   round(realized,    2),
        "unrealized_pnl": round(unrealized,  2),
        "equity":         equity,
        "open_count":     len(open_trades),
    }


def _fmt_trade(t: dict) -> str:
    variant = f" [{t['stop_variant']}]" if t.get("stop_variant") else ""
    return f"{t['symbol']} {t['type']}{variant}"


def _html_table(rows: list[list], headers: list[str]) -> str:
    th = "".join(f"<th style='padding:4px 10px;text-align:left;border-bottom:1px solid #ccc'>{h}</th>" for h in headers)
    body = ""
    for row in rows:
        td = "".join(f"<td style='padding:4px 10px'>{c}</td>" for c in row)
        body += f"<tr>{td}</tr>"
    return f"<table style='border-collapse:collapse;font-size:13px'><tr>{th}</tr>{body}</table>"


def render_html(digest: dict, scan_date: str) -> str:
    eq = digest.get("equity", {})
    starting   = eq.get("starting_equity", 1500.0)
    in_use     = eq.get("capital_in_use", 0.0)
    available  = eq.get("available_equity", starting)
    open_count = digest.get("open_count", 0)

    parts = [f"""
<h2 style='font-family:sans-serif'>📈 Swing Agent Daily Digest — {scan_date}</h2>
<p style='font-family:sans-serif;color:#555'>Paper trading summary. No real orders placed.</p>
<table style='font-family:sans-serif;font-size:13px;border-collapse:collapse;margin-bottom:12px'>
  <tr>
    <td style='padding:4px 16px 4px 0'><strong>Starting equity</strong></td>
    <td style='padding:4px 16px 4px 0'>${starting:,.2f}</td>
    <td style='padding:4px 16px 4px 0'><strong>Open positions</strong></td>
    <td style='padding:4px 0'>{open_count}</td>
  </tr>
  <tr>
    <td style='padding:4px 16px 4px 0'><strong>Capital in use</strong></td>
    <td style='padding:4px 16px 4px 0;color:{"#c00" if in_use > starting else "#333"}'>${in_use:,.2f}</td>
    <td style='padding:4px 16px 4px 0'><strong>Available</strong></td>
    <td style='padding:4px 0;color:{"#080" if available > 0 else "#c00"}'>${available:,.2f}</td>
  </tr>
</table>
"""]

    # Closes
    closes = digest["closes"]
    if closes:
        rows = [[_fmt_trade(t), t.get("exit_reason","?"),
                 f"${t.get('exit_price', t.get('entry',0)):.2f}",
                 f"${t.get('realized_pnl', 0):.2f}"] for t in closes]
        parts.append("<h3 style='font-family:sans-serif;color:#c00'>🔴 Closed today</h3>")
        parts.append(_html_table(rows, ["Position", "Reason", "Exit Price", "P&L"]))
    else:
        parts.append("<p style='font-family:sans-serif'>🔴 <strong>Closes:</strong> none today</p>")

    # New entries
    entries = digest["new_entries"]
    if entries:
        rows = [[_fmt_trade(t), f"${t['entry']:.2f}", f"${t['stop']:.2f}",
                 f"${t['target_2R']:.2f}", str(t.get("shares","?"))] for t in entries]
        parts.append("<h3 style='font-family:sans-serif;color:#080'>🟢 New entries today</h3>")
        parts.append(_html_table(rows, ["Position", "Entry", "Stop", "2R Target", "Shares"]))
    else:
        parts.append("<p style='font-family:sans-serif'>🟢 <strong>New entries:</strong> none today</p>")

    # Near 2R
    near_2r = digest["near_2r"]
    if near_2r:
        rows = []
        for t in near_2r:
            eta  = t.get("est_days_to_2r")
            prog = t.get("progress_pct")
            eta_str  = f"{eta:.1f}d" if eta is not None else "—"
            prog_str = f"{prog:.0f}%" if prog is not None else "—"
            rows.append([_fmt_trade(t), f"${t['price']:.2f}", f"${t['target_2R']:.2f}",
                         f"{t['gap_pct']}%", prog_str, eta_str])
        parts.append("<h3 style='font-family:sans-serif;color:#e80'>🎯 Near 2R target (within 5%)</h3>")
        parts.append(_html_table(rows, ["Position", "Price", "2R Target", "Gap", "Progress", "Est. Days"]))

    # Near stop
    near_stop = digest["near_stop"]
    if near_stop:
        rows = [[_fmt_trade(t), f"${t['price']:.2f}", f"${t['stop']:.2f}",
                 f"{t['gap_pct']}%"] for t in near_stop]
        parts.append("<h3 style='font-family:sans-serif;color:#a00'>🟡 Near stop (within 5%)</h3>")
        parts.append(_html_table(rows, ["Position", "Price", "Stop", "Gap"]))

    # P&L summary
    parts.append(f"""
<hr/>
<p style='font-family:sans-serif'>
  <strong>Realized P&L:</strong> ${digest['realized_pnl']:.2f} &nbsp;|&nbsp;
  <strong>Unrealized P&L:</strong> ${digest['unrealized_pnl']:.2f}
</p>
<p style='font-family:sans-serif;color:#999;font-size:11px'>
  Generated by swing_agent — paper mode only. No real capital at risk.
</p>
""")

    return "\n".join(parts)


def send_email(subject: str, html_body: str) -> bool:
    """Send email via SMTP. Returns True on success."""
    sender    = os.environ.get("NOTIFY_EMAIL_FROM")
    password  = os.environ.get("NOTIFY_EMAIL_PASSWORD")
    recipient = os.environ.get("NOTIFY_EMAIL_TO")
    smtp_host = os.environ.get("NOTIFY_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("NOTIFY_SMTP_PORT", "587"))

    if not all([sender, password, recipient]):
        print("NOTIFY_EMAIL_FROM / NOTIFY_EMAIL_PASSWORD / NOTIFY_EMAIL_TO not set — skipping email.",
              file=sys.stderr)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = recipient
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        print(f"Email sent to {recipient}")
        return True
    except Exception as e:
        print(f"Email failed: {e}", file=sys.stderr)
        return False


def send_digest(
    new_entries: list[dict],
    closes: list[dict],
    quotes: dict[str, float],
    scan_date: str | None = None,
) -> bool:
    if scan_date is None:
        scan_date = str(date.today())

    digest = build_digest(new_entries, closes, quotes, scan_date)
    html   = render_html(digest, scan_date)

    n_close = len(digest["closes"])
    n_new   = len(digest["new_entries"])
    n_2r    = len(digest["near_2r"])
    n_stop  = len(digest["near_stop"])
    subject = (
        f"[Swing Agent {scan_date}] "
        f"{n_close} closed · {n_new} new entries · "
        f"{n_2r} near 2R · {n_stop} near stop"
    )
    return send_email(subject, html)
