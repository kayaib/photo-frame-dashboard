#!/usr/bin/env python3
"""Sync Audi Q4 e-tron status via EU Data Act portal.

No device approval or encrypted tokens needed — just email/password.
Uses https://eu-data-act.drivesomethinggreater.com (VW Group EU Data Act mandate).

PREREQUISITE (one-time, in a browser):
  1. Visit https://eu-data-act.drivesomethinggreater.com
  2. Log in with your Audi/VW credentials
  3. Click "Allow" on the consent screen
  4. Link your vehicle
  5. Request vehicle data:
     - One-time batch: Fahrzeugdetails → "Verfügbare Daten anfragen"
     - Continuous (preferred): Fahrzeugdetails → "Benutzerdefinierte Daten anfragen"
       → configure data clusters + frequency (e.g. 1h)

After that this script runs headlessly every hour via GitHub Actions.
It prefers a continuous 'partial' request if one exists, and falls back
to any configured 'all' (batch) request.

Env vars:
  AUDI_USERNAME   your myAudi / VW email
  AUDI_PASSWORD   your password
"""
import datetime
import io
import json
import os
import sys
import zipfile

import requests
from bs4 import BeautifulSoup

BASE_URL      = "https://eu-data-act.drivesomethinggreater.com"
IDENTITY_BASE = "https://identity.vwgroup.io"
AUTHORIZE_URL = IDENTITY_BASE + "/oidc/v1/authorize"
REDIRECT_URI  = BASE_URL + "/login"
SCOPE         = "openid cars profile"
CLIENT_ID     = "cc29b87a-5e9a-4362-aecf-5adea6b01bbb@apps_vw-dilab_com"
BRAND_STATE   = "de__en__AUDI"

SENTINEL_INTS = {65535, 2147483647, 4294967295}
MINUS_ONE_FIELDS = {
    "remaining_charging_time",
    "remaining_charging_time_to_complete",
    "battery_state_report.remaining_charging_time_complete",
    "battery_state_report.remaining_charging_time_bulk",
    "remaining_charging_time_target_soc",
}

OUT_FILE = os.path.join(os.path.dirname(__file__), "..", "audi.json")


# ── HTML helpers ──────────────────────────────────────────────────────────────

