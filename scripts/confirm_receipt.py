#!/usr/bin/env python3
import re, requests, sys

BASE = "http://127.0.0.1:5000"
s = requests.Session()

# GET /receive to get CSRF token and session cookie
r = s.get(f"{BASE}/receive")
if r.status_code != 200:
    print("GET /receive failed:", r.status_code, r.text[:400])
    sys.exit(1)

m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
if not m:
    print("CSRF token not found in /receive page (first 400 chars):")
    print(r.text[:400])
    sys.exit(1)
token = m.group(1)
print("CSRF token:", token)

# POST confirm (empty receipt_id -> server will create one)
payload = {"receipt_id": "", "confirmed_by": "web"}
headers = {"Content-Type": "application/json", "X-CSRFToken": token}
resp = s.post(f"{BASE}/api/receive/confirm", headers=headers, json=payload)

print("Status:", resp.status_code)
print(resp.text)
