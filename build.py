#!/usr/bin/env python3
"""
build.py — builds data.js from:
  - Picter API   (submission data — fetched automatically)
  - Stripe API   (submission fee payments)
  - Hardcoded    (Meta Ads — until API is wired up)

Run locally before pushing:
    python3 build.py

Picter JWT expires periodically. If you get a 401, log into app.picter.com,
open DevTools → Network, trigger any request, and copy the jwt cookie value
into .env as picter_jwt=<value>.
"""

import io
import json
import os
import datetime
import requests
import pandas as pd
import stripe

# ── Config ────────────────────────────────────────────────────────────────────
OUT            = "data.js"
CAMPAIGN_YEAR  = 2026
CAMPAIGN_START = datetime.date(2026, 4, 2)

PICTER_API     = "https://api.picter.com/app-curations"
PICTER_HEADERS = {"origin": "https://app.picter.com", "accept": "application/json"}

EXPORT_FIELDS  = {
    "profile": {
        "email": False, "firstName": False, "lastName": False, "name": True,
        "gender": True, "website": False, "facebook": False, "twitter": False,
        "instagram": False, "phone": False, "birthday": True, "nationality": True,
        "address": False, "addressLine2": False, "zip": True, "city": True,
        "country": True, "cv": False, "cvFile": False, "bio": False,
    },
    "entryCoordinator": {
        "entryCoordinatorFirstName": False, "entryCoordinatorLastName": False,
        "entryCoordinatorOrganisation": False, "entryCoordinatorEmail": False,
    },
    "id": True, "submittedAt": True,
}
# Columns present in the xlsx after export
# ID | Submitted at | Name | Gender | Birthday | Nationality | ZIP | City | Country
# Name/ZIP are fetched but NOT written to data.js (public GitHub Pages file — GDPR)

# ── Load .env ─────────────────────────────────────────────────────────────────
def load_env(path=".env"):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env

env = load_env()

# ── 1. Picter — fetch all submission IDs ──────────────────────────────────────
picter_jwt     = env.get("picter_jwt") or os.environ.get("PICTER_JWT")
picter_call_id = env.get("picter_call_id") or os.environ.get("PICTER_CALL_ID")

if not picter_jwt or not picter_call_id:
    print("⚠️  picter_jwt or picter_call_id missing from .env — cannot fetch from Picter API")
    raise SystemExit(1)

auth_headers = {**PICTER_HEADERS, "Authorization": f"Bearer {picter_jwt}"}

print("Fetching submission IDs from Picter…")
sub_ids = []
page = 1
while True:
    r = requests.get(
        f"{PICTER_API}/calls/{picter_call_id}/submissions",
        headers=auth_headers,
        params={"include": "owner.businessProfile", "page[size]": 200, "page[number]": page},
        timeout=30,
    )
    if r.status_code == 401:
        print("❌ Picter JWT expired. Log into app.picter.com, copy the jwt cookie into .env.")
        raise SystemExit(1)
    r.raise_for_status()
    data = r.json()
    sub_ids += [item["id"] for item in data["data"]]
    pagination = data.get("meta", {}).get("pagination", {})
    if page >= pagination.get("totalPages", 1):
        break
    page += 1

print(f"  → {len(sub_ids)} submissions found")

# ── 2. Picter — export xlsx ───────────────────────────────────────────────────
print("Downloading xlsx export…")
export_payload = {
    "data": {
        "attributes": {
            "submissions": sub_ids,
            "selected-fields-for-submissions": EXPORT_FIELDS,
        },
        "relationships": {
            "call": {"data": {"id": picter_call_id, "type": "calls"}}
        },
    }
}
r = requests.post(
    f"{PICTER_API}/exports",
    headers={**PICTER_HEADERS, "cookie": f"jwt={picter_jwt}", "content-type": "text/plain;charset=UTF-8"},
    json=export_payload,
    timeout=60,
)
r.raise_for_status()
print(f"  → {len(r.content):,} bytes received")

# ── 3. Parse xlsx ─────────────────────────────────────────────────────────────
df = pd.read_excel(io.BytesIO(r.content))
df.columns = [c.strip() for c in df.columns]

