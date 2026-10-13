#!/usr/bin/env python3
"""One-time local script to authenticate with Audi Connect and save encrypted tokens.

Run this locally once to set up the Audi integration:

  1. Clone myaudi-api:
       git clone https://github.com/John6810/myaudi-api.git /tmp/myaudi-api
       pip install -r /tmp/myaudi-api/requirements.txt

  2. Set env vars:
       export AUDI_USERNAME="your@email.de"
       export AUDI_PASSWORD="yourpassword"
       export AUDI_SPIN="1234"          # only if your account has a S-PIN
       export AUDI_TOKEN_KEY="some-long-random-string"  # encryption key
       export MYAUDI_API_PATH="/tmp/myaudi-api"

  3. Run:
       python scripts/get-audi-token.py

  4. Follow the browser device approval prompt (EU accounts only).

  5. Commit the generated audi_tokens.enc to the repo.

  6. Set these GitHub Secrets:
       AUDI_USERNAME, AUDI_PASSWORD, AUDI_SPIN, AUDI_TOKEN_KEY
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile

API_PATH = os.environ.get("MYAUDI_API_PATH", "/tmp/myaudi-api")
sys.path.insert(0, API_PATH)

try:
    from audi_connect.connection import connect_and_get_vehicles, create_session
    from audi_connect.token_store import DEFAULT_TOKEN_FILE
except ImportError as e:
    print(f"ERROR: Could not import myaudi-api from {API_PATH}: {e}")
    print("Run: git clone https://github.com/John6810/myaudi-api.git /tmp/myaudi-api")
    print("     pip install -r /tmp/myaudi-api/requirements.txt")
    sys.exit(1)

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
OUT_ENC = os.path.join(REPO_ROOT, "audi_tokens.enc")


def on_verification(url, user_code, expires_in):
    print()
    print("=" * 60)
    print("AUDI CONNECT DEVICE VERIFICATION REQUIRED")
    print("=" * 60)
    print(f"1. Open this URL in your browser:")
    print(f"   {url}")
    print(f"2. Enter code: {user_code}")
    print(f"   (expires in {expires_in // 60} minutes)")
    print("=" * 60)
    print("Waiting for approval...")
    print()


async def main():
    username = os.environ.get("AUDI_USERNAME") or input("Audi email: ").strip()
    password = os.environ.get("AUDI_PASSWORD") or input("Audi password: ").strip()
    country = os.environ.get("AUDI_COUNTRY", "DE")
    spin = os.environ.get("AUDI_SPIN") or None
    token_key = os.environ.get("AUDI_TOKEN_KEY")

    if not token_key:
        import secrets
        token_key = secrets.token_hex(32)
        print(f"Generated AUDI_TOKEN_KEY: {token_key}")
        print("Set this as a GitHub Secret named AUDI_TOKEN_KEY")
        print()

    async with create_session() as session:
        auth, vehicles = await connect_and_get_vehicles(
            session, username, password,
            country=country, spin=spin,
            on_verification=on_verification,
        )

    print(f"Authenticated successfully. {len(vehicles)} vehicle(s) found:")
    for v in vehicles:
        print(f"  VIN: {v.vin}  |  {v.title}  {v.model}")

    if not os.path.exists(DEFAULT_TOKEN_FILE):
        print(f"ERROR: Token file not found at {DEFAULT_TOKEN_FILE}", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run(
        [
            "openssl", "enc", "-aes-256-cbc", "-pbkdf2",
            "-in", DEFAULT_TOKEN_FILE,
            "-out", OUT_ENC,
            "-pass", f"pass:{token_key}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"ERROR encrypting token: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    print()
    print(f"Encrypted tokens written to: audi_tokens.enc")
    print()
    print("Next steps:")
    print("  git add audi_tokens.enc")
    print("  git commit -m 'feat: add encrypted Audi tokens'")
    print("  git push")
    print()
    print("GitHub Secrets to set:")
    print(f"  AUDI_USERNAME  = {username}")
    print(f"  AUDI_PASSWORD  = (your password)")
    if spin:
        print(f"  AUDI_SPIN      = {spin}")
    print(f"  AUDI_TOKEN_KEY = {token_key}")


asyncio.run(main())
