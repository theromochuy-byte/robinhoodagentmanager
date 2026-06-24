#!/usr/bin/env python3
"""One-time setup: log in interactively and export the session token.

Run this ONCE on your local machine. It will:
  1. Prompt for your Robinhood email and password.
  2. Prompt for the 2FA code from your authenticator app.
  3. Save the session token to ~/.tokens/robinhood.pickle.
  4. Print the base64-encoded token — paste that as ROBINHOOD_TOKEN in
     GitHub → Settings → Secrets and variables → Actions.

After that, GitHub Actions never needs your TOTP key again.

Usage:
  python3 export_token.py
"""
import base64
import getpass
from pathlib import Path

try:
    import robin_stocks.robinhood as rh
except ImportError:
    raise SystemExit("Run: pip install robin-stocks")

print("=== Robinhood token export ===\n")
username = input("Robinhood email: ").strip()
password = getpass.getpass("Robinhood password: ")
mfa_code = input("2FA code from your authenticator app: ").strip()

print("\nLogging in...")
rh.login(username=username, password=password, mfa_code=mfa_code, store_session=True)

pickle_path = Path.home() / ".tokens" / "robinhood.pickle"
if not pickle_path.exists():
    raise SystemExit(f"Token file not found at {pickle_path} — login may have failed.")

token_b64 = base64.b64encode(pickle_path.read_bytes()).decode()

print("\n" + "=" * 60)
print("SUCCESS. Add this as a GitHub Actions secret named ROBINHOOD_TOKEN:")
print("=" * 60)
print(token_b64)
print("=" * 60)
print("\nGitHub: Settings → Secrets and variables → Actions → New repository secret")
print("Name:  ROBINHOOD_TOKEN")
print("Value: (the long string above)")