df["submitted_dt"]   = pd.to_datetime(df["Submitted at"], utc=True)
df["submitted_date"] = df["submitted_dt"].dt.date.astype(str)

def birth_year(s):
    try:
        return int(str(s).strip()[-4:])
    except Exception:
        return None

df["birth_year"] = df["Birthday"].apply(birth_year)
df["age"]        = CAMPAIGN_YEAR - df["birth_year"]

# All columns: ID | Submitted at | Name | Gender | Birthday | Nationality | ZIP | City | Country
# Name and ZIP are available here for any local processing but excluded from data.js (public)
df["ID"]          = df["ID"].astype(str).str.strip()
df["Name"]        = df["Name"].astype(str).str.strip()
df["ZIP"]         = df["ZIP"].astype(str).str.strip()
df["City"]        = df["City"].astype(str).str.strip()
df["Gender"]      = df["Gender"].str.lower().str.strip()
df["Nationality"] = df["Nationality"].str.upper().str.strip()
df["Country"]     = df["Country"].str.upper().str.strip()

# Public-safe records (no name / zip)
records_df = df[["submitted_date", "ID", "Gender", "age", "Nationality", "City", "Country"]].copy()
records_df = records_df.rename(columns={
    "submitted_date": "date", "ID": "id", "Gender": "gender",
    "Nationality": "nationality", "City": "city", "Country": "country",
})
records_df = records_df.dropna(subset=["date", "gender"])
records_df["age"] = records_df["age"].apply(
    lambda x: int(x) if pd.notna(x) and str(x) != "nan" else None
)
records = records_df.to_dict(orient="records")

dates = sorted(set(r["date"] for r in records))
print(f"  → {len(records)} records, {len(dates)} days ({dates[0]} → {dates[-1]})")

# ── 4. Stripe payments ────────────────────────────────────────────────────────
stripe.api_key = env.get("stripe_secret") or os.environ.get("STRIPE_SECRET")
stripe_data = {"totalRevenue": 0, "totalCount": 0, "currency": "eur", "byDay": {}}

if stripe.api_key:
    since_ts = int(datetime.datetime(
        CAMPAIGN_START.year, CAMPAIGN_START.month, CAMPAIGN_START.day,
        tzinfo=datetime.timezone.utc
    ).timestamp())

    by_day = {}
    total_revenue = 0
    total_count   = 0
    currency      = "eur"
    params        = dict(created={"gte": since_ts}, limit=100)
    has_more      = True
    starting_after = None

    print("Fetching Stripe payments…")
    while has_more:
        if starting_after:
            params["starting_after"] = starting_after
        page = stripe.PaymentIntent.list(**params)
        for pi in page.data:
            if pi.status != "succeeded":
                continue
            currency = pi.currency.lower()
            amount   = pi.amount / 100
            date_str = datetime.datetime.fromtimestamp(
                pi.created, tz=datetime.timezone.utc
            ).strftime("%Y-%m-%d")
            if date_str not in by_day:
                by_day[date_str] = {"revenue": 0, "count": 0}
            by_day[date_str]["revenue"] += amount
            by_day[date_str]["count"]   += 1
            total_revenue += amount
            total_count   += 1
        has_more       = page.has_more
        starting_after = page.data[-1].id if page.data else None

    stripe_data = {
        "totalRevenue": round(total_revenue, 2),
        "totalCount":   total_count,
        "currency":     currency,
        "byDay":        {d: {"revenue": round(v["revenue"], 2), "count": v["count"]}
                         for d, v in sorted(by_day.items())},
    }
    print(f"  → {total_count} payments, {currency.upper()} {total_revenue:.2f}")
else:
    print("⚠️  No Stripe key — skipping Stripe data")

# ── 5. Write data.js ──────────────────────────────────────────────────────────
output = {"records": records, "stripe": stripe_data}
js  = "// Auto-generated by build.py — do not edit manually.\n"
js += "const DATA = " + json.dumps(output, indent=2) + ";\n"

with open(OUT, "w") as f:
    f.write(js)

print(f"✓ {OUT} written")
