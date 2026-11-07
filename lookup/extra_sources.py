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


# ---------------------------------------------------------------- Walmart Affiliate API (free; walmart.io app + RSA key)
# Secrets: WALMART_CONSUMER_ID, WALMART_PRIVATE_KEY (PEM text), optional WALMART_KEY_VERSION (default 1).
# Each request is signed: base64(RSA-SHA256("<consumer id>\n<timestamp ms>\n<key version>\n")).

WALMART_API = "https://developer.api.walmart.com/api-proxy/service/affil/product/v2"


def walmart_keys():
    cid, pem = os.environ.get("WALMART_CONSUMER_ID"), os.environ.get("WALMART_PRIVATE_KEY")
    if cid and pem:
        return cid.strip(), pem.strip().replace("\\n", "\n")
    lines = _keyfile("walmart_consumer_id.txt", 1)
    path = os.path.join(HERE, "WM_IO_private_key.pem")
    if lines and os.path.exists(path):
        return lines[0], open(path, encoding="utf-8").read()
    return None, None


def _rsa_sign(pem: str, message: bytes) -> str:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        return base64.b64encode(key.sign(message, padding.PKCS1v15(), hashes.SHA256())).decode()
    except ImportError:  # GitHub runners and most PCs with Git have openssl
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as f:
            f.write(pem)
            kp = f.name
        try:
            sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", kp], input=message, capture_output=True, check=True).stdout
        finally:
            os.unlink(kp)
        return base64.b64encode(sig).decode()


def _walmart_headers():
    cid, pem = walmart_keys()
    ver = os.environ.get("WALMART_KEY_VERSION", "1")
    ts = str(int(time.time() * 1000))
    sig = _rsa_sign(pem, f"{cid}\n{ts}\n{ver}\n".encode())
    return {"WM_CONSUMER.ID": cid, "WM_CONSUMER.INTIMESTAMP": ts, "WM_SEC.KEY_VERSION": ver, "WM_SEC.AUTH_SIGNATURE": sig}


def fetch_walmart(gtin14, http_get):
    if not walmart_keys()[0]:
        return None, "", [], {}
    s = gtin14.lstrip("0")
    upc = s.zfill(12) if len(s) <= 12 else s
    st, body = http_get(f"{WALMART_API}/items?upc={upc}", _walmart_headers())
    if st != 200:
        return st, body, [], {}
    items = (json.loads(body) or {}).get("items") or []
    offers, info = [], {}
    for it in items[:1]:
        info = {"name": it.get("name"), "brand": it.get("brandName")}
        link = f"https://www.walmart.com/ip/{it['itemId']}" if it.get("itemId") else (it.get("productUrl") or "")
        price = it.get("salePrice") or it.get("msrp")
        seller = (it.get("sellerInfo") or "Walmart.com")
        if price:
            offers.append({"merchant": "Walmart" if "walmart" in str(seller).lower() else f"Walmart marketplace ({seller})",
                           "price": float(price), "link": link, "condition": "New", "title": it.get("name") or "",
                           "updated_t": int(time.time())})
    if offers:
        info["note"] = "Walmart.com current price; confirm on the product page"
    return st, body, offers, info


# ---------------------------------------------------------------- Best Buy Products API (free with approval)
# Secret: BESTBUY_API_KEY. Terms: do not keep Best Buy prices longer than 72 hours as a live price; we store
# the dated price as evidence for a reviewer, with the product page link.

def fetch_bestbuy(gtin14, http_get):
    key = os.environ.get("BESTBUY_API_KEY") or (_keyfile("bestbuy_key.txt", 1) or [None])[0]
    if not key:
        return None, "", [], {}
    s = gtin14.lstrip("0")
    upc = s.zfill(12) if len(s) <= 12 else s
    st, body = http_get(f"https://api.bestbuy.com/v1/products(upc={upc})?apiKey={urllib.parse.quote(key)}"
                        "&format=json&show=sku,name,manufacturer,regularPrice,salePrice,url,condition")
    if st != 200:
        return st, body.replace(key, "***"), [], {}
    prods = (json.loads(body) or {}).get("products") or []
    offers, info = [], {}
    for p in prods[:1]:
        info = {"name": p.get("name"), "brand": p.get("manufacturer")}
        price = p.get("regularPrice") or p.get("salePrice")
        if price and (p.get("condition") or "New").lower() == "new":
            offers.append({"merchant": "Best Buy", "price": float(price), "link": p.get("url") or "", "condition": "New",
                           "title": p.get("name") or "", "updated_t": int(time.time())})
    if offers:
        info["note"] = "Best Buy regular price (dated evidence; confirm on the product page)"
    return st, body, offers, info


# Order = priority. Add new sources here (Walmart, Best Buy, Amazon Business) as they are approved.
PRICE_SOURCES = {
    "kroger": fetch_kroger,
    "walmart": fetch_walmart,
    "bestbuy": fetch_bestbuy,
}
