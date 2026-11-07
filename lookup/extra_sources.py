"""Extra price sources for gik.py. Each source is a function fetch(gtin14, http_get) that returns
(http_status, raw_body, offers, info). Return http_status None when the source is not configured (no keys),
so it is skipped without being logged. gik.py logs every answer, saves every offer, and uses the middle
price when the item has none yet. Order matters: the first source that prices an item wins.

Keys come from environment variables (GitHub Actions secrets) or a text file next to gik.py.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


def _keyfile(name, n=2):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")][:n]


# ---------------------------------------------------------------- Kroger (free, 10,000 calls/day)
# Products API returns the shelf price at one store; we pick the Kroger-family store nearest TEP
# (override with KROGER_LOCATION_ID). Docs: https://developer.kroger.com/reference/

KROGER_API = "https://api.kroger.com/v1"
_kroger = {"token": None, "expires": 0, "location": None}


def kroger_keys():
    cid, sec = os.environ.get("KROGER_CLIENT_ID"), os.environ.get("KROGER_CLIENT_SECRET")
    if cid and sec:
        return cid.strip(), sec.strip()
    lines = _keyfile("kroger_keys.txt")
    return (lines[0], lines[1]) if len(lines) == 2 else (None, None)


def _kroger_token():
    if _kroger["token"] and time.time() < _kroger["expires"] - 60:
        return _kroger["token"]
    cid, sec = kroger_keys()
    auth = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    req = urllib.request.Request(f"{KROGER_API}/connect/oauth2/token",
                                 data=urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "product.compact"}).encode(),
                                 headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())
    _kroger["token"], _kroger["expires"] = d["access_token"], time.time() + int(d.get("expires_in", 1800))
    return _kroger["token"]


def _kroger_location(http_get):
    if _kroger["location"]:
        return _kroger["location"]
    loc = os.environ.get("KROGER_LOCATION_ID")
    if not loc:
        zipc = os.environ.get("KROGER_ZIP", "15222")  # TEP, Pittsburgh; nearest Kroger-family store within 100 miles
        st, body = http_get(f"{KROGER_API}/locations?filter.zipCode.near={zipc}&filter.radiusInMiles=100&filter.limit=1",
                            {"Authorization": f"Bearer {_kroger_token()}"})
        if st == 200:
            data = json.loads(body).get("data") or []
            if data:
                loc = data[0]["locationId"]
                _kroger["store_name"] = f"{data[0].get('chain', 'Kroger').title()} {data[0].get('address', {}).get('city', '')}".strip()
    _kroger["location"] = loc
    return loc


def fetch_kroger(gtin14, http_get):
    if not kroger_keys()[0]:
        return None, "", [], {}
    token = _kroger_token()
    loc = _kroger_location(http_get)
    # Kroger product ids are 13 digits; try the GTIN as-is, then without its check digit (Kroger's internal UPC form)
    candidates = [gtin14[1:], "0" + gtin14[1:-1]] if gtin14.startswith("0") else [gtin14[-13:]]
    st, body = 404, ""
    for pid in candidates:
        q = f"?filter.locationId={loc}" if loc else ""
        st, body = http_get(f"{KROGER_API}/products/{pid}{q}", {"Authorization": f"Bearer {token}"})
        if st == 200:
            break
    if st != 200:
        return st, body, [], {}
    item = json.loads(body).get("data") or {}
    info = {"name": item.get("description"), "brand": item.get("brand")}
    page = "https://www.kroger.com" + item["productPageURI"] if item.get("productPageURI") else \
           f"https://www.kroger.com/search?query={gtin14.lstrip('0')}"
    offers = []
    for it in item.get("items") or []:
        price = (it.get("price") or {}).get("regular") or (it.get("nationalPrice") or {}).get("regular")
        if price:
            offers.append({"merchant": _kroger.get("store_name") or "Kroger", "price": float(price), "link": page,
                           "condition": "New", "title": item.get("description") or "", "updated_t": int(time.time())})
    if offers:
        info["note"] = "Kroger regular shelf price at the nearest Kroger-family store; confirm at a store"
    return st, body, offers, info


# Order = priority. Add new sources here (Walmart, Best Buy, Amazon Business) as they are approved.
PRICE_SOURCES = {
    "kroger": fetch_kroger,
}
