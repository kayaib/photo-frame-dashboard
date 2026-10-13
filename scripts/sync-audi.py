#!/usr/bin/env python3
"""GitHub Actions sync script for Audi Q4 e-tron status.

Reads AUDI_TOKEN_KEY to decrypt audi_tokens.enc, authenticates,
fetches vehicle data, writes audi.json, and re-encrypts the updated
token file back to audi_tokens.enc so refresh tokens persist across runs.
"""
import asyncio
import datetime
import json
import logging
import os
import subprocess
import sys

API_PATH = os.environ.get("MYAUDI_API_PATH", "/tmp/myaudi-api")
sys.path.insert(0, API_PATH)

try:
    from audi_connect.connection import connect_and_get_vehicles, create_session
    from audi_connect.token_store import DEFAULT_TOKEN_FILE
except ImportError as e:
    print(f"ERROR: Cannot import myaudi-api from {API_PATH}: {e}", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(level=logging.WARNING)

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
ENC_FILE = os.path.join(REPO_ROOT, "audi_tokens.enc")
OUT_FILE = os.path.join(REPO_ROOT, "audi.json")


def decrypt_tokens(key: str) -> None:
    if not os.path.exists(ENC_FILE):
        print("ERROR: audi_tokens.enc not found. Run scripts/get-audi-token.py locally first.", file=sys.stderr)
        sys.exit(1)
    result = subprocess.run(
        ["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
         "-in", ENC_FILE, "-out", DEFAULT_TOKEN_FILE, "-pass", f"pass:{key}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR decrypting tokens: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print("Token file restored.")


def encrypt_tokens(key: str) -> None:
    if not os.path.exists(DEFAULT_TOKEN_FILE):
        return
    result = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc", "-pbkdf2",
         "-in", DEFAULT_TOKEN_FILE, "-out", ENC_FILE, "-pass", f"pass:{key}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"WARNING: Could not re-encrypt token: {result.stderr}", file=sys.stderr)
    else:
        print("Token file re-encrypted.")


async def main():
    token_key = os.environ.get("AUDI_TOKEN_KEY")
    if not token_key:
        print("ERROR: AUDI_TOKEN_KEY not set", file=sys.stderr)
        sys.exit(1)

    decrypt_tokens(token_key)

    username = os.environ.get("AUDI_USERNAME")
    password = os.environ.get("AUDI_PASSWORD")
    country = os.environ.get("AUDI_COUNTRY", "DE")
    spin = os.environ.get("AUDI_SPIN") or None

    if not username or not password:
        print("ERROR: AUDI_USERNAME and AUDI_PASSWORD must be set", file=sys.stderr)
        sys.exit(1)

    async with create_session() as session:
        auth, vehicles = await connect_and_get_vehicles(
            session, username, password, country=country, spin=spin,
        )

    if not vehicles:
        print("ERROR: No vehicles found", file=sys.stderr)
        sys.exit(1)

    v = vehicles[0]
    await v.update()

    data = {
        "updatedAt": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "vin": v.vin,
        "title": v.title or v.model or "Audi Q4 e-tron",
        "stateOfCharge": v.state_of_charge,
        "chargingState": v.charging_state,
        "chargingPower": v.charging_power,
        "remainingChargingTime": v.remaining_charging_time,
        "plugState": v.plug_state,
        "rangeKm": v.range_km,
    }

    with open(OUT_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

    soc = data["stateOfCharge"]
    rng = data["rangeKm"]
    state = data["chargingState"]
    print(f"audi.json written: {soc}% SOC, {rng} km range, state={state}")

    encrypt_tokens(token_key)


asyncio.run(main())