def _template_model(html: str) -> dict:
    idx = html.find("templateModel")
    if idx < 0:
        return {}
    brace = html.find("{", idx)
    if brace < 0:
        return {}
    depth = 0
    for i in range(brace, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[brace : i + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def _parse_form(html: str, base_url: str) -> tuple[str, dict]:
    """Return (action_url, {field: value}) from the first <form>."""
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        return base_url, {}
    action = form.get("action", "") or base_url
    if action.startswith("/"):
        from urllib.parse import urlparse
        p = urlparse(base_url)
        action = f"{p.scheme}://{p.netloc}{action}"
    fields = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            fields[name] = inp.get("value", "")
    model = _template_model(html)
    for key in ("hmac", "relayState"):
        if model.get(key):
            fields[key] = model[key]
    return action, fields


def _parse_form_pairs(html: str, base_url: str) -> tuple[str, list]:
    """Return (action_url, [(name, value), ...]) preserving duplicate field names."""
    from urllib.parse import urlparse, urljoin
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")
    if not form:
        return base_url, []
    action = form.get("action", "") or base_url
    if action.startswith("/"):
        p = urlparse(base_url)
        action = f"{p.scheme}://{p.netloc}{action}"
    elif action and not action.startswith("http"):
        action = urljoin(base_url, action)
    pairs = []
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            pairs.append((name, inp.get("value", "")))
    return action, pairs


def _login_error(html: str) -> str | None:
    model = _template_model(html)
    err = model.get("error") or model.get("errorCode")
    if not err:
        return None
    if isinstance(err, dict):
        return err.get("text") or err.get("errorCode") or json.dumps(err)
    return str(err)


# ── Login ─────────────────────────────────────────────────────────────────────

def login(email: str, password: str) -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) photo-frame-dashboard/1.0"

    # Step 1: start OIDC flow → email form
    auth_url = (
        f"{AUTHORIZE_URL}?client_id={CLIENT_ID}"
        f"&response_type=code&scope={SCOPE}"
        f"&state={BRAND_STATE}&redirect_uri={REDIRECT_URI}&prompt=login"
    )
    r = session.get(auth_url, allow_redirects=True)
    r.raise_for_status()

    action, fields = _parse_form(r.text, r.url)
    if not fields.get("hmac") or not fields.get("_csrf"):
        raise RuntimeError("Could not parse sign-in form (missing hmac/_csrf)")
    csrf_step1 = fields["_csrf"]
    fields["email"] = email

    # Step 2: POST email
    r2 = session.post(action, data=fields, allow_redirects=True)
    r2.raise_for_status()

    # Step 3: POST password
    model2 = _template_model(r2.text)
    hmac2 = model2.get("hmac", "")
    relay2 = model2.get("relayState", "")
    if not hmac2:
        err = _login_error(r2.text)
        raise RuntimeError(err or "Identity portal did not return password form — check email address")

    auth_action = r2.url.split("?")[0]
    r3 = session.post(auth_action, data={
        "_csrf":      csrf_step1,
        "hmac":       hmac2,
        "relayState": relay2,
        "email":      email,
        "password":   password,
    }, allow_redirects=True)

    # Step 4: accept VW scope consent (first-time only)
    if "consent" in r3.url and "signin-service" in r3.url:
        _, consent_pairs = _parse_form_pairs(r3.text, r3.url)
        r3 = session.post(r3.url, data=consent_pairs, allow_redirects=True)

    # Step 5: handle any remaining error/consent pages
    landing = r3.url
    if "signin-service" in landing or "/error" in landing:
        err = _login_error(r3.text)
        if "consent/marketing" in landing:
            from urllib.parse import urlparse, parse_qs
            cb_param = parse_qs(urlparse(landing).query).get("callback", [None])[0]
            if cb_param:
                session.get(cb_param, allow_redirects=True)
        elif err:
            raise RuntimeError(f"Login failed: {err}")
        else:
            raise RuntimeError(f"Login did not complete (landed at {landing})")

    if not any(c.name == "access_token" for c in session.cookies):
        raise RuntimeError(
            "Login did not establish an access_token cookie.\n"
            "Complete first-time setup at https://eu-data-act.drivesomethinggreater.com"
        )

    print("Logged in to EU Data Act portal.")
    return session


# ── Portal API ────────────────────────────────────────────────────────────────

def get_vehicles(session: requests.Session) -> list[dict]:
    r = session.get(f"{BASE_URL}/proxy_api/consent/me/vehicles?viewPosition=FRONT_LEFT")
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("vehicles", [])


def get_identifier(session: requests.Session, vin: str) -> tuple[str, str]:
    """Return (identifier, data_type) where data_type is 'partial' or 'all'.

    Tries the continuous/partial request first (preferred: frequent updates).
    Falls back to the one-time 'all' request if no partial request is configured.
    """
    r = session.get(f"{BASE_URL}/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/partial")
    if r.status_code == 200:
        return r.json()["Identifier"], "partial"

    r2 = session.get(f"{BASE_URL}/proxy_api/euda-apim/datarequest/vehicles/{vin}/metadata/all")
    if r2.status_code == 200:
        meta = r2.json()
        print(f"Using 'all' data request (Frequency={meta.get('Frequency')}, expires {meta.get('EndDate')})")
        return meta["Identifier"], "all"

    if r2.status_code == 404:
        raise RuntimeError(
            f"No data request configured for VIN {vin}.\n"
            "In the portal: Fahrzeugdetails → 'Verfügbare Daten anfragen'"
        )
    r2.raise_for_status()
    raise RuntimeError("Unreachable")


def list_datasets(session: requests.Session, vin: str, identifier: str, data_type: str) -> list[dict]:
    r = session.get(
        f"{BASE_URL}/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/list",
        headers={"type": data_type},
    )
    if r.status_code == 404:
        return []
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data
    return data.get("files", [])


def download_dataset(session: requests.Session, vin: str, identifier: str, name: str, data_type: str) -> bytes:
    r = session.get(
        f"{BASE_URL}/proxy_api/euda-apim/datadelivery/vehicles/{vin}/{identifier}/download",
        headers={"filename": name, "type": data_type},
    )
    r.raise_for_status()
    return r.content


# ── Dataset parsing ───────────────────────────────────────────────────────────

def _is_sentinel(value: str, field: str) -> bool:
    if not value or not value.strip():
        return True
    try:
        n = float(value)
        if int(n) in SENTINEL_INTS:
            return True
        if field in MINUS_ONE_FIELDS and n == -1:
            return True
    except (ValueError, OverflowError):
        pass
    return False


def parse_zip(data: bytes) -> dict[str, str]:
    """Extract data points from the dataset ZIP → {field_name: value}."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        json_name = next((n for n in zf.namelist() if n.lower().endswith(".json")), None)
        if not json_name:
            raise RuntimeError("No JSON in dataset ZIP")
        payload = json.loads(zf.read(json_name))

    points: dict[str, dict] = {}
    for seq, item in enumerate(payload.get("Data", [])):
        field = item.get("dataFieldName") or item.get("key", "")
        if not field:
            continue
        value = str(item.get("value", ""))
        if _is_sentinel(value, field):
            if field not in points:
                points[field] = {"value": value, "seq": seq, "sentinel": True}
            continue
        existing = points.get(field)
        if existing is None or existing["sentinel"]:
            points[field] = {"value": value, "seq": seq, "sentinel": False}
        else:
            points[field] = {"value": value, "seq": seq, "sentinel": False}

    return {f: p["value"] for f, p in points.items() if not p["sentinel"]}


def _float(fields: dict, *keys) -> float | None:
    for k in keys:
        v = fields.get(k)
        if v is not None:
            try:
                return float(v)
            except ValueError:
                pass
    return None


def _int(fields: dict, *keys) -> int | None:
    v = _float(fields, *keys)
    return int(v) if v is not None else None


def _str(fields: dict, *keys) -> str | None:
    for k in keys:
        v = fields.get(k)
        if v is not None:
            return str(v)
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    email    = os.environ.get("AUDI_USERNAME")
    password = os.environ.get("AUDI_PASSWORD")
    if not email or not password:
        print("ERROR: AUDI_USERNAME and AUDI_PASSWORD must be set", file=sys.stderr)
        sys.exit(1)

    session = login(email, password)

    vehicles = get_vehicles(session)
    if not vehicles:
        raise RuntimeError(
            "No vehicles found on the EU Data Act portal.\n"
            "Complete setup at https://eu-data-act.drivesomethinggreater.com"
        )

    v = vehicles[0]
    vin = v.get("vin") or v.get("vehicleIdentificationNumber", "")
    title = v.get("nickName") or v.get("vehicleNickname") or v.get("nickname") or v.get("modelName") or "Audi Q4 e-tron"
    print(f"Vehicle: {title} ({vin})")

    identifier, data_type = get_identifier(session, vin)
    datasets = list_datasets(session, vin, identifier, data_type)

    content_sets = [
        d for d in datasets
        if not d.get("name", "").lower().endswith("_no_content_found.zip")
    ]
    if not content_sets:
        print("No files available yet — portal is still collecting data. audi.json unchanged.")
        sys.exit(0)

    content_sets.sort(key=lambda d: d.get("createdOn", ""))
    latest = content_sets[-1]
    print(f"Downloading dataset: {latest['name']}")

    raw = download_dataset(session, vin, identifier, latest["name"], data_type)
    fields = parse_zip(raw)

    soc      = _int(fields, "battery_level_HV.value", "hv_soc", "state_of_charge")
    range_km = _int(fields, "cruising_range_secondary_engine", "cruising_range_primary_engine", "cruising_range_combined")
    ch_state = _str(fields, "charging_state", "charging_state_report.current_charge_state")
    plug     = _str(fields, "plug_state", "charging_plug1_connectionstate")
    rem_time = _int(fields, "remaining_charging_time")
    target   = _int(fields, "settings.target_soc")

    data = {
        "updatedAt": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "datasetTime": latest.get("createdOn"),
        "vin": vin,
        "title": title,
        "stateOfCharge": soc,
        "rangeKm": range_km,
        "chargingState": ch_state,
        "plugState": plug,
        "remainingChargingTime": rem_time,
        "targetSoc": target,
    }

    with open(OUT_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"audi.json written: {soc}% SOC, {range_km} km range, state={ch_state}")


if __name__ == "__main__":
    main()
