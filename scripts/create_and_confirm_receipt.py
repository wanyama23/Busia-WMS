#!/usr/bin/env python3
# scripts/confirm_receipt.py
import re, requests, sys, json

BASE = "http://127.0.0.1:5000"
session = requests.Session()

# 1) GET /receive to get CSRF token and session cookie
r = session.get(f"{BASE}/receive")
if r.status_code != 200:
    print("GET /receive failed:", r.status_code, r.text[:400])
    sys.exit(1)

m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
if not m:
    print("CSRF token not found in /receive page (first 200 chars):")
    print(r.text[:200])
    sys.exit(1)
token = m.group(1)
print("CSRF token:", token)

# 2) POST confirm (replace receipt_id with a real id if you want)
payload = {
    "receipt_id": "",        # empty -> server will create a receipt (our route supports this)
    "confirmed_by": "web"
}
headers = {"Content-Type": "application/json", "X-CSRFToken": token}
resp = session.post(f"{BASE}/api/receive/confirm", headers=headers, json=payload)

print("Status:", resp.status_code)
print(resp.text)
