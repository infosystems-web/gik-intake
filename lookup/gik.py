#!/usr/bin/env python3
"""
GIK barcode intake prototype — The Education Partnership.

Standard-library only. One SQLite file is the staging dataset that lives
outside NetSuite; exports are NetSuite CSV-import-ready.

Usage:
  python3 gik.py init                               create gik.db with schema + seed
  python3 gik.py donor add "Name" --type corporate --ns-id CUST-1003
  python3 gik.py donation new DON-2026-0001 --donor "Name" --date 2026-10-30 [--bol ...] [--pallets N]
  python3 gik.py intake DON-2026-0001 [--by cs] [--mock]  scanner loop (gun acts as keyboard)
  python3 gik.py lookup 0885909950805 [--mock]      research one barcode now
  python3 gik.py retry [--all]                      look up barcodes the free tier throttled during intake
                                                    (ISBNs go to Google Books first: free, ~1,000/day; set GOOGLE_BOOKS_KEY for a project quota)
  python3 gik.py review                             show the review queue
  python3 gik.py verify 00885909950805 [--fmv 12.99 --url https://... --category "School supplies"]
  python3 gik.py export je  --out exports/         journal-entry CSV (approach 1 + 3)
  python3 gik.py export items --out exports/       item-record CSV (approach 2)
  python3 gik.py export master                      master list CSV with Amazon/Walmart/Target links
  python3 gik.py export values                      THE LIST: item, UPC, and price at up to three merchants per barcode
  python3 gik.py master import exports/item_master.csv   pull reviewer edits (URLs, prices, status) back in
  python3 gik.py links 071417000242                 print the retailer search links for one barcode
  python3 gik.py pushed BATCH_ID --ns-tran 12345   mark an exported batch as pushed
  python3 gik.py report                            totals by donor and category
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("GIK_DB", os.path.join(HERE, "gik.db"))
UPCITEMDB_TRIAL = "https://api.upcitemdb.com/prod/trial/lookup?upc={gtin}"
UPCITEMDB_PAID = "https://api.upcitemdb.com/prod/v1/lookup?upc={gtin}"
GOOGLE_BOOKS = "https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}&country=US&maxResults=1"
OPEN_LIBRARY = "https://openlibrary.org/isbn/{isbn}.json"
MOCK_FILE = os.path.join(HERE, "mock_lookups.json")
MOCK_BOOKS_FILE = os.path.join(HERE, "mock_googlebooks.json")


OPEN_FACTS = [  # Open Food Facts family: free, open data (ODbL), no key, ~100 lookups/min. Names and categories, no prices.
    ("openfoodfacts", "https://world.openfoodfacts.org/api/v2/product/{code}.json?fields=product_name,brands,categories,quantity,code"),
    ("openproductsfacts", "https://world.openproductsfacts.org/api/v2/product/{code}.json?fields=product_name,brands,categories,quantity,code"),
    ("openbeautyfacts", "https://world.openbeautyfacts.org/api/v2/product/{code}.json?fields=product_name,brands,categories,quantity,code"),
]
OPEN_FACTS_CATEGORY = {"openfoodfacts": "Food & beverage", "openbeautyfacts": "Apparel & hygiene", "openproductsfacts": None}


_OPEN_FACTS_CALLS = {}  # source -> list of recent call timestamps (Open *Facts allow ~15 reads/min/IP and ban abusers)
OPEN_FACTS_PER_MIN = 12


def _open_facts_slot(source: str) -> bool:
    now = time.time()
    calls = [t for t in _OPEN_FACTS_CALLS.get(source, []) if now - t < 60]
    if len(calls) >= OPEN_FACTS_PER_MIN:
        _OPEN_FACTS_CALLS[source] = calls
        return False
    calls.append(now)
    _OPEN_FACTS_CALLS[source] = calls
    return True


def open_facts_order(hint_cat):
    """Which Open *Facts database to ask first, from the prefix hint. During intake only the first is asked;
    `retry` asks the rest."""
    if hint_cat == "Food & beverage":
        return ["openfoodfacts", "openproductsfacts", "openbeautyfacts"]
    if hint_cat in ("Apparel & hygiene", "Health & personal care"):
        return ["openbeautyfacts", "openproductsfacts", "openfoodfacts"]
    if hint_cat in ("School supplies", "Household & lighting", "Technology", "Furniture & equipment"):
        return ["openproductsfacts"]  # food/beauty DBs will not have these
    return ["openfoodfacts", "openproductsfacts", "openbeautyfacts"]


def fetch_open_facts(gtin14: str, mock=False, sources=None, max_calls=1):
    """Try the Open *Facts databases in turn (at most max_calls of them, paced). Returns (source, status, body, parsed)
    where parsed is dict(title, brand, category, raw_category) or None; (None, 0, ..) when paced out."""
    if mock:
        return None, 404, "{}", None
    code = gtin_for_lookup(gtin14)
    wanted = sources or [s for s, _ in OPEN_FACTS]
    last = (None, 404, "{}", None)
    tried = 0
    for source, url in OPEN_FACTS:
        if source not in wanted or tried >= max_calls:
            continue
        if not _open_facts_slot(source):
            return None, 0, json.dumps({"skipped": "local rate limit"}), None
        tried += 1
        st, body = http_get(url.format(code=code))
        last = (source, st, body, None)
        if st != 200:
            continue
        try:
            d = json.loads(body)
        except json.JSONDecodeError:
            continue
        p = d.get("product") or {}
        if d.get("status") != 1 or not p.get("product_name"):
            continue
        title = p["product_name"].strip()
        if p.get("quantity"):
            title += f" ({p['quantity']})"
        raw_cat = p.get("categories") or ""
        cat = OPEN_FACTS_CATEGORY[source] or map_category(raw_cat + " " + title)
        return source, st, body, {"title": title, "brand": (p.get("brands") or "").split(",")[0].strip() or None,
                                  "category": cat, "raw_category": raw_cat}
    return last


# GS1 company prefixes (leading digits of the 12-digit UPC form). A starting table for brands TEP sees;
# the barcode itself names the maker, so an item gets brand + category the instant it is scanned, before any lookup.
GS1_PREFIXES = [
    ("049000", "The Coca-Cola Company", "Food & beverage"), ("078000", "Keurig Dr Pepper", "Food & beverage"),
    ("012000", "PepsiCo Beverages", "Food & beverage"), ("028400", "Frito-Lay (PepsiCo)", "Food & beverage"),
    ("030000", "Quaker Oats (PepsiCo)", "Food & beverage"), ("038000", "Kellogg's", "Food & beverage"),
    ("016000", "General Mills", "Food & beverage"), ("028000", "Nestlé USA", "Food & beverage"),
    ("034000", "The Hershey Company", "Food & beverage"), ("040000", "Mars Inc.", "Food & beverage"),
    ("022000", "Wrigley (Mars)", "Food & beverage"), ("044000", "Nabisco (Mondelez)", "Food & beverage"),
    ("012546", "Mondelez (Trident, Dentyne)", "Food & beverage"), ("021000", "Kraft Heinz", "Food & beverage"),
    ("071662", "Crayola", "School supplies"), ("026000", "Elmer's (Newell)", "School supplies"),
    ("072818", "Dixon Ticonderoga", "School supplies"), ("043100", "Mead / Five Star (ACCO)", "School supplies"),
    ("072782", "Avery", "School supplies"), ("071641", "Sharpie / Paper Mate / Expo (Newell)", "School supplies"),
    ("070330", "BIC", "School supplies"), ("072838", "Pilot Pen", "School supplies"),
    ("078484", "Fiskars", "School supplies"), ("051141", "3M Post-it", "School supplies"),
    ("021200", "3M Scotch (consumer)", "School supplies"), ("051131", "3M", "School supplies"),
    ("029444", "Pacon (Fadeless, Tru-Ray)", "School supplies"), ("073333", "Scholastic", "Books & media"),
    ("043168", "GE Lighting", "Household & lighting"), ("017801", "Feit Electric", "Household & lighting"),
    ("037000", "Procter & Gamble", "Apparel & hygiene"), ("035000", "Colgate-Palmolive", "Apparel & hygiene"),
    ("036000", "Kimberly-Clark", "Apparel & hygiene"), ("041333", "Duracell", "Household & lighting"),
    ("039800", "Energizer", "Household & lighting"),
]


WAREHOUSE_LOCATIONS = [  # (code, rack, description) — from "Storage: Warehouse ABC Inventory", FY26 OPS layout
    ("A-RACK", "A", "A-Rack: bulk incentive overflow"), ("STEAM", "A", "STEAM"),
    ("B-CORE", "B", "B-Rack: core and incentives"), ("B01Z", "B", "B-Rack bin 01"), ("B02Z", "B", "B-Rack bin 02"),
    ("B-INC", "B", "B-Rack: incentives only"), ("B15Z", "B", "B-Rack bin 15"), ("B16Z", "B", "B-Rack bin 16"),
    ("C-RACK", "C", "C-Rack: writing utensils, copy paper 5-9"), ("C01Z", "C", "C-Rack bin 01"), ("C02Z", "C", "C-Rack bin 02"), ("C03Z", "C", "C-Rack bin 03"),
    ("D-RACK", "D", "D-Rack: glue, scissors, tape, erasers, paper items"), ("D01Z", "D", "D-Rack bin 01"), ("D02Z", "D", "D-Rack bin 02"), ("D03Z", "D", "D-Rack bin 03"),
    ("E-GEN", "E", "E-Rack: general overflow"), ("E-INC", "E", "E-Rack: incentive overflow"), ("E-OLDINC", "E", "E-Rack: old incentive overflow"),
    ("E15Z", "E", "E-Rack bin 15"), ("E16Z", "E", "E-Rack bin 16"), ("E17Z", "E", "E-Rack bin 17"), ("E18Z", "E", "E-Rack bin 18"),
    ("F-AAS", "F", "F-Rack: AAS"), ("OPS", "OPS", "OPS staging"), ("ESP", "ESP", "ESP"), ("WISHLIST", "ESP", "Wish List & ESP"),
    ("DOCK", "DOCK", "Receiving dock, not yet put away"),
]


def prefix_hint(conn, gtin14: str):
    """Longest matching GS1 prefix from the table → (company, category) or (None, None)."""
    upc12 = gtin14[-12:]
    row = conn.execute("""SELECT company, default_category FROM gs1_prefix
                          WHERE substr(?, 1, length(prefix)) = prefix ORDER BY length(prefix) DESC LIMIT 1""", (upc12,)).fetchone()
    return (row["company"], row["default_category"]) if row else (None, None)


def isbn_from_gtin(gtin14: str):
    """A GTIN whose EAN-13 form starts with 978 or 979 is a book ISBN; return the ISBN-13 or None."""
    ean13 = gtin14[-13:]
    return ean13 if ean13.startswith(("978", "979")) else None

# ---------------------------------------------------------------- barcodes

def normalize_gtin(raw: str):
    """Return a 14-digit GTIN with a valid check digit, or None."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) not in (8, 12, 13, 14):
        return None
    gtin = digits.zfill(14)
    body, check = gtin[:-1], int(gtin[-1])
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(body))
    if (10 - total % 10) % 10 != check:
        return None
    return gtin


def gtin_for_lookup(gtin14: str) -> str:
    """UPCitemdb prefers the UPC-A/EAN-13 form; strip the leading zeros."""
    s = gtin14.lstrip("0")
    return s.zfill(12) if len(s) <= 12 else s

# ---------------------------------------------------------------- retailer search links

RETAILERS = {
    "amazon":  "https://www.amazon.com/s?k={upc}",
    "walmart": "https://www.walmart.com/search?q={upc}",
    "target":  "https://www.target.com/s?searchTerm={upc}",
}
GOOGLE_SHOPPING = "https://www.google.com/search?tbm=shop&q={upc}"


def retail_search_urls(gtin14: str) -> dict:
    """Ready-made search pages for a barcode at each retailer (UPC-A form works best)."""
    upc = gtin_for_lookup(gtin14)
    return {r: url.format(upc=upc) for r, url in RETAILERS.items()}


def ensure_links(conn, gtin14: str):
    if is_generic(gtin14):
        return
    for retailer, url in retail_search_urls(gtin14).items():
        conn.execute("INSERT OR IGNORE INTO item_links (gtin, retailer, search_url) VALUES (?,?,?)",
                     (gtin14, retailer, url))

# ---------------------------------------------------------------- category rules

CATEGORY_RULES = [
    ("Food & beverage", r"food|beverage|snack|chips\b|soda|\bgum\b|candy|drink|juice|water\b|coffee|tea\b|cereal|granola|cracker|cookie|electrolyte|\bfl oz\b|\boz\b"),
    ("Books & media", r"\bbooks?\b|\bmedia\b|dvd|audio|magazine|textbook|\breader\b|reading|paperback|hardcover|publish"),
    ("Technology", r"electronic|computer|tablet|laptop|headphone|calculator|\busb\b|charger|\bcable\b|printer"),
    ("Health & personal care", r"supplement|vitamin|gummies|capsule|immune|medicine|first aid|bandage|sanitizer|wipes"),
    ("Apparel & hygiene", r"apparel|clothing|\bshoes?\b|\bsocks?\b|hygiene|toothbrush|\bsoap\b|deodorant|shampoo"),
    ("Furniture & equipment", r"furniture|\bchairs?\b|\bdesks?\b|\btables?\b|\bshelf\b|shelving|cabinet|equipment|appliance"),
    ("Household & lighting", r"light bulb|\bbulbs?\b|lighting|\blamps?\b|flashlight|\bled\b|\d+\s?w\b|watt|lumen|dimmable|home & garden|hardware|cleaning|\bbattery\b|batteries"),
    ("School supplies", r"office|school|stationery|\bpaper|\bpens?\b|pencil|marker|crayon|notebook|binder|folder|\bglue\b|scissor|backpack|\barts?\b|\bcrafts?\b|\btoys?\b|\bgames?\b|bulletin board"),
]
# Order matters for ties: food first (snack titles mention "party" and "art"), lighting before technology
# (a bulb may carry an "electronics" source category), school supplies last as the broadest net.
CATEGORY_RULES.insert(1, CATEGORY_RULES.pop([c for c, _ in CATEGORY_RULES].index("Household & lighting")))

# Sellers whose prices are not new-retail evidence (used books, marketplaces, liquidators).
SKIP_MERCHANTS = re.compile(r"ebay|marketplace|alibris|betterworld|abebooks|thriftbooks|biggerbooks|ecampus|vitalsource|"
                            r"mercari|poshmark|unbeatablesale|pricefalls|massgenie|onbuy|11 main|jet\.com|rakuten|sears", re.I)
USED_WORDS = re.compile(r"pre-owned|preowned|used|lightly used|good condition|acceptable|refurbished|ex-library", re.I)
STALE_DAYS = 730  # an offer older than two years gets flagged for a human check


def map_category(raw: str | None):
    text = (raw or "").lower()
    for cat, pattern in CATEGORY_RULES:
        if re.search(pattern, text):
            return cat
    return None

# ---------------------------------------------------------------- db

def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn):
    """Add columns introduced after the first release; safe to run every time."""
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(item_master)")}
    except sqlite3.Error:
        return
    if not cols:
        return
    for col, ddl in (("fmv_merchant", "TEXT"), ("notes", "TEXT"), ("offer_updated", "TEXT"), ("model_number", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE item_master ADD COLUMN {col} {ddl}")
    ocols = {r["name"] for r in conn.execute("PRAGMA table_info(offers)")}
    for col in ("match_level", "match_basis", "title"):
        if ocols and col not in ocols:
            conn.execute(f"ALTER TABLE offers ADD COLUMN {col} TEXT")
    rcols = {r["name"] for r in conn.execute("PRAGMA table_info(receipts)")}
    if "location" not in rcols:
        conn.execute("ALTER TABLE receipts ADD COLUMN location TEXT")  # rack/bin code, e.g. C03Z
    # Warehouse locations from the FY26 OPS layout; add rows as the floor changes.
    conn.execute("""CREATE TABLE IF NOT EXISTS locations (
        code TEXT PRIMARY KEY, rack TEXT NOT NULL, description TEXT, active INTEGER NOT NULL DEFAULT 1)""")
    conn.executemany("INSERT OR IGNORE INTO locations (code, rack, description) VALUES (?,?,?)", WAREHOUSE_LOCATIONS)
    conn.execute("""CREATE TABLE IF NOT EXISTS gs1_prefix (
        prefix TEXT PRIMARY KEY, company TEXT NOT NULL, default_category TEXT)""")
    # Every merchant price ever seen for a barcode, from any source. The values export picks three per item.
    conn.execute("""CREATE TABLE IF NOT EXISTS offers (
        offer_id   INTEGER PRIMARY KEY,
        gtin       TEXT NOT NULL,
        merchant   TEXT NOT NULL,
        price      REAL NOT NULL,
        url        TEXT,
        condition  TEXT,
        source     TEXT NOT NULL,         -- upcitemdb | googlebooks | ebay | sheet | manual
        offer_date TEXT,                  -- when the source last saw this price (may be old)
        seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
        usable     INTEGER NOT NULL DEFAULT 1,   -- 0 = used/marketplace/reseller, kept for the record
        match_level TEXT,                 -- confirmed | probable | rejected: is this listing the same item? (see match_offer)
        match_basis TEXT,                 -- why: barcode lookup, model number, barcode in title, name only, reviewer
        title      TEXT,                  -- the listing's own title, kept so a reviewer can judge the match
        UNIQUE (gtin, merchant, price, source))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_gtin ON offers(gtin)")
    # Non-barcoded goods: a searchable catalog of generic items with thrift-value ranges. Each gets a code
    # (G-000123) that lives in item_master like a barcode, so receipts, exports and NetSuite all work unchanged.
    conn.execute("""CREATE TABLE IF NOT EXISTS generic_items (
        code        TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        category    TEXT,
        keywords    TEXT,
        value_low   REAL,
        value_high  REAL,
        unit        TEXT DEFAULT 'each',
        source      TEXT,
        status      TEXT NOT NULL DEFAULT 'ok' CHECK (status IN ('ok','needs_value')),
        added_by    TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')))""")
    if conn.execute("SELECT COUNT(*) FROM generic_items").fetchone()[0] == 0:
        seed = os.path.join(HERE, "generic_items.csv")
        if os.path.exists(seed):
            with open(seed, newline="", encoding="utf-8") as f:
                for i, r in enumerate(csv.DictReader(f), start=1):
                    add_generic(conn, r["name"], r["category"], r.get("keywords", ""), float(r["value_low"]), float(r["value_high"]),
                                r.get("unit") or "each", r.get("source") or "", "seed", code=f"G-{i:06d}", commit=False)
    conn.executemany("INSERT OR IGNORE INTO gs1_prefix VALUES (?,?,?)", GS1_PREFIXES)
    conn.execute("""INSERT OR IGNORE INTO gl_map VALUES
        ('Household & lighting','1300','GIK Inventory','4350','In-Kind Contributions - Household','101','Program: Resource Center'),
        ('Health & personal care','1300','GIK Inventory','4360','In-Kind Contributions - Health','101','Program: Resource Center'),
        ('Food & beverage','1300','GIK Inventory','4370','In-Kind Contributions - Food','101','Program: Resource Center')""")
    conn.commit()


def cmd_init(_args):
    conn = connect()
    with open(os.path.join(HERE, "schema.sql")) as f:
        conn.executescript(f.read())
    with open(os.path.join(HERE, "seed.sql")) as f:
        conn.executescript(f.read())
    conn.commit()
    migrate(conn)
    print(f"Initialized {DB_PATH}")

# ---------------------------------------------------------------- generic (non-barcoded) items

GENERIC_GL = {"Appliances": "Household & lighting", "Household goods": "Household & lighting", "Seasonal & decor": "Household & lighting",
              "Furniture": "Furniture & equipment", "Clothing": "Apparel & hygiene", "Office": "School supplies",
              "Kids & toys": "School supplies", "Miscellaneous": None}
GUIDE_URL = "https://satruck.org/Home/DonationValueGuide"


def is_generic(code: str) -> bool:
    return (code or "").startswith("G-")


def next_generic_code(conn) -> str:
    row = conn.execute("SELECT code FROM generic_items ORDER BY code DESC LIMIT 1").fetchone()
    n = int(row["code"][2:]) + 1 if row else 1
    return f"G-{n:06d}"


def add_generic(conn, name, category, keywords, low, high, unit="each", source="", added_by="", code=None, commit=True):
    """Create a generic item and its item_master twin. With no values yet it goes to needs_value (photo + staff)."""
    code = code or next_generic_code(conn)
    has_value = low is not None and high is not None
    conn.execute("""INSERT OR IGNORE INTO generic_items (code, name, category, keywords, value_low, value_high, unit, source, status, added_by)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                 (code, name.strip(), category, (keywords or "").strip(), low, high, unit, source,
                  "ok" if has_value else "needs_value", added_by))
    gl_cat = map_category(f"{name} {keywords or ''}") or GENERIC_GL.get(category) or None
    mid = round((low + high) / 2, 2) if has_value else None
    guide_based = has_value and "estimate" not in (source or "").lower()
    conn.execute("""INSERT INTO item_master (gtin, name, brand, category, raw_category, fmv_unit, fmv_basis, fmv_source_url, fmv_merchant,
                                             fmv_captured_at, fmv_status, lookup_source, notes, updated_at)
                    VALUES (?,?,?,?,?,?,'appraised',?,?,?,?,?,?,?)
                    ON CONFLICT(gtin) DO UPDATE SET name=excluded.name, category=COALESCE(item_master.category, excluded.category),
                      fmv_unit=COALESCE(excluded.fmv_unit, item_master.fmv_unit), fmv_status=excluded.fmv_status,
                      fmv_source_url=excluded.fmv_source_url, fmv_merchant=excluded.fmv_merchant, notes=excluded.notes, updated_at=excluded.updated_at""",
                 (code, name.strip(), None, gl_cat, f"Generic > {category}", mid, GUIDE_URL if has_value else None,
                  (source or None), datetime.now().isoformat(timespec="seconds") if has_value else None,
                  "verified" if guide_based else ("unverified" if has_value else "not_found"), "generic",
                  (f"thrift value guide: ${low:g}-${high:g} {unit}, midpoint used" if has_value else "no value yet: take a photo, name it <code>.jpg in photos/, staff will price it").replace("<code>", code),
                  datetime.now().isoformat(timespec="seconds")))
    if has_value:
        record_offers(conn, code, [{"merchant": source or "value guide", "price": mid, "link": GUIDE_URL, "condition": "Used", "updated_t": int(time.time())}], "manual")
    if commit:
        conn.commit()
    return code


def search_generic(conn, text: str, limit=8):
    """Simple ranked search over name + keywords: every typed word must appear; name matches rank first."""
    words = [w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 1]
    if not words:
        return []
    out = []
    for r in conn.execute("SELECT * FROM generic_items"):
        hay_name = r["name"].lower()
        hay = hay_name + " " + (r["keywords"] or "").lower() + " " + (r["category"] or "").lower()
        if all(any(w in tok or tok.startswith(w) or (w.endswith("s") and w[:-1] in tok) for tok in re.findall(r"[a-z0-9']+", hay)) or w in hay for w in words):
            score = sum(3 for w in words if w in hay_name) + sum(1 for w in words if w in hay)
            if hay_name.startswith(words[0]):
                score += 2
            out.append((score, r))
    out.sort(key=lambda t: (-t[0], t[1]["name"]))
    return [r for _, r in out[:limit]]


def fmt_generic(r) -> str:
    val = f"${r['value_low']:g}-${r['value_high']:g}" if r["value_low"] is not None else "no value yet"
    return f"{r['name']:<42} {r['category'] or '':<18} {val:<14} {r['code']}"


def cmd_find(args):
    conn = connect()
    hits = search_generic(conn, " ".join(args.words), limit=15)
    if not hits:
        print("No match. Add it with:  gik.py generic add \"Item name\" --category \"Household goods\" [--low 2 --high 8]")
        return
    for r in hits:
        print("  " + fmt_generic(r))
    print(f"\n{len(hits)} match(es). Values are thrift-guide ranges per {hits[0]['unit']}; the midpoint is used unless staff set otherwise.")


def _generic_args(a):
    """`generic value G-000123` passes the code in the name slot; make both spellings work."""
    if a.action == "value":
        a.code = a.name
    return a


def cmd_generic(args):
    conn = connect()
    if args.action == "add":
        code = add_generic(conn, args.name, args.category or "Household goods", args.keywords or "", args.low, args.high,
                           args.unit or "each", args.source or ("TEP estimate (thrift value); confirm" if args.low is not None else ""), args.by or "staff")
        print(f"Added {code}: {args.name}" + ("" if args.low is not None else f"  (needs a value; photo → photos/{code}.jpg)"))
    elif args.action == "value":
        r = conn.execute("SELECT * FROM generic_items WHERE code=?", (args.code,)).fetchone()
        if not r:
            sys.exit(f"{args.code} not found")
        conn.execute("UPDATE generic_items SET value_low=?, value_high=?, status='ok', source=COALESCE(?, source) WHERE code=?",
                     (args.low, args.high, args.source, args.code))
        mid = round((args.low + args.high) / 2, 2)
        conn.execute("""UPDATE item_master SET fmv_unit=?, fmv_status='verified', fmv_source_url=COALESCE(?, ?), fmv_merchant=COALESCE(?, 'staff valuation'),
                        fmv_captured_at=?, notes=? WHERE gtin=?""",
                     (mid, args.url, GUIDE_URL, args.source, datetime.now().isoformat(timespec="seconds"),
                      f"staff valued ${args.low:g}-${args.high:g}, midpoint used", args.code))
        record_offers(conn, args.code, [{"merchant": args.source or "staff valuation", "price": mid, "link": args.url or GUIDE_URL,
                                         "condition": "Used", "updated_t": int(time.time())}], "manual")
        conn.commit()
        print(f"{args.code} {r['name']}: ${args.low:g}-${args.high:g}, using ${mid:.2f}")
    else:  # pending
        photos = os.path.join(HERE, "photos")
        rows = conn.execute("""SELECT g.*, COALESCE(SUM(r.qty),0) units FROM generic_items g LEFT JOIN receipts r ON r.gtin=g.code
                               WHERE g.status='needs_value' GROUP BY g.code ORDER BY units DESC""").fetchall()
        if not rows:
            print("No generic items waiting for a value."); return
        print(f"{'Code':<10}{'Units':>6}  {'Photo':<6} Item")
        for r in rows:
            has_photo = any(os.path.exists(os.path.join(photos, f"{r['code']}{ext}")) for ext in (".jpg", ".jpeg", ".png", ".heic"))
            print(f"{r['code']:<10}{r['units']:>6}  {'yes' if has_photo else '--':<6} {r['name']}")
        print(f"\nSet a value with:  gik.py generic value G-000123 --low 5 --high 20 [--source \"eBay sold listings\" --url https://...]")

# ---------------------------------------------------------------- lookup

def fetch_upcitemdb(gtin14: str, api_key: str | None):
    url = (UPCITEMDB_PAID if api_key else UPCITEMDB_TRIAL).format(gtin=gtin_for_lookup(gtin14))
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "TEP-GIK-intake/0.1"})
    if api_key:
        req.add_header("user_key", api_key)
        req.add_header("key_type", "3scale")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # network down, DNS, proxy
        return 0, json.dumps({"error": str(e)})


def http_get(url: str, headers: dict | None = None):
    ua = os.environ.get("GIK_USER_AGENT", "TEP-GIK-Intake/0.3 (The Education Partnership, Pittsburgh; infosystems@theeducationpartnership.org)")
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": ua, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, json.dumps({"error": str(e)})


def fetch_googlebooks(isbn13: str, mock=False):
    if mock:
        if not os.path.exists(MOCK_BOOKS_FILE):
            return 200, json.dumps({"totalItems": 0})
        with open(MOCK_BOOKS_FILE) as f:
            hit = json.load(f).get(isbn13)
        return (200, json.dumps({"totalItems": 1, "items": [hit]})) if hit else (200, json.dumps({"totalItems": 0}))
    url = GOOGLE_BOOKS.format(isbn=isbn13)
    key = google_books_key()
    if key:
        url += "&key=" + key
    return http_get(url)


def google_books_key():
    """Free API key: env var GOOGLE_BOOKS_KEY, or the first line of google_books_key.txt next to this script.
    Without a key, requests share a public quota that is usually exhausted, so get one (see README)."""
    key = os.environ.get("GOOGLE_BOOKS_KEY")
    if key:
        return key.strip()
    path = os.path.join(HERE, "google_books_key.txt")
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    return None


def fetch_openlibrary(isbn13: str, mock=False):
    if mock:
        return 404, "{}"
    return http_get(OPEN_LIBRARY.format(isbn=isbn13))


def parse_googlebooks(data: dict):
    """Return dict(title, brand, category, price, url, merchant) or None if no volume."""
    items = data.get("items") or []
    if not items:
        return None
    v = items[0]
    info = v.get("volumeInfo") or {}
    sale = v.get("saleInfo") or {}
    title = info.get("title") or ""
    if info.get("subtitle"):
        title += ": " + info["subtitle"]
    authors = ", ".join(info.get("authors") or [])
    if authors:
        title += f" - by {authors}"
    price = None
    merchant = None
    for key, label in (("listPrice", "Google Books (publisher list price)"), ("retailPrice", "Google Books (Play Store price)")):
        p = sale.get(key) or {}
        if p.get("amount") and p.get("currencyCode", "USD") == "USD":
            price, merchant = round(float(p["amount"]), 2), label
            break
    url = sale.get("buyLink") or info.get("infoLink") or info.get("canonicalVolumeLink")
    return {"title": title, "brand": info.get("publisher"), "category": "Books & media",
            "raw_category": "Books > " + " / ".join(info.get("categories") or []), "price": price, "url": url, "merchant": merchant}


# ---------------------------------------------------------------- UPC Database (upcdatabase.org, free key, 100 lookups/day)

UPCDATABASE_URL = "https://api.upcdatabase.org/product/{code}"


def upcdatabase_key():
    key = os.environ.get("UPCDATABASE_KEY")
    if key:
        return key.strip()
    path = os.path.join(HERE, "upcdatabase_key.txt")
    if os.path.exists(path):
        lines = [l.strip() for l in open(path) if l.strip() and not l.strip().startswith("#")]
        if lines:
            return lines[0]
    return None


def fetch_upcdatabase(gtin14: str):
    """Second general source. Returns (status, body, item) with item in UPCitemdb's shape
    (title, brand, category, offers[]) so the same pricing rules apply; item is None on a miss."""
    key = upcdatabase_key()
    if not key:
        return 0, json.dumps({"error": "no UPC Database key"}), None
    st, body = http_get(UPCDATABASE_URL.format(code=gtin_for_lookup(gtin14)), headers={"Authorization": "Bearer " + key})
    if st != 200:
        return st, body, None
    item = parse_upcdatabase(body, gtin14)
    return (st, body, item) if item else (404, body, None)


def parse_upcdatabase(body: str, gtin14: str):
    try:
        d = json.loads(body)
    except json.JSONDecodeError:
        return None
    if str(d.get("success")).lower() != "true" or not (d.get("title") or d.get("description")):
        return None
    offers = []
    for s in d.get("stores") or []:
        if not isinstance(s, dict) or not s.get("price"):
            continue
        link = s.get("link") or s.get("url") or next((v for v in s.values() if isinstance(v, str) and v.startswith("http")), None)
        offers.append({"merchant": s.get("store") or "store", "price": s["price"], "link": link, "condition": "New",
                       "title": d.get("title") or "", "updated_t": int(time.time())})
    if d.get("msrp") not in (None, "", "0", 0, "0.00"):
        offers.append({"merchant": "MSRP (UPC Database)", "price": d["msrp"], "link": f"https://upcdatabase.org/code/{gtin_for_lookup(gtin14)}",
                       "condition": "New", "title": d.get("title") or "", "updated_t": int(time.time())})
    return {"title": d.get("title") or d.get("description") or "", "brand": d.get("brand") or d.get("manufacturer"),
            "category": d.get("category") or "", "offers": offers}


# ---------------------------------------------------------------- eBay Browse API (free developer account, 5,000 calls/day)

EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search?gtin={gtin}&filter=conditions:{{NEW}},buyingOptions:{{FIXED_PRICE}},deliveryCountry:US&limit=20"
_EBAY_TOKEN = {"value": None, "expires": 0}


def ebay_keys():
    """Client ID and secret: env EBAY_CLIENT_ID / EBAY_CLIENT_SECRET, or two lines in ebay_keys.txt next to this script."""
    cid, sec = os.environ.get("EBAY_CLIENT_ID"), os.environ.get("EBAY_CLIENT_SECRET")
    if cid and sec:
        return cid.strip(), sec.strip()
    path = os.path.join(HERE, "ebay_keys.txt")
    if os.path.exists(path):
        lines = [l.strip() for l in open(path) if l.strip() and not l.strip().startswith("#")]
        if len(lines) >= 2:
            return lines[0], lines[1]
    return None, None


def ebay_token():
    import base64
    cid, sec = ebay_keys()
    if not cid:
        return None
    if _EBAY_TOKEN["value"] and time.time() < _EBAY_TOKEN["expires"] - 60:
        return _EBAY_TOKEN["value"]
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    data = "grant_type=client_credentials&scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope".encode()
    req = urllib.request.Request(EBAY_TOKEN_URL, data=data, method="POST",
                                 headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            tok = json.loads(resp.read().decode())
    except Exception as e:
        print(f"  ! eBay token error: {e}")
        return None
    _EBAY_TOKEN["value"] = tok.get("access_token")
    _EBAY_TOKEN["expires"] = time.time() + int(tok.get("expires_in", 7200))
    return _EBAY_TOKEN["value"]


def fetch_ebay(gtin14: str):
    """Returns (status, body, offers) where offers are in the same shape UPCitemdb uses, so pick_fmv/record_offers work."""
    token = ebay_token()
    if not token:
        return 0, json.dumps({"error": "no eBay keys"}), []
    st, body = http_get(EBAY_SEARCH_URL.format(gtin=gtin_for_lookup(gtin14)),
                        headers={"Authorization": "Bearer " + token, "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
    offers = []
    if st == 200:
        try:
            for s in json.loads(body).get("itemSummaries") or []:
                p = (s.get("price") or {})
                if p.get("currency", "USD") != "USD" or not p.get("value"):
                    continue
                offers.append({"merchant": "eBay (" + ((s.get("seller") or {}).get("username") or "seller") + ")",
                               "domain": "ebay.com", "price": float(p["value"]), "link": s.get("itemWebUrl"),
                               "condition": "New", "title": s.get("title") or "", "updated_t": int(time.time())})
        except (json.JSONDecodeError, ValueError):
            pass
    return st, body, offers


def ebay_value(offers):
    """eBay listings vary widely; use the median of new fixed-price listings and the listing nearest it as evidence."""
    if len(offers) < 2:
        return None
    prices = sorted(o["price"] for o in offers)
    med = statistics.median(prices)
    best = min(offers, key=lambda o: abs(o["price"] - med))
    return {"price": round(med, 2), "url": best["link"], "merchant": "eBay (median of %d new listings)" % len(offers), "n": len(offers)}


def fetch_mock(gtin14: str):
    if not os.path.exists(MOCK_FILE):
        return 404, json.dumps({"code": "INVALID_UPC", "message": "no mock file"})
    with open(MOCK_FILE) as f:
        mocks = json.load(f)
    hit = mocks.get(gtin14) or mocks.get(gtin_for_lookup(gtin14))
    if hit:
        return 200, json.dumps({"code": "OK", "total": 1, "items": [hit]})
    return 404, json.dumps({"code": "INVALID_UPC", "total": 0, "items": []})


def clean_merchant(name: str) -> str:
    n = (name or "").strip()
    low = n.lower()
    for key, label in (("wal-mart", "Walmart"), ("walmart.com", "Walmart"), ("target", "Target"), ("amazon", "Amazon"),
                       ("best buy", "Best Buy"), ("bestbuy", "Best Buy"), ("home depot", "Home Depot"), ("lowe", "Lowe's"),
                       ("staples", "Staples"), ("office depot", "Office Depot"), ("booksamillion", "Books-A-Million"),
                       ("barnes", "Barnes & Noble"), ("kroger", "Kroger"), ("ebay", "eBay")):
        if key in low and "marketplace" not in low:
            return label
    return n or "Unknown"


# ---------------------------------------------------------------- is this listing the same item? (evidence rules)
# confirmed = found by the barcode itself (store API, UPC database, eBay GTIN search, Open Prices, Kroger record),
#             or the listing shows our barcode or our model number, or a reviewer said so.
# probable  = found by name only (e.g. Google Shopping): shown to reviewers, never counted as proof until confirmed.
# rejected  = the listing is a different pack size (a "lot of 8" price is not the price of one pack); kept, never used.
BARCODE_SOURCES = {
    "upcitemdb": "Store listing returned for this barcode (UPCitemdb)",
    "upcdatabase": "MSRP returned for this barcode (UPC Database)",
    "googlebooks": "ISBN lookup (Google Books)",
    "openlibrary": "ISBN lookup (Open Library)",
    "ebay": "eBay search by barcode (GTIN)",
    "openprices": "Shelf price reported for this barcode (Open Prices)",
    "kroger": "Kroger product record for this UPC",
    "walmart": "Walmart product record for this UPC",
    "bestbuy": "Best Buy product record for this UPC",
    "manual": "Entered by a staff reviewer",
    "sheet": "Entered by a staff reviewer (Google Sheet)",
    "mock": "Test data",
}
NAME_SOURCES = {"serper": "Google Shopping"}   # found by searching the item name: needs proof of the same item

PACK_RE = re.compile(r"\b(?:pack|pk|lot|set|case|box)\s*(?:of\s*)?(\d{1,4})\b"
                     r"|\b(\d{1,4})\s*-?\s*(?:pk|packs?|ct|count|pcs?|pieces?)\b", re.I)
LOT_RE = re.compile(r"\b(?:lot|case|bulk)\s*(?:of\s*)?\d{1,4}\b", re.I)   # "Lot of 8", "case of 12": many packs in one price
_UNIT_MODEL = re.compile(r"\d+(?:W|V|A|OZ|FLOZ|CT|PK|MM|CM|IN|FT|LB|LBS|ML|L|G|KG|MAH|GB|TB|MB|HZ|K|PC|PCS|QT|GAL)", re.I)


def pack_counts(text):
    """Pack/count numbers stated in a name or title: '2pk', '3-Pack', 'Pack of 6', 'Lot 8', '24 ct' -> {2}, {3}, {6}, {8}, {24}."""
    out = set()
    for m in PACK_RE.finditer(text or ""):
        n = int(m.group(1) or m.group(2))
        if 0 < n < 10000:
            out.add(n)
    return out


def clean_model(model):
    """A model number we can trust for matching, upper-case letters/digits only; None when it is too short or generic
    (a size like 60W or 12OZ, a year, a bare count)."""
    m = re.sub(r"[^A-Z0-9]", "", (model or "").upper())
    if len(m) < 4 or not re.search(r"\d", m) or (m.isdigit() and len(m) < 5) or _UNIT_MODEL.fullmatch(m):
        return None
    if re.fullmatch(r"(?:N/?A|NONE|NULL|UNKNOWN)\d*", m):
        return None
    return m


def model_in_text(model, text):
    """True when the model number appears in the text as its own token (separators like - / . or spaces allowed)."""
    m = clean_model(model)
    if not m or not text:
        return False
    pat = r"(?<![A-Z0-9])" + r"[\s\-/._]*".join(re.escape(ch) for ch in m) + r"(?![A-Z0-9])"
    return re.search(pat, text.upper()) is not None


def barcode_in_text(gtin14, text):
    """True when the item's own barcode (with or without leading zeros) is printed in the text."""
    core = (gtin14 or "").lstrip("0")
    return len(core) >= 8 and re.search(r"(?<!\d)0*" + re.escape(core) + r"(?!\d)", text or "") is not None


def match_offer(gtin14, source, title, item_name=None, model=None, level=None, basis=None):
    """Decide (match_level, match_basis) for one offer. An explicit level (e.g. from a reviewer) is kept."""
    if level in ("confirmed", "probable", "rejected"):
        return level, basis or ""
    mine, theirs, lots = pack_counts(item_name), pack_counts(title), pack_counts(" ".join(LOT_RE.findall(title or "")))
    if (mine and theirs - mine) or (lots - mine):
        shown = lambda c: "/".join(map(str, sorted(c))) or "not stated"
        return "rejected", f"Pack size differs (item {shown(mine)} vs listing {shown(theirs | lots)}); a reviewer can override"
    if source in NAME_SOURCES:
        where = NAME_SOURCES[source]
        if barcode_in_text(gtin14, title):
            return "confirmed", f"Barcode shown in the listing ({where})"
        if model_in_text(model, title):
            return "confirmed", f"Model number {clean_model(model)} shown in the listing ({where})"
        return "probable", f"Name match only ({where}); needs a reviewer"
    return "confirmed", BARCODE_SOURCES.get(source, f"Looked up by barcode ({source})")


def record_offers(conn, gtin14: str, offers, source: str):
    """Save every offer a lookup returned, flagged usable or not by the same rules pick_fmv applies, and with a
    match level saying whether the listing is provably the same item (see match_offer)."""
    item = conn.execute("SELECT name, model_number FROM item_master WHERE gtin=?", (gtin14,)).fetchone()
    item_name, model = (item["name"], item["model_number"]) if item else (None, None)
    for o in offers or []:
        price = o.get("price")
        if price in (None, "", 0, "0"):
            continue
        merchant_raw = o.get("merchant") or o.get("domain") or ""
        if source in ("manual", "sheet"):
            usable = True  # a person chose this value (thrift guide or confirmed store page); condition is theirs to judge
        else:
            usable = not ((source != "ebay" and SKIP_MERCHANTS.search(merchant_raw)) or USED_WORDS.search(o.get("title") or "")
                          or (o.get("condition") and o["condition"].lower() != "new"))
        level, basis = match_offer(gtin14, source, o.get("title") or "", item_name, model, o.get("match_level"), o.get("match_basis"))
        if level == "rejected":
            usable = False
        ts = int(o.get("updated_t") or 0)
        offer_date = datetime.fromtimestamp(ts).date().isoformat() if ts else None
        try:
            conn.execute("""INSERT OR IGNORE INTO offers (gtin, merchant, price, url, condition, source, offer_date, usable,
                                                          match_level, match_basis, title)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                         (gtin14, clean_merchant(merchant_raw), round(float(price), 2), o.get("link"), o.get("condition"),
                          source, offer_date, 1 if usable else 0, level, basis, (o.get("title") or "")[:300] or None))
        except (ValueError, sqlite3.Error):
            continue


def fill_models(conn):
    """Copy the manufacturer model number from saved UPCitemdb answers onto items that have none yet.
    Free: re-reads lookups already made. Returns how many items got a model number."""
    rows = conn.execute("""SELECT im.gtin, l.raw_response FROM item_master im
                           JOIN lookup_log l ON l.gtin = im.gtin AND l.source = 'upcitemdb' AND l.http_status = 200
                           WHERE im.model_number IS NULL AND l.raw_response IS NOT NULL
                           ORDER BY l.requested_at DESC""").fetchall()
    done = set()
    for r in rows:
        if r["gtin"] in done:
            continue
        try:
            items = (json.loads(r["raw_response"]) or {}).get("items") or []
        except (json.JSONDecodeError, TypeError, AttributeError):
            continue
        model = next((clean_model(it.get("model")) for it in items if isinstance(it, dict) and clean_model(it.get("model"))), None)
        if model:
            conn.execute("UPDATE item_master SET model_number=? WHERE gtin=? AND model_number IS NULL",
                         (model, r["gtin"]))
            done.add(r["gtin"])
    conn.commit()
    return len(done)


def pick_fmv(offers):
    """Pick the cheapest NEW-condition retail offer at or above the median, skipping used-book
    sellers and marketplaces. Returns dict(price, url, merchant, n, newest_ts, note) — price, URL
    and merchant always come from the SAME offer, so the evidence link matches the number posted."""
    usable = []
    for o in offers:
        price = o.get("price")
        if price in (None, "", 0, "0"):
            continue
        merchant = o.get("merchant") or o.get("domain") or ""
        if SKIP_MERCHANTS.search(merchant) or USED_WORDS.search(o.get("title") or "") or \
           (o.get("condition") and o["condition"].lower() != "new"):
            continue
        usable.append((float(price), o.get("link") or "", merchant, int(o.get("updated_t") or 0)))
    if not usable:
        return {"price": None, "url": None, "merchant": None, "n": 0, "newest_ts": 0, "note": "no new-condition retail offer"}
    med = statistics.median([u[0] for u in usable])
    price, url, merchant, _ = sorted([u for u in usable if u[0] >= med], key=lambda u: u[0])[0]
    newest = max(u[3] for u in usable)
    note = ""
    if newest and (datetime.now().timestamp() - newest) > STALE_DAYS * 86400:
        note = f"offers last seen {datetime.fromtimestamp(newest).year}; confirm current price"
    if len(usable) < 2:
        note = (note + "; " if note else "") + "single offer"
    return {"price": round(price, 2), "url": url, "merchant": merchant, "n": len(usable), "newest_ts": newest, "note": note}


def fetch_openprices(gtin14: str):
    """Open Prices (prices.openfoodfacts.org): free, keyless, crowd-sourced shelf prices from receipts and price tags.
    Returns (http_status, raw_body, offers) with USD prices from the last two years only."""
    code = gtin14[1:] if gtin14.startswith("0") else gtin14  # Open Food Facts stores UPC-A as 13-digit EAN
    st, body = http_get(f"https://prices.openfoodfacts.org/api/v1/prices?product_code={code}&size=50&order_by=-date")
    offers = []
    if st == 200:
        try:
            cutoff = (datetime.now() - timedelta(days=STALE_DAYS)).date().isoformat()
            for it in json.loads(body).get("items") or []:
                if (it.get("currency") or "").upper() != "USD" or not it.get("price") or (it.get("date") or "") < cutoff:
                    continue
                loc = it.get("location") or {}
                merchant = loc.get("osm_name") or loc.get("osm_brand") or "store (Open Prices)"
                offers.append({"merchant": merchant, "price": float(it["price"]), "condition": "New",
                               "link": f"https://prices.openfoodfacts.org/prices/{it.get('id')}" if it.get("id") else
                                       f"https://prices.openfoodfacts.org/products/{code}",
                               "title": (it.get("product") or {}).get("product_name") or "",
                               "updated_t": int(datetime.fromisoformat(it["date"]).timestamp()) if it.get("date") else 0})
        except (ValueError, KeyError, TypeError):
            pass
    return st, body, offers


def maybe_openprices(conn, gtin14: str, now: str):
    """If the item still has no price, try Open Prices; take the median USD shelf price, with the matching receipt link."""
    if gtin14.startswith("G-"):
        return None
    row = conn.execute("SELECT fmv_unit, fmv_status FROM item_master WHERE gtin=?", (gtin14,)).fetchone()
    if row and (row["fmv_unit"] is not None or row["fmv_status"] == "verified"):
        return None
    if conn.execute("SELECT 1 FROM lookup_log WHERE gtin=? AND source='openprices' AND requested_at > datetime('now','-7 days')", (gtin14,)).fetchone():
        return None  # asked recently; crowd data changes slowly
    st, body, offers = fetch_openprices(gtin14)
    conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES (?,?,?,?)", (gtin14, "openprices", st, body[:20000]))
    record_offers(conn, gtin14, offers, "openprices")
    if not offers:
        return None
    ranked = sorted(offers, key=lambda o: o["price"])
    pick = ranked[len(ranked) // 2]
    note = f"Open Prices median of {len(offers)} shelf price(s); confirm at a store" + (" (single report)" if len(offers) == 1 else "")
    conn.execute("""INSERT INTO item_master (gtin, name, fmv_unit, fmv_source_url, fmv_merchant, fmv_captured_at, fmv_status, lookup_source, notes, updated_at)
                    VALUES (?,?,?,?,?,?,'unverified','openprices',?,?)
                    ON CONFLICT(gtin) DO UPDATE SET name=COALESCE(item_master.name, excluded.name),
                      fmv_unit=excluded.fmv_unit, fmv_source_url=excluded.fmv_source_url, fmv_merchant=excluded.fmv_merchant,
                      fmv_captured_at=excluded.fmv_captured_at, fmv_status='unverified', notes=excluded.notes, updated_at=excluded.updated_at""",
                 (gtin14, (pick.get("title") or None), pick["price"], pick["link"], pick["merchant"], now, note, now))
    return {"price": pick["price"], "url": pick["link"], "merchant": pick["merchant"], "n": len(offers), "note": note}


# Extra price sources live in extra_sources.py next to this file, so a new source is one small file, not a new gik.py.
# Each entry: name -> fetch(gtin14, http_get) returning (http_status, raw_body, offers, info) where offers are dicts
# {merchant, price, link, condition, title, updated_t} and info may carry {"name", "brand", "note"}.
try:
    import extra_sources
except Exception as _e:  # missing or broken file must never stop scanning or lookups
    extra_sources = None


def maybe_extra_sources(conn, gtin14: str, now: str):
    if gtin14.startswith("G-") or extra_sources is None:
        return None
    for name, fetch in getattr(extra_sources, "PRICE_SOURCES", {}).items():
        row = conn.execute("SELECT fmv_unit, fmv_status FROM item_master WHERE gtin=?", (gtin14,)).fetchone()
        if row and (row["fmv_unit"] is not None or row["fmv_status"] == "verified"):
            return None
        if conn.execute("SELECT 1 FROM lookup_log WHERE gtin=? AND source=? AND requested_at > datetime('now','-7 days')",
                        (gtin14, name)).fetchone():
            continue
        try:
            st, body, offers, info = fetch(gtin14, http_get)
        except Exception as e:
            st, body, offers, info = 0, json.dumps({"error": str(e)}), [], {}
        if st is None:  # source not configured (no keys): skip silently, nothing logged
            continue
        conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES (?,?,?,?)", (gtin14, name, st, (body or "")[:20000]))
        if info.get("name") or info.get("brand"):
            conn.execute("UPDATE item_master SET name=COALESCE(name, ?), brand=COALESCE(brand, ?) WHERE gtin=?",
                         (info.get("name"), info.get("brand"), gtin14))
        record_offers(conn, gtin14, offers, name)
        good = sorted((o for o in offers if o.get("price")), key=lambda o: o["price"])
        if not good:
            continue
        pick = good[len(good) // 2]
        note = info.get("note") or f"{name} price; confirm at a store"
        conn.execute("""INSERT INTO item_master (gtin, name, brand, fmv_unit, fmv_source_url, fmv_merchant, fmv_captured_at, fmv_status, lookup_source, notes, updated_at)
                        VALUES (?,?,?,?,?,?,?,'unverified',?,?,?)
                        ON CONFLICT(gtin) DO UPDATE SET name=COALESCE(item_master.name, excluded.name), brand=COALESCE(item_master.brand, excluded.brand),
                          fmv_unit=excluded.fmv_unit, fmv_source_url=excluded.fmv_source_url, fmv_merchant=excluded.fmv_merchant,
                          fmv_captured_at=excluded.fmv_captured_at, fmv_status='unverified', lookup_source=excluded.lookup_source,
                          notes=excluded.notes, updated_at=excluded.updated_at""",
                     (gtin14, info.get("name") or pick.get("title") or None, info.get("brand"), pick["price"], pick.get("link"),
                      pick.get("merchant"), now, name, note, now))
        return {"price": pick["price"], "url": pick.get("link"), "merchant": pick.get("merchant"), "n": len(good), "note": note}
    return None


def maybe_ebay(conn, gtin14: str, now: str):
    """If the item still has no price and eBay keys are configured, price it from new fixed-price eBay listings."""
    ex = maybe_extra_sources(conn, gtin14, now)
    if ex:
        return ex
    op = maybe_openprices(conn, gtin14, now)
    if op:
        return op
    if not ebay_keys()[0]:
        return None
    row = conn.execute("SELECT fmv_unit, fmv_status FROM item_master WHERE gtin=?", (gtin14,)).fetchone()
    if row and (row["fmv_unit"] is not None or row["fmv_status"] == "verified"):
        return None
    st, body, offers = fetch_ebay(gtin14)
    conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES (?,?,?,?)", (gtin14, "ebay", st, body))
    record_offers(conn, gtin14, offers, "ebay")
    val = ebay_value(offers)
    if val:
        name = (offers[0].get("title") or "")[:120]
        conn.execute("""INSERT INTO item_master (gtin, name, fmv_unit, fmv_source_url, fmv_merchant, fmv_captured_at, fmv_status, lookup_source, notes, updated_at)
                        VALUES (?,?,?,?,?,?,'unverified','ebay','eBay median; confirm at a store',?)
                        ON CONFLICT(gtin) DO UPDATE SET name=COALESCE(item_master.name, excluded.name),
                          fmv_unit=excluded.fmv_unit, fmv_source_url=excluded.fmv_source_url, fmv_merchant=excluded.fmv_merchant,
                          fmv_captured_at=excluded.fmv_captured_at, fmv_status='unverified', notes='eBay median; confirm at a store',
                          updated_at=excluded.updated_at""",
                     (gtin14, name, val["price"], val["url"], val["merchant"], now, now))
        cat = conn.execute("SELECT category FROM item_master WHERE gtin=?", (gtin14,)).fetchone()["category"]
        if not cat:
            c2 = map_category(name)
            if c2:
                conn.execute("UPDATE item_master SET category=? WHERE gtin=?", (c2, gtin14))
        ensure_links(conn, gtin14)
    conn.commit()
    return val


def upsert_book(conn, gtin14: str, b: dict, now: str, source: str):
    """Write what Google Books / Open Library know. Publisher list price, when present, is the FMV."""
    note = None if b.get("price") else "no list price yet; confirm from store links"
    conn.execute("""INSERT INTO item_master (gtin, name, brand, category, raw_category, fmv_unit, fmv_source_url, fmv_merchant,
                                             fmv_captured_at, fmv_status, lookup_source, notes, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,'unverified',?,?,?)
                    ON CONFLICT(gtin) DO UPDATE SET
                      name=excluded.name, brand=COALESCE(excluded.brand, item_master.brand),
                      category=COALESCE(item_master.category, excluded.category), raw_category=excluded.raw_category,
                      fmv_unit=COALESCE(excluded.fmv_unit, item_master.fmv_unit),
                      fmv_source_url=CASE WHEN excluded.fmv_unit IS NOT NULL THEN excluded.fmv_source_url ELSE item_master.fmv_source_url END,
                      fmv_merchant=CASE WHEN excluded.fmv_unit IS NOT NULL THEN excluded.fmv_merchant ELSE item_master.fmv_merchant END,
                      fmv_captured_at=CASE WHEN excluded.fmv_unit IS NOT NULL THEN excluded.fmv_captured_at ELSE item_master.fmv_captured_at END,
                      fmv_status=CASE WHEN item_master.fmv_status='verified' THEN 'verified' ELSE 'unverified' END,
                      lookup_source=excluded.lookup_source,
                      notes=CASE WHEN excluded.fmv_unit IS NOT NULL THEN NULL ELSE COALESCE(item_master.notes, excluded.notes) END,
                      updated_at=excluded.updated_at""",
                 (gtin14, b["title"], b.get("brand"), b.get("category", "Books & media"), b.get("raw_category"), b.get("price"),
                  b.get("url"), b.get("merchant"), now if b.get("price") else None, source, note, now))


def do_lookup(conn, gtin14: str, mock=False, api_key=None, source_label=None, thorough=False, skip_upc=False):
    """thorough=True (from `retry`) asks every Open *Facts database; intake asks one. skip_upc avoids spending a
    UPCitemdb call when that source already answered 404 for this barcode."""
    now = datetime.now().isoformat(timespec="seconds")
    isbn = isbn_from_gtin(gtin14)
    if isbn:
        # Tier 0 for books: Google Books (free, 1,000/day) gives title, publisher and often the list price.
        st, body = fetch_googlebooks(isbn, mock)
        conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES (?,?,?,?)", (gtin14, "googlebooks", st, body))
        parsed = None
        if st == 200:
            try:
                parsed = parse_googlebooks(json.loads(body))
            except json.JSONDecodeError:
                parsed = None
        if parsed:
            upsert_book(conn, gtin14, parsed, now, "googlebooks")
            if parsed["price"]:
                record_offers(conn, gtin14, [{"merchant": parsed["merchant"], "price": parsed["price"], "link": parsed["url"],
                                              "condition": "New", "updated_t": int(time.time())}], "googlebooks")
            ensure_links(conn, gtin14)
            conn.commit()
            if parsed["price"]:
                return {"gtin": gtin14, "status": "unverified", "name": parsed["title"], "brand": parsed.get("brand"),
                        "category": "Books & media", "raw_category": parsed["raw_category"], "fmv": parsed["price"],
                        "url": parsed["url"], "offers": 1, "note": ""}
            # title known, no list price: fall through to UPCitemdb for a retail offer
        elif st in (429, 403):
            # Google quota (keyless requests share a public pool that is usually empty). Get a title from
            # Open Library if we can, hold the book for `retry`, and do NOT spend a UPC lookup on it.
            st2, body2 = fetch_openlibrary(isbn, mock)
            conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES (?,?,?,?)", (gtin14, "openlibrary", st2, body2))
            title = None
            if st2 == 200:
                try:
                    ol = json.loads(body2)
                    title = ol.get("title")
                    if title:
                        upsert_book(conn, gtin14, {"title": title, "brand": ", ".join(ol.get("publishers") or []) or None,
                                                   "raw_category": "Books"}, now, "openlibrary")
                except json.JSONDecodeError:
                    pass
            if not title:
                conn.execute("""INSERT INTO item_master (gtin, category, fmv_status, lookup_source, notes, updated_at)
                                VALUES (?, 'Books & media', 'not_found', 'googlebooks', 'Google Books quota; retry with an API key', ?)
                                ON CONFLICT(gtin) DO UPDATE SET updated_at=excluded.updated_at""", (gtin14, now))
            conn.execute("UPDATE item_master SET notes='Google Books quota; retry with an API key' WHERE gtin=? AND fmv_unit IS NULL", (gtin14,))
            ensure_links(conn, gtin14)
            conn.commit()
            return {"gtin": gtin14, "status": "pending", "http": st, "name": title}
        else:
            st2, body2 = fetch_openlibrary(isbn, mock)
            conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES (?,?,?,?)", (gtin14, "openlibrary", st2, body2))
            if st2 == 200:
                try:
                    ol = json.loads(body2)
                    if ol.get("title"):
                        upsert_book(conn, gtin14, {"title": ol["title"], "brand": ", ".join(ol.get("publishers") or []) or None,
                                                   "raw_category": "Books"}, now, "openlibrary")
                        ensure_links(conn, gtin14)
                        conn.commit()
                except json.JSONDecodeError:
                    pass
    else:
        # Non-book: the GS1 prefix names the maker instantly; Open *Facts (free, open data) usually names the
        # product for food, drink and toiletries. Neither has prices, so UPCitemdb still runs for the offer.
        company, hint_cat = prefix_hint(conn, gtin14)
        src, st, body, parsed = fetch_open_facts(gtin14, mock, sources=open_facts_order(hint_cat), max_calls=thorough and 3 or 1)
        if src:
            conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES (?,?,?,?)", (gtin14, src, st, body))
        if parsed:
            upsert_book(conn, gtin14, {"title": parsed["title"], "brand": parsed["brand"] or company,
                                       "category": parsed["category"] or hint_cat, "raw_category": parsed["raw_category"]}, now, src)
        elif company:
            conn.execute("""INSERT INTO item_master (gtin, brand, category, fmv_status, lookup_source, updated_at)
                            VALUES (?, ?, ?, 'not_found', 'gs1-prefix', ?)
                            ON CONFLICT(gtin) DO UPDATE SET brand=COALESCE(item_master.brand, excluded.brand),
                                                            category=COALESCE(item_master.category, excluded.category)""",
                         (gtin14, company, hint_cat, now))
        if parsed or company:
            ensure_links(conn, gtin14)
            conn.commit()
        if skip_upc:
            val = maybe_ebay(conn, gtin14, now)
            r = conn.execute("SELECT name, category FROM item_master WHERE gtin=?", (gtin14,)).fetchone()
            if val:
                return {"gtin": gtin14, "status": "unverified", "name": r["name"], "category": r["category"], "fmv": val["price"],
                        "url": val["url"], "offers": val.get("n", 1), "note": val.get("note") or "eBay median; confirm at a store"}
            return {"gtin": gtin14, "status": "named" if (r and r["name"]) else "not_found", "http": st, "name": r["name"] if r else None,
                    "category": (r["category"] if r else None) or hint_cat, "fmv": None}
    status, body = fetch_mock(gtin14) if mock else fetch_upcitemdb(gtin14, api_key)
    source = source_label or ("mock" if mock else "upcitemdb")
    conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES (?,?,?,?)",
                 (gtin14, source, status, body))
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = {}
    items = data.get("items") or []
    if (status != 200 or not items) and not mock and upcdatabase_key() and \
            not conn.execute("SELECT 1 FROM lookup_log WHERE gtin=? AND source='upcdatabase' AND http_status IN (200,404) LIMIT 1", (gtin14,)).fetchone():
        # UPCitemdb throttled or empty: try UPC Database (second free general source) before giving up
        st2, body2, item2 = fetch_upcdatabase(gtin14)
        conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES (?,?,?,?)", (gtin14, "upcdatabase", st2, body2))
        if item2:
            status, items, source = 200, [item2], "upcdatabase"
    if status == 429 or status == 0:
        # throttled (free tier: "TOO_FAST") or no network — not a miss. Leave a placeholder row;
        # `gik.py retry` picks these up later.
        conn.execute("""INSERT INTO item_master (gtin, fmv_status, lookup_source, notes, updated_at)
                        VALUES (?, 'not_found', ?, 'lookup pending (rate limited)', ?)
                        ON CONFLICT(gtin) DO UPDATE SET notes=CASE WHEN item_master.name IS NULL THEN 'lookup pending (rate limited)' ELSE item_master.notes END,
                                                        updated_at=excluded.updated_at""",
                     (gtin14, source, now))
        ensure_links(conn, gtin14)
        conn.commit()
        val = maybe_ebay(conn, gtin14, now)
        if val:
            return {"gtin": gtin14, "status": "unverified", "name": conn.execute("SELECT name FROM item_master WHERE gtin=?", (gtin14,)).fetchone()["name"],
                    "category": None, "fmv": val["price"], "url": val["url"], "offers": val.get("n", 1), "note": val.get("note") or "eBay median; confirm at a store"}
        return {"gtin": gtin14, "status": "pending", "http": status}
    if status != 200 or not items:
        conn.execute("""INSERT INTO item_master (gtin, fmv_status, lookup_source, notes, updated_at)
                        VALUES (?, 'not_found', ?, 'not in UPC database; research by hand', ?)
                        ON CONFLICT(gtin) DO UPDATE SET fmv_status=CASE WHEN item_master.name IS NULL THEN 'not_found' ELSE item_master.fmv_status END,
                                                        notes=CASE WHEN item_master.name IS NULL THEN 'not in UPC database; research by hand' ELSE item_master.notes END,
                                                        lookup_source=excluded.lookup_source, updated_at=excluded.updated_at""",
                     (gtin14, source, now))
        ensure_links(conn, gtin14)
        conn.commit()
        val = maybe_ebay(conn, gtin14, now)
        if val:
            r = conn.execute("SELECT name, category FROM item_master WHERE gtin=?", (gtin14,)).fetchone()
            return {"gtin": gtin14, "status": "unverified", "name": r["name"], "category": r["category"], "fmv": val["price"],
                    "url": val["url"], "offers": val.get("n", 1), "note": val.get("note") or "eBay median; confirm at a store"}
        return {"gtin": gtin14, "status": "not_found", "http": status}
    it = items[0]
    record_offers(conn, gtin14, it.get("offers") or [], source)
    pick = pick_fmv(it.get("offers") or [])
    raw_cat = it.get("category") or ""
    title = it.get("title") or ""
    cat = map_category(raw_cat + " " + title)
    note = pick["note"]
    if USED_WORDS.search(title):
        note = (note + "; " if note else "") + "listing title says used; price a NEW copy"
    fmv_status = "unverified"  # a human verifies before anything is pushed
    offer_updated = datetime.fromtimestamp(pick["newest_ts"]).date().isoformat() if pick["newest_ts"] else None
    conn.execute("""INSERT INTO item_master (gtin, name, brand, category, raw_category, fmv_unit, fmv_source_url, fmv_merchant,
                                             offer_updated, fmv_captured_at, fmv_status, lookup_source, notes, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(gtin) DO UPDATE SET
                      name=COALESCE(item_master.name, excluded.name), brand=COALESCE(item_master.brand, excluded.brand),
                      category=COALESCE(item_master.category, excluded.category), raw_category=excluded.raw_category,
                      fmv_unit=COALESCE(item_master.fmv_unit, excluded.fmv_unit),
                      fmv_source_url=COALESCE(item_master.fmv_source_url, excluded.fmv_source_url),
                      fmv_merchant=COALESCE(item_master.fmv_merchant, excluded.fmv_merchant),
                      offer_updated=COALESCE(item_master.offer_updated, excluded.offer_updated),
                      fmv_captured_at=COALESCE(item_master.fmv_captured_at, excluded.fmv_captured_at),
                      fmv_status=CASE WHEN item_master.fmv_status='verified' THEN 'verified' ELSE excluded.fmv_status END,
                      lookup_source=excluded.lookup_source,
                      notes=CASE WHEN item_master.name IS NULL THEN excluded.notes ELSE item_master.notes END,
                      updated_at=excluded.updated_at""",
                 (gtin14, title, it.get("brand"), cat, raw_cat, pick["price"], pick["url"], pick["merchant"], offer_updated,
                  now if pick["price"] else None, fmv_status, source, note or None, now))
    ensure_links(conn, gtin14)
    # if the lookup already returned one of the three retailers (new condition), keep that offer as a starting link
    for o in it.get("offers") or []:
        dom = (o.get("domain") or o.get("merchant") or "").lower().replace("wal-mart", "walmart")
        if o.get("condition") and o["condition"].lower() != "new":
            continue
        for r in RETAILERS:
            if r in dom and "marketplace" not in dom and o.get("link") and o.get("price"):
                conn.execute("""UPDATE item_links SET product_url=COALESCE(product_url, ?), price=COALESCE(price, ?),
                                checked_at=COALESCE(checked_at, ?), checked_by=COALESCE(checked_by, ?)
                                WHERE gtin=? AND retailer=?""", (o["link"], float(o["price"]), now, source, gtin14, r))
    conn.commit()
    if pick["price"] is None:
        val = maybe_ebay(conn, gtin14, now)
        if val:
            return {"gtin": gtin14, "status": fmv_status, "name": title, "brand": it.get("brand"), "category": cat,
                    "raw_category": raw_cat, "fmv": val["price"], "url": val["url"], "offers": val.get("n", 1), "note": val.get("note") or "eBay median; confirm at a store"}
    return {"gtin": gtin14, "status": fmv_status, "name": title, "brand": it.get("brand"),
            "category": cat, "raw_category": raw_cat, "fmv": pick["price"], "url": pick["url"], "offers": pick["n"], "note": note}


def cmd_test_ebay(args):
    cid, _ = ebay_keys()
    if not cid:
        sys.exit("No eBay keys. Put the Client ID on line 1 and Client Secret on line 2 of ebay_keys.txt (see README).")
    tok = ebay_token()
    if not tok:
        sys.exit("Could not get an eBay token; check the two lines in ebay_keys.txt (production keys, not sandbox).")
    gtin = normalize_gtin(args.gtin or "071662000240")
    st, body, offers = fetch_ebay(gtin)
    print(f"HTTP {st}; {len(offers)} new fixed-price listings")
    for o in offers[:8]:
        print(f"  ${o['price']:>8.2f}  {o['merchant']:<28} {o['title'][:60]}")
    val = ebay_value(offers)
    print("median value:", val["price"] if val else "n/a (need 2+ listings)")
    if st != 200:
        print(body[:400])


def cmd_lookup(args):
    gtin = normalize_gtin(args.gtin)
    if not gtin:
        sys.exit(f"'{args.gtin}' is not a valid UPC/EAN/GTIN (bad length or check digit)")
    conn = connect()
    res = do_lookup(conn, gtin, mock=args.mock, api_key=os.environ.get("UPCITEMDB_KEY"))
    print(json.dumps(res, indent=2))

def pending_lookups(conn, include_misses=False):
    """GTINs still needing a lookup: no name yet and (last attempt was throttled/no-network, or never attempted).
    Generic (G-) items are never looked up; staff value them."""
    rows = conn.execute("""SELECT im.gtin,
                                  (SELECT http_status FROM lookup_log l WHERE l.gtin=im.gtin
                                   AND l.source NOT IN ('googlebooks','openlibrary','openfoodfacts','openproductsfacts','openbeautyfacts')
                                   ORDER BY log_id DESC LIMIT 1) AS last_http
                           FROM item_master im WHERE im.fmv_unit IS NULL AND im.fmv_status <> 'verified' AND im.gtin NOT LIKE 'G-%'""").fetchall()
    out = []
    for r in rows:
        if r["last_http"] in (None, 0, 429) or (include_misses and r["last_http"] == 404):
            out.append(r["gtin"])
    # books with no successful Google Books answer yet (never asked, or quota-refused): give them a list-price pass
    for r in conn.execute("""SELECT gtin FROM item_master im WHERE fmv_status <> 'verified' AND fmv_unit IS NULL
                             AND (substr(gtin, 2, 3) IN ('978','979'))
                             AND NOT EXISTS (SELECT 1 FROM lookup_log l WHERE l.gtin = im.gtin AND l.source = 'googlebooks' AND l.http_status = 200)"""):
        if r["gtin"] not in out:
            out.append(r["gtin"])
    # non-books still nameless that the Open *Facts databases have not all been asked about (intake asks only one)
    for r in conn.execute("""SELECT gtin FROM item_master im WHERE fmv_status <> 'verified' AND name IS NULL
                             AND substr(gtin, 2, 3) NOT IN ('978','979') AND gtin NOT LIKE 'G-%'
                             AND (SELECT COUNT(DISTINCT source) FROM lookup_log l WHERE l.gtin = im.gtin
                                  AND l.source IN ('openfoodfacts','openproductsfacts','openbeautyfacts')) < 3"""):
        if r["gtin"] not in out:
            out.append(r["gtin"])
    # UPCitemdb said no, but UPC Database has not been asked yet (only when its key is configured)
    if upcdatabase_key():
        for r in conn.execute("""SELECT gtin FROM item_master im WHERE fmv_status <> 'verified' AND fmv_unit IS NULL AND gtin NOT LIKE 'G-%'
                                 AND NOT EXISTS (SELECT 1 FROM lookup_log l WHERE l.gtin = im.gtin AND l.source = 'upcdatabase')"""):
            if r["gtin"] not in out:
                out.append(r["gtin"])
    # anything still unpriced that eBay has not been asked about (only when eBay keys are configured)
    if ebay_keys()[0]:
        for r in conn.execute("""SELECT gtin FROM item_master im WHERE fmv_status <> 'verified' AND fmv_unit IS NULL AND gtin NOT LIKE 'G-%'
                                 AND NOT EXISTS (SELECT 1 FROM lookup_log l WHERE l.gtin = im.gtin AND l.source = 'ebay')"""):
            if r["gtin"] not in out:
                out.append(r["gtin"])
    # Spend the scarce daily lookups where the dollars are: scanned items first, most units first. Pre-loaded catalog
    # items that nobody has scanned are left to the catalog price fill (free, unlimited sources), never queued here.
    units = {r[0]: r[1] for r in conn.execute("SELECT gtin, SUM(qty) FROM receipts GROUP BY gtin")}
    seeded = {r[0] for r in conn.execute("SELECT gtin FROM item_master WHERE lookup_source LIKE '%-seed'")}
    out = [g for g in out if g in units or g not in seeded]
    out.sort(key=lambda g: -(units.get(g) or 0))
    return out


def last_upc_status(conn, gtin14):
    """HTTP status of the last UPCitemdb answer, with a 200 that carried no items reported as 404."""
    r = conn.execute("""SELECT http_status, raw_response, source FROM lookup_log WHERE gtin=? AND source IN ('upcitemdb','mock','upcdatabase')
                        ORDER BY log_id DESC LIMIT 1""", (gtin14,)).fetchone()
    if not r:
        return None
    if r["http_status"] == 200:
        try:
            d = json.loads(r["raw_response"])
            if r["source"] == "upcdatabase":
                return 200 if str(d.get("success")).lower() == "true" else 404
            if not (d.get("items") or []):
                return 404
        except json.JSONDecodeError:
            return 404
    return r["http_status"]


def cmd_retry(args):
    """Re-run lookups for throttled/deferred barcodes, slowly enough for the free tier, and re-apply category rules."""
    conn = connect()
    api_key = os.environ.get("UPCITEMDB_KEY")
    todo = pending_lookups(conn, include_misses=args.all)
    if getattr(args, "max", 0):
        todo = todo[:args.max]
    gap = 1.0 if api_key else args.gap
    print(f"{len(todo)} barcodes to look up, {gap:.0f}s apart" + (" (including earlier misses)" if args.all else ""))
    found = misses = 0
    if not google_books_key() and any(isbn_from_gtin(g) for g in todo):
        print("  note: no Google Books API key found (google_books_key.txt). Keyless requests share a public quota that is\n"
              "  usually exhausted, so books may stay pending. See README for the 10-minute free key setup.")
    books_blocked = False
    for i, gtin in enumerate(todo):
        if books_blocked and isbn_from_gtin(gtin):
            continue
        # don't spend a UPCitemdb call on a barcode it already said 404 to, unless --all
        # UPCitemdb already answered: skip it, unless UPC Database is configured and has not been asked yet
        asked_upcdb = conn.execute("SELECT 1 FROM lookup_log WHERE gtin=? AND source='upcdatabase' LIMIT 1", (gtin,)).fetchone() is not None
        skip_upc = (not args.all) and last_upc_status(conn, gtin) in (200, 404) and (asked_upcdb or not upcdatabase_key())
        res = do_lookup(conn, gtin, mock=args.mock, api_key=api_key, thorough=True, skip_upc=skip_upc)
        if res["status"] == "named":
            found += 1; print(f"  {gtin}  {(res['name'] or '')[:50]:<50} {res['category'] or 'Uncategorized?':<24} FMV? (named via open data; price from store links)")
            time.sleep(1.0); continue
        if res["status"] == "pending" and isbn_from_gtin(gtin) and res.get("http") in (429, 403):
            books_blocked = True
            print(f"  {gtin}  Google Books quota exceeded — skipping remaining books until a key is set")
            continue
        if res["status"] == "pending":
            print(f"  {gtin}  still throttled — waiting 60s"); time.sleep(60)
            res = do_lookup(conn, gtin, mock=args.mock, api_key=api_key)
        if res["status"] == "pending":
            print("  giving up for now; run retry again later (free tier allows 100 lookups/day)"); break
        if res["status"] == "not_found":
            misses += 1; print(f"  {gtin}  not in UPC database")
        else:
            found += 1; print(f"  {gtin}  {(res['name'] or '')[:50]:<50} {res['category'] or 'Uncategorized?':<24} {('$%.2f' % res['fmv']) if res['fmv'] else 'FMV?'}")
        if i < len(todo) - 1:
            # Google Books handles ISBNs and allows ~1,000/day, so only lookups that hit UPCitemdb need the long gap
            time.sleep(1.0 if (isbn_from_gtin(gtin) and res.get("fmv")) or skip_upc else gap)
    # re-apply category rules to anything still uncategorized (rules improve over time)
    n = 0
    for r in conn.execute("SELECT gtin, raw_category, name FROM item_master WHERE category IS NULL AND name IS NOT NULL").fetchall():
        cat = map_category((r["raw_category"] or "") + " " + (r["name"] or ""))
        if cat:
            conn.execute("UPDATE item_master SET category=? WHERE gtin=?", (cat, r["gtin"])); n += 1
    conn.commit()
    print(f"\nDone: {found} found, {misses} not in database, {n} items newly categorized. Still pending: {len(pending_lookups(conn))}")

def cmd_refresh(args):
    """Re-apply the current offer and category rules to every unverified item using the responses
    already saved in lookup_log. No web calls. Run after rules change, or after a code update."""
    conn = connect()
    now = datetime.now().isoformat(timespec="seconds")
    n = 0
    for it in conn.execute("SELECT gtin FROM item_master WHERE fmv_status <> 'verified'").fetchall():
        log = conn.execute("""SELECT raw_response, source FROM lookup_log WHERE gtin=? AND http_status=200
                              AND source IN ('upcitemdb','upcdatabase','mock')
                              ORDER BY log_id DESC LIMIT 1""",
                           (it["gtin"],)).fetchone()
        if not log:
            continue
        if log["source"] == "upcdatabase":
            item = parse_upcdatabase(log["raw_response"], it["gtin"])
            if not item:
                continue
        else:
            try:
                items = json.loads(log["raw_response"]).get("items") or []
            except (json.JSONDecodeError, TypeError):
                continue
            if not items:
                continue
            item = items[0]
        record_offers(conn, it["gtin"], item.get("offers") or [], log["source"])
        pick = pick_fmv(item.get("offers") or [])
        title = item.get("title") or ""
        raw_cat = item.get("category") or ""
        cat = map_category(raw_cat + " " + title)
        note = pick["note"]
        if USED_WORDS.search(title):
            note = (note + "; " if note else "") + "listing title says used; price a NEW copy"
        offer_updated = datetime.fromtimestamp(pick["newest_ts"]).date().isoformat() if pick["newest_ts"] else None
        conn.execute("""UPDATE item_master SET name=COALESCE(name, ?), brand=COALESCE(brand, ?), category=?, raw_category=?,
                        fmv_unit=?, fmv_source_url=?, fmv_merchant=?, offer_updated=?, fmv_captured_at=?,
                        fmv_status='unverified', notes=?, updated_at=? WHERE gtin=?""",
                     (title, item.get("brand"), cat, raw_cat, pick["price"], pick["url"], pick["merchant"], offer_updated,
                      now if pick["price"] else None, note or None, now, it["gtin"]))
        conn.execute("UPDATE item_links SET product_url=NULL, price=NULL, checked_at=NULL, checked_by=NULL WHERE gtin=? AND checked_by IN ('upcitemdb','mock')", (it["gtin"],))
        for o in item.get("offers") or []:
            dom = (o.get("domain") or o.get("merchant") or "").lower().replace("wal-mart", "walmart")
            if o.get("condition") and o["condition"].lower() != "new":
                continue
            for r in RETAILERS:
                if r in dom and "marketplace" not in dom and o.get("link") and o.get("price"):
                    conn.execute("""UPDATE item_links SET product_url=COALESCE(product_url, ?), price=COALESCE(price, ?),
                                    checked_at=COALESCE(checked_at, ?), checked_by=COALESCE(checked_by, 'upcitemdb')
                                    WHERE gtin=? AND retailer=?""", (o["link"], float(o["price"]), now, it["gtin"], r))
        n += 1
    # Google Books wins for ISBNs: publisher list price is the cleanest FMV evidence for a new book.
    nb = 0
    for it in conn.execute("SELECT gtin FROM item_master WHERE fmv_status <> 'verified'").fetchall():
        log = conn.execute("SELECT raw_response FROM lookup_log WHERE gtin=? AND source='googlebooks' AND http_status=200 ORDER BY log_id DESC LIMIT 1",
                           (it["gtin"],)).fetchone()
        if not log:
            continue
        try:
            parsed = parse_googlebooks(json.loads(log["raw_response"]))
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if parsed:
            upsert_book(conn, it["gtin"], parsed, now, "googlebooks")
            if parsed["price"]:
                record_offers(conn, it["gtin"], [{"merchant": parsed["merchant"], "price": parsed["price"], "link": parsed["url"],
                                                  "condition": "New", "updated_t": int(time.time())}], "googlebooks")
            nb += 1
    # GS1 prefix hints for anything still missing a brand or category
    nh = 0
    for it in conn.execute("SELECT gtin FROM item_master WHERE (brand IS NULL OR category IS NULL) AND fmv_status <> 'verified'").fetchall():
        company, hint_cat = prefix_hint(conn, it["gtin"])
        if company:
            conn.execute("UPDATE item_master SET brand=COALESCE(brand, ?), category=COALESCE(category, ?) WHERE gtin=?",
                         (company, hint_cat, it["gtin"])); nh += 1
    conn.commit()
    print(f"Refreshed {n} items from saved UPC lookups, {nb} books from saved Google Books lookups, {nh} brand/category hints from GS1 prefixes.")

# ---------------------------------------------------------------- donors / donations

def cmd_donor(args):
    conn = connect()
    if args.action == "add":
        conn.execute("INSERT INTO donors (name, type, ns_customer_id, contact) VALUES (?,?,?,?)",
                     (args.name, args.type, args.ns_id, args.contact))
        conn.commit()
        print(f"Added donor '{args.name}'")
    else:
        for r in conn.execute("SELECT * FROM donors ORDER BY name"):
            print(f"{r['donor_id']:>3}  {r['name']:<35} {r['type']:<16} NS:{r['ns_customer_id'] or '-'}")


def cmd_donation(args):
    conn = connect()
    if args.action == "new":
        donor = conn.execute("SELECT donor_id FROM donors WHERE name = ?", (args.donor,)).fetchone()
        if not donor:
            sys.exit(f"Donor '{args.donor}' not found. Add it with: gik.py donor add \"{args.donor}\" --type corporate")
        conn.execute("""INSERT INTO donations (donation_id, donor_id, received_date, bol_number, pallets, restriction, notes)
                        VALUES (?,?,?,?,?,?,?)""",
                     (args.donation_id, donor["donor_id"], args.date or date.today().isoformat(),
                      args.bol, args.pallets, args.restriction, args.notes))
        conn.commit()
        print(f"Created donation {args.donation_id}")
    else:
        q = """SELECT d.donation_id, d.received_date, dn.name, d.pallets,
                      COALESCE(SUM(r.qty),0) AS units, COUNT(DISTINCT r.gtin) AS skus
               FROM donations d JOIN donors dn ON dn.donor_id = d.donor_id
               LEFT JOIN receipts r ON r.donation_id = d.donation_id
               GROUP BY d.donation_id ORDER BY d.received_date DESC"""
        for r in conn.execute(q):
            print(f"{r['donation_id']:<16} {r['received_date']}  {r['name']:<30} units={r['units']:<6} skus={r['skus']}")

# ---------------------------------------------------------------- intake loop

HELP = """
Scan a barcode (the gun types it and presses Enter).
No barcode? Type what it is (e.g. "table lamp", "mason jar") and pick from the list.
Commands:
  @C03Z   set the rack/bin for everything scanned next (scan a location label, or type it); @DOCK = not put away yet
  q<N>    set quantity for the NEXT scan, e.g. q24 for a case of 24
  -       undo the last scan
  ?       show the running count for this donation
  x       finish
"""


def cmd_intake(args):
    conn = connect()
    don = conn.execute("""SELECT d.*, dn.name AS donor_name FROM donations d
                          JOIN donors dn ON dn.donor_id = d.donor_id WHERE donation_id = ?""",
                       (args.donation_id,)).fetchone()
    if not don:
        sys.exit(f"Donation {args.donation_id} not found. Create it with: gik.py donation new ...")
    api_key = os.environ.get("UPCITEMDB_KEY")
    print(f"\nIntake for {don['donation_id']} — {don['donor_name']} ({don['received_date']})")
    print(HELP)
    pending_qty = 1
    last_receipt = None
    cooldown_until = 0.0  # after a 429, stop live lookups for a minute; `retry` catches up later
    location = args.location.upper() if getattr(args, "location", None) else None
    if location:
        print(f"  location: {location}")
    while True:
        try:
            line = input("scan> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        low = line.lower()
        if low == "x":
            break
        if low == "?":
            show_running(conn, args.donation_id)
            continue
        if low == "-":
            if last_receipt:
                conn.execute("DELETE FROM receipts WHERE receipt_id = ?", (last_receipt,))
                conn.commit()
                print("  undone")
                last_receipt = None
            else:
                print("  nothing to undo")
            continue
        m = re.fullmatch(r"q(\d+)", low)
        if m:
            pending_qty = max(1, int(m.group(1)))
            print(f"  next scan counts as {pending_qty}")
            continue
        m = re.fullmatch(r"(?:@|loc\s+)([a-z0-9\-]{2,12})", low)
        if m:
            code = m.group(1).upper()
            row = conn.execute("SELECT description FROM locations WHERE code=? AND active=1", (code,)).fetchone()
            if row:
                location = code
                print(f"  location set to {code} ({row['description']}) for the scans that follow")
            else:
                print(f"  ! {code} is not a known location. Known: " + ", ".join(r[0] for r in conn.execute("SELECT code FROM locations WHERE active=1 ORDER BY code")))
            continue
        gtin = normalize_gtin(line)
        if not gtin and re.search(r"[a-zA-Z]{2,}", line):
            # Typed words: a non-barcoded item. Search the generic catalog, let the volunteer pick or add.
            gtin = pick_generic(conn, line, args.by)
            if not gtin:
                continue
        elif not gtin:
            print(f"  ! '{line}' is not a valid barcode (length/check digit). Rescan, or type a description for a non-barcoded item.")
            continue
        item = conn.execute("SELECT * FROM item_master WHERE gtin = ?", (gtin,)).fetchone()
        if item is None and is_generic(gtin):
            item = conn.execute("SELECT * FROM item_master WHERE gtin = ?", (gtin,)).fetchone()
        if item is None:
            if time.time() < cooldown_until:
                conn.execute("INSERT INTO item_master (gtin, fmv_status, lookup_source, notes) VALUES (?, 'not_found', 'deferred', 'lookup pending (rate limited)')", (gtin,))
                ensure_links(conn, gtin)
                print("  new barcode — lookup deferred (rate limit cooldown); `gik.py retry` will fetch it")
            else:
                print("  new barcode — looking it up…")
                res = do_lookup(conn, gtin, mock=args.mock, api_key=api_key)
                if res["status"] == "pending":
                    cooldown_until = time.time() + 60
                    print("  ! lookup service says TOO FAST — pausing lookups for 60s; keep scanning, run `gik.py retry` afterwards")
                elif res["status"] == "not_found":
                    print("  ! not in UPC database; added to review queue")
                elif res.get("note"):
                    print(f"  note: {res['note']}")
            item = conn.execute("SELECT * FROM item_master WHERE gtin = ?", (gtin,)).fetchone()
        cur = conn.execute("INSERT INTO receipts (donation_id, gtin, qty, scanned_by, location) VALUES (?,?,?,?,?)",
                           (args.donation_id, gtin, pending_qty, args.by, location))
        conn.commit()
        last_receipt = cur.lastrowid
        name = item["name"] or "(unknown item)"
        fmv = f"${item['fmv_unit']:.2f}" if item["fmv_unit"] else "FMV?"
        flag = "" if item["fmv_status"] == "verified" else f"  [{item['fmv_status']}]"
        where = f"  @{location}" if location else ""
        print(f"  +{pending_qty}  {name[:48]:<48} {item['category'] or 'Uncategorized?':<22} {fmv}{flag}{where}")
        pending_qty = 1
    show_running(conn, args.donation_id)


def pick_generic(conn, text: str, who: str):
    """Interactive: show matches for typed words; return the chosen G- code, a newly added one, or None."""
    hits = search_generic(conn, text)
    if hits:
        print(f"  Matches for \"{text}\":")
        for i, r in enumerate(hits, 1):
            print(f"   {i}. {fmt_generic(r)}")
        prompt = f"  pick 1-{len(hits)}, n = not listed (add it), Enter = cancel > "
    else:
        print(f"  No match for \"{text}\".")
        prompt = "  n = add it as a new item, Enter = cancel > "
    try:
        ans = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if ans.isdigit() and hits and 1 <= int(ans) <= len(hits):
        return hits[int(ans) - 1]["code"]
    if ans == "n":
        name = text.strip().capitalize()
        code = add_generic(conn, name, "Household goods", "", None, None, "each", "", who or "volunteer")
        print(f"  Added {code} \"{name}\" with no value yet.")
        print(f"  >>> Take a photo of it. Name the photo {code}.jpg and put it in the photos folder. Staff will price it.")
        return code
    return None


def show_running(conn, donation_id):
    rows = conn.execute("""SELECT im.category, COUNT(DISTINCT r.gtin) skus, SUM(r.qty) units,
                                  ROUND(SUM(r.qty * COALESCE(im.fmv_unit,0)),2) est_fmv,
                                  SUM(CASE WHEN im.fmv_status<>'verified' THEN r.qty ELSE 0 END) unverified_units
                           FROM receipts r JOIN item_master im ON im.gtin = r.gtin
                           WHERE r.donation_id = ? GROUP BY im.category ORDER BY est_fmv DESC""", (donation_id,)).fetchall()
    print(f"\n  {'Category':<24}{'SKUs':>6}{'Units':>8}{'Est. FMV':>12}{'Unverified':>12}")
    tu = tf = 0
    for r in rows:
        print(f"  {(r['category'] or 'Uncategorized'):<24}{r['skus']:>6}{r['units']:>8}{r['est_fmv']:>12.2f}{r['unverified_units']:>12}")
        tu += r["units"]; tf += r["est_fmv"]
    print(f"  {'TOTAL':<24}{'':>6}{tu:>8}{tf:>12.2f}\n")

# ---------------------------------------------------------------- review / verify

def cmd_review(args):
    conn = connect()
    rows = conn.execute("SELECT * FROM v_review_queue ORDER BY units_waiting DESC").fetchall()
    if not rows:
        print("Review queue is empty.")
        return
    pend = set(pending_lookups(conn))
    print(f"{'GTIN':<15}{'Status':<12}{'Units':>6}  {'Category':<22} {'FMV':>8}  Name / note")
    for r in rows:
        status = "pending" if r["gtin"] in pend else r["fmv_status"]
        print(f"{r['gtin']:<15}{status:<12}{r['units_waiting']:>6}  {(r['category'] or '—'):<22} "
              f"{(r['fmv_unit'] or 0):>8.2f}  {(r['name'] or '(unknown)')[:40]}")
        note = conn.execute("SELECT notes FROM item_master WHERE gtin=?", (r["gtin"],)).fetchone()["notes"]
        if note:
            print(f"{'':<15}{'':<12}{'':>6}  {'':<22} {'':>8}  ↳ {note[:80]}")
    if pend:
        print(f"\n{len(pend)} barcodes are waiting on the lookup service: run  gik.py retry")
    print("\nVerify with: gik.py verify <GTIN> [--fmv 12.99] [--url https://...] [--category \"School supplies\"] [--name ...]")


def cmd_verify(args):
    conn = connect()
    gtin = normalize_gtin(args.gtin)
    if not gtin:
        sys.exit("invalid GTIN")
    item = conn.execute("SELECT * FROM item_master WHERE gtin = ?", (gtin,)).fetchone()
    if not item:
        conn.execute("INSERT INTO item_master (gtin, lookup_source) VALUES (?, 'manual')", (gtin,))
    ensure_links(conn, gtin)
    fields, vals = [], []
    for col, val in (("fmv_unit", args.fmv), ("fmv_source_url", args.url), ("category", args.category),
                     ("name", args.name), ("brand", args.brand), ("fmv_basis", args.basis)):
        if val is not None:
            fields.append(f"{col} = ?"); vals.append(val)
    if args.fmv is not None or args.url is not None:
        fields.append("fmv_captured_at = ?"); vals.append(datetime.now().isoformat(timespec="seconds"))
        fields.append("lookup_source = ?"); vals.append("manual")
    fields.append("fmv_status = ?"); vals.append("disputed" if args.dispute else "verified")
    fields.append("updated_at = ?"); vals.append(datetime.now().isoformat(timespec="seconds"))
    vals.append(gtin)
    conn.execute(f"UPDATE item_master SET {', '.join(fields)} WHERE gtin = ?", vals)
    conn.commit()
    row = conn.execute("SELECT * FROM item_master WHERE gtin = ?", (gtin,)).fetchone()
    missing = [c for c in ("fmv_unit", "fmv_source_url", "category") if not row[c]]
    if missing and not args.dispute:
        print(f"Marked verified, but still missing: {', '.join(missing)} — export will skip it until filled.")
    else:
        print(f"{gtin} → {row['fmv_status']}: {row['name']} | {row['category']} | ${row['fmv_unit'] or 0:.2f} | {row['fmv_source_url']}")

# ---------------------------------------------------------------- exports

def cmd_export(args):
    conn = connect()
    os.makedirs(args.out, exist_ok=True)
    batch = datetime.now().strftime("GIK%Y%m%d-%H%M%S")
    if args.what == "master":
        return cmd_export_master(conn, args.out)
    if args.what == "values":
        return cmd_export_values(conn, args.out)
    if args.what == "receipts":
        path = os.path.join(args.out, "receipts.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["receipt_id", "donation_id", "donor", "received_date", "scanned_at", "scanned_by", "location", "upc_or_code",
                        "item", "category", "qty", "unit_value", "line_value", "fmv_status", "push_status"])
            for r in conn.execute("""SELECT r.*, d.received_date, dn.name donor, im.name item, im.category, im.fmv_unit, im.fmv_status
                                     FROM receipts r JOIN donations d ON d.donation_id=r.donation_id JOIN donors dn ON dn.donor_id=d.donor_id
                                     JOIN item_master im ON im.gtin=r.gtin ORDER BY r.receipt_id"""):
                unit = r["fmv_unit"] if r["fmv_unit"] is not None else ""
                w.writerow([r["receipt_id"], r["donation_id"], r["donor"], r["received_date"], r["scanned_at"], r["scanned_by"] or "",
                            r["location"] or "", r["gtin"][-12:] if r["gtin"].startswith("00") else r["gtin"], r["item"] or "", r["category"] or "",
                            r["qty"], unit, round(r["qty"] * r["fmv_unit"], 2) if r["fmv_unit"] is not None else "", r["fmv_status"], r["push_status"]])
        print(f"Wrote {path}")
        return
    if args.what == "je":
        rows = conn.execute("SELECT * FROM v_journal_lines ORDER BY donation_id, category").fetchall()
        if not rows:
            print("Nothing verified to export. Check `gik.py review`.")
            return
        path = os.path.join(args.out, f"netsuite_journal_{batch}.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["External ID", "Date", "Posting Period", "Account", "Debit", "Credit", "Name",
                        "Class", "Memo", "Line Memo"])
            for r in rows:
                d = datetime.strptime(r["received_date"], "%Y-%m-%d")
                period = d.strftime("%b %Y")
                memo = f"GIK {r['donation_id']} {r['donor_name']} — batch {batch}"
                line = f"{r['category']}: {r['units']} units"
                w.writerow([r["donation_id"], d.strftime("%m/%d/%Y"), period, r["debit_account_id"],
                            f"{r['amount']:.2f}", "", r["ns_customer_id"] or "", r["class_id"] or "", memo, line])
                w.writerow([r["donation_id"], d.strftime("%m/%d/%Y"), period, r["credit_account_id"],
                            "", f"{r['amount']:.2f}", r["ns_customer_id"] or "", r["class_id"] or "", memo, line])
        # mark the receipts behind these lines
        conn.execute("""UPDATE receipts SET push_status='exported', export_batch=?
                        WHERE push_status='staged' AND gtin IN (SELECT gtin FROM item_master
                              WHERE fmv_status='verified' AND category IS NOT NULL)""", (batch,))
        conn.commit()
        # evidence file alongside
        ev = os.path.join(args.out, f"fmv_evidence_{batch}.csv")
        with open(ev, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["donation_id", "gtin", "name", "brand", "category", "units", "fmv_unit", "fmv_basis",
                        "fmv_source_url", "fmv_captured_at", "lookup_source"])
            for r in conn.execute("""SELECT r.donation_id, im.*, SUM(r.qty) units FROM receipts r
                                     JOIN item_master im ON im.gtin=r.gtin WHERE r.export_batch=?
                                     GROUP BY r.donation_id, im.gtin ORDER BY r.donation_id, im.category""", (batch,)):
                w.writerow([r["donation_id"], r["gtin"], r["name"], r["brand"], r["category"], r["units"],
                            r["fmv_unit"], r["fmv_basis"], r["fmv_source_url"], r["fmv_captured_at"], r["lookup_source"]])
        total = sum(r["amount"] for r in rows)
        print(f"Wrote {path}  ({len(rows)} JE line pairs, ${total:,.2f})\nWrote {ev}  (per-item URL evidence)\nBatch {batch}")
    else:  # items
        rows = conn.execute("""SELECT im.*, g.debit_account_id, g.credit_account_id FROM item_master im
                               LEFT JOIN gl_map g ON g.category = im.category
                               WHERE im.fmv_status='verified' AND im.ns_item_id IS NULL ORDER BY im.category, im.name""").fetchall()
        path = os.path.join(args.out, f"netsuite_items_{batch}.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["External ID", "Item Name/Number", "Display Name", "Type", "Asset Account", "Income Account",
                        "Purchase Price", "UPC Code", "Category", "FMV Source URL", "FMV Captured"])
            for r in rows:
                w.writerow([r["gtin"], r["gtin"], (r["name"] or "")[:60], "Non-inventory Item", r["debit_account_id"],
                            r["credit_account_id"], r["fmv_unit"], r["gtin"], r["category"], r["fmv_source_url"], r["fmv_captured_at"]])
        print(f"Wrote {path}  ({len(rows)} item records)")


# ---------------------------------------------------------------- master list (sheet-facing)

MASTER_COLUMNS = [
    "gtin", "name", "brand", "category", "fmv_unit", "fmv_basis", "fmv_status", "fmv_source_url", "fmv_merchant", "offer_updated",
    "amazon_search", "amazon_url", "amazon_price",
    "walmart_search", "walmart_url", "walmart_price",
    "target_search", "target_url", "target_price",
    "units_received", "first_seen", "last_checked", "notes",
]


def master_rows(conn):
    """One dict per barcode with the retailer links pivoted into columns."""
    items = conn.execute("""SELECT im.*, COALESCE(SUM(r.qty),0) AS units_received
                            FROM item_master im LEFT JOIN receipts r ON r.gtin = im.gtin
                            GROUP BY im.gtin HAVING units_received > 0 OR im.gtin NOT LIKE 'G-%'
                            ORDER BY im.category, im.name""").fetchall()
    links = {}
    for l in conn.execute("SELECT * FROM item_links"):
        links.setdefault(l["gtin"], {})[l["retailer"]] = l
    out = []
    for it in items:
        ensure_links(conn, it["gtin"])
        row = {c: "" for c in MASTER_COLUMNS}
        row.update({"gtin": it["gtin"], "name": it["name"] or "", "brand": it["brand"] or "",
                    "category": it["category"] or "", "fmv_unit": it["fmv_unit"] if it["fmv_unit"] is not None else "",
                    "fmv_basis": it["fmv_basis"], "fmv_status": it["fmv_status"],
                    "fmv_source_url": it["fmv_source_url"] or "", "fmv_merchant": it["fmv_merchant"] or "",
                    "offer_updated": it["offer_updated"] or "", "notes": it["notes"] or "",
                    "units_received": it["units_received"], "first_seen": (it["created_at"] or "")[:10]})
        if is_generic(it["gtin"]):
            q = urllib.parse.quote_plus(it["name"] or "")
            searches = {"amazon": f"https://www.amazon.com/s?k={q}", "walmart": f"https://www.walmart.com/search?q={q}",
                        "target": f"https://www.target.com/s?searchTerm={q}"}
        else:
            searches = retail_search_urls(it["gtin"])
        last = ""
        for r in RETAILERS:
            l = links.get(it["gtin"], {}).get(r)
            row[f"{r}_search"] = searches[r]
            row[f"{r}_url"] = (l["product_url"] if l else "") or ""
            row[f"{r}_price"] = (l["price"] if l and l["price"] is not None else "")
            if l and l["checked_at"]:
                last = max(last, l["checked_at"][:10])
        row["last_checked"] = last
        out.append(row)
    conn.commit()
    return out


def cmd_export_master(conn, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "item_master.csv")
    rows = master_rows(conn)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MASTER_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"Wrote {path}  ({len(rows)} barcodes). Import into the Google Sheet 'Items' tab, or run sheet_sync.py push.")


def import_master_rows(conn, rows, who="sheet"):
    """Apply reviewer edits from the sheet/CSV back to the database. Returns (updated_items, updated_links)."""
    now = datetime.now().isoformat(timespec="seconds")
    n_items = n_links = 0
    for r in rows:
        gtin = normalize_gtin(str(r.get("gtin", "")))
        if not gtin:
            continue
        if not conn.execute("SELECT 1 FROM item_master WHERE gtin=?", (gtin,)).fetchone():
            conn.execute("INSERT INTO item_master (gtin, lookup_source) VALUES (?, 'manual')", (gtin,))
        ensure_links(conn, gtin)
        sets, vals = [], []
        for col in ("name", "brand", "category", "fmv_source_url", "fmv_basis", "notes"):
            v = (r.get(col) or "").strip()
            if v:
                sets.append(f"{col}=?"); vals.append(v)
        fmv = (str(r.get("fmv_unit") or "")).replace("$", "").replace(",", "").strip()
        if fmv:
            try:
                sets.append("fmv_unit=?"); vals.append(float(fmv))
            except ValueError:
                pass
        status = (r.get("fmv_status") or "").strip().lower()
        if status in ("unverified", "verified", "disputed", "not_found"):
            sets.append("fmv_status=?"); vals.append(status)
        if sets:
            sets.append("updated_at=?"); vals.append(now); vals.append(gtin)
            conn.execute(f"UPDATE item_master SET {', '.join(sets)} WHERE gtin=?", vals)
            n_items += 1
        for retailer in RETAILERS:
            url = (r.get(f"{retailer}_url") or "").strip()
            price = str(r.get(f"{retailer}_price") or "").replace("$", "").replace(",", "").strip()
            if url or price:
                conn.execute("""UPDATE item_links SET product_url=COALESCE(NULLIF(?,''), product_url),
                                price=COALESCE(?, price), checked_at=?, checked_by=? WHERE gtin=? AND retailer=?""",
                             (url, float(price) if price else None, now, who, gtin, retailer))
                if price:
                    record_offers(conn, gtin, [{"merchant": retailer.title(), "price": float(price), "link": url or None,
                                                "condition": "New", "updated_t": int(time.time())}], "sheet")
                n_links += 1
        # a confirmed retailer URL can stand in as FMV evidence when none was set
        row = conn.execute("SELECT fmv_source_url, fmv_unit FROM item_master WHERE gtin=?", (gtin,)).fetchone()
        if not row["fmv_source_url"]:
            best = conn.execute("""SELECT product_url, price FROM item_links WHERE gtin=? AND product_url IS NOT NULL
                                   AND price IS NOT NULL ORDER BY price LIMIT 1""", (gtin,)).fetchone()
            if best:
                conn.execute("UPDATE item_master SET fmv_source_url=?, fmv_unit=COALESCE(fmv_unit, ?), fmv_captured_at=? WHERE gtin=?",
                             (best["product_url"], best["price"], now, gtin))
    conn.commit()
    return n_items, n_links


VALUES_COLUMNS = ["upc", "item", "brand", "category", "units_received", "value_used", "value_basis",
                  "merchant_1", "price_1", "url_1", "price_date_1",
                  "merchant_2", "price_2", "url_2", "price_date_2",
                  "merchant_3", "price_3", "url_3", "price_date_3",
                  "offers_seen", "status", "notes"]


def top_offers(conn, gtin14: str, n=3):
    """Up to n usable offers, one per merchant, preferring confirmed-by-reviewer, then newest, then the big-box stores."""
    rows = conn.execute("""SELECT merchant, price, url, offer_date, source, seen_at FROM offers
                           WHERE gtin=? AND usable=1 AND COALESCE(match_level, 'confirmed') = 'confirmed' ORDER BY
                             CASE WHEN source IN ('sheet','manual') THEN 0 ELSE 1 END,
                             CASE WHEN merchant IN ('Walmart','Target','Amazon') THEN 0 ELSE 1 END,
                             COALESCE(offer_date, '') DESC, price ASC""", (gtin14,)).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["merchant"] in seen:
            continue
        seen.add(r["merchant"]); out.append(r)
        if len(out) == n:
            break
    return out


def cmd_export_values(conn, out_dir):
    """The deliverable: item, UPC, and the value at up to three merchants, for every barcode in the catalog."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "gik_values.csv")
    items = conn.execute("""SELECT im.*, COALESCE(SUM(r.qty),0) AS units FROM item_master im
                            LEFT JOIN receipts r ON r.gtin=im.gtin GROUP BY im.gtin
                            HAVING units > 0 OR im.gtin NOT LIKE 'G-%'      -- generic catalog rows appear once something is received
                            ORDER BY im.category, im.name""").fetchall()
    n_priced = 0
    with open(path, "w", newline="") as f:
        w = csv.writer(f); w.writerow(VALUES_COLUMNS)
        for it in items:
            offers = top_offers(conn, it["gtin"])
            total = conn.execute("SELECT COUNT(*) FROM offers WHERE gtin=?", (it["gtin"],)).fetchone()[0]
            row = [it["gtin"][-12:] if it["gtin"].startswith("00") else it["gtin"][-13:], it["name"] or "", it["brand"] or "",
                   it["category"] or "", it["units"], it["fmv_unit"] if it["fmv_unit"] is not None else "", it["fmv_basis"]]
            for i in range(3):
                if i < len(offers):
                    o = offers[i]; row += [o["merchant"], f"{o['price']:.2f}", o["url"] or "", o["offer_date"] or o["seen_at"][:10]]
                else:
                    row += ["", "", "", ""]
            row += [total, it["fmv_status"], it["notes"] or ""]
            w.writerow(row)
            if offers:
                n_priced += 1
    print(f"Wrote {path}  ({len(items)} barcodes, {n_priced} with at least one merchant price)")
    return path


def cmd_master(args):
    conn = connect()
    if args.action == "import":
        with open(args.path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        n_items, n_links = import_master_rows(conn, rows, who=args.by or "csv")
        print(f"Imported {len(rows)} rows: {n_items} items updated, {n_links} retailer links updated")
    else:
        cmd_export_master(conn, args.out)


def cmd_links(args):
    conn = connect()
    gtin = normalize_gtin(args.gtin)
    if not gtin:
        sys.exit("invalid GTIN")
    item = conn.execute("SELECT name FROM item_master WHERE gtin=?", (gtin,)).fetchone()
    print(f"{gtin}  {item['name'] if item else '(not in master list yet)'}")
    for r, url in retail_search_urls(gtin).items():
        l = conn.execute("SELECT product_url, price FROM item_links WHERE gtin=? AND retailer=?", (gtin, r)).fetchone()
        confirmed = f"  → {l['product_url']} (${l['price']:.2f})" if l and l["product_url"] else ""
        print(f"  {r:<8} {url}{confirmed}")
    print(f"  google   {GOOGLE_SHOPPING.format(upc=gtin_for_lookup(gtin))}")


def cmd_locations(args):
    conn = connect()
    if args.action == "add":
        conn.execute("INSERT OR REPLACE INTO locations (code, rack, description, active) VALUES (?,?,?,1)",
                     (args.code.upper(), (args.rack or args.code[0]).upper(), args.description or args.code.upper()))
        conn.commit(); print(f"Added location {args.code.upper()}")
    elif args.action == "retire":
        conn.execute("UPDATE locations SET active=0 WHERE code=?", (args.code.upper(),)); conn.commit()
        print(f"Retired {args.code.upper()} (history kept)")
    else:
        print(f"{'Code':<10}{'Rack':<6}{'Units':>8}  Description")
        for r in conn.execute("""SELECT l.code, l.rack, l.description, l.active, COALESCE(SUM(r.qty),0) units
                                 FROM locations l LEFT JOIN receipts r ON r.location = l.code
                                 GROUP BY l.code ORDER BY l.rack, l.code"""):
            print(f"{r['code']:<10}{r['rack']:<6}{r['units']:>8}  {r['description']}{'' if r['active'] else '  (retired)'}")
        n = conn.execute("SELECT COALESCE(SUM(qty),0) FROM receipts WHERE location IS NULL").fetchone()[0]
        if n:
            print(f"{'(none)':<10}{'':<6}{n:>8}  scanned before locations existed, or without a location set")


def cmd_labels(args):
    """Printable sheet of scannable command barcodes: one per location plus q12/q24/q48 and the undo/finish commands.
    Uses JsBarcode from cdnjs, so open it with internet access, then print (works offline afterwards as a PDF)."""
    conn = connect()
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "scan_labels.html")
    locs = conn.execute("SELECT code, description FROM locations WHERE active=1 ORDER BY rack, code").fetchall()
    cmds = [("q12", "Next scan = case of 12"), ("q24", "Next scan = case of 24"), ("q48", "Next scan = case of 48"),
            ("-", "Undo last scan"), ("?", "Show running count"), ("x", "Finish session")]
    cells = []
    for code, desc in [("@" + r["code"], r["description"]) for r in locs] + cmds:
        cells.append(f'<div class="label"><svg class="bc" jsbarcode-value="{code}"></svg><div class="t">{code}</div><div class="d">{desc}</div></div>')
    html = f"""<!doctype html><html><head><meta charset="utf-8"><title>GIK scan labels</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/jsbarcode/3.11.6/JsBarcode.all.min.js"></script>
<style>body{{font-family:Arial,sans-serif;margin:16px}} h1{{font-size:18px;margin:0 0 4px}} p{{margin:0 0 12px;color:#444;font-size:12px}}
.grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}} .label{{border:1px solid #999;border-radius:6px;padding:8px;text-align:center;page-break-inside:avoid}}
.t{{font-weight:bold;font-size:16px}} .d{{font-size:11px;color:#333}} @media print{{.grid{{gap:6px}}}}</style></head><body>
<h1>GIK intake: scan labels</h1><p>Scan a location label before putting a pallet away; everything scanned after it is recorded there. Scan a q-label before a sealed case. Laminate and keep at the dock.</p>
<div class="grid">{''.join(cells)}</div>
<script>JsBarcode(".bc", {{format:"CODE128", width:2, height:48, displayValue:false, margin:4}});</script></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Wrote {path}  ({len(locs)} location labels + {len(cmds)} command labels). Open in a browser and print.")


def cmd_pushed(args):
    conn = connect()
    n = conn.execute("UPDATE receipts SET push_status='pushed', ns_tran_id=? WHERE export_batch=? AND push_status='exported'",
                     (args.ns_tran, args.batch)).rowcount
    conn.commit()
    print(f"Marked {n} receipts in batch {args.batch} as pushed (NS tran {args.ns_tran})")


def cmd_report(_args):
    conn = connect()
    print("\nIn-kind received by donor (verified FMV only)")
    for r in conn.execute("""SELECT dn.name, SUM(r.qty) units, ROUND(SUM(r.qty*im.fmv_unit),2) fmv
                             FROM receipts r JOIN item_master im ON im.gtin=r.gtin
                             JOIN donations d ON d.donation_id=r.donation_id JOIN donors dn ON dn.donor_id=d.donor_id
                             WHERE im.fmv_status='verified' GROUP BY dn.name ORDER BY fmv DESC"""):
        print(f"  {r['name']:<35}{r['units']:>8} units  ${r['fmv']:>12,.2f}")
    print("\nBy warehouse location (all receipts)")
    for r in conn.execute("""SELECT COALESCE(r.location,'(none)') loc, COUNT(DISTINCT r.gtin) skus, SUM(r.qty) units,
                             ROUND(SUM(r.qty*COALESCE(im.fmv_unit,0)),2) fmv FROM receipts r JOIN item_master im ON im.gtin=r.gtin
                             GROUP BY loc ORDER BY units DESC"""):
        print(f"  {r['loc']:<12}{r['skus']:>5} SKUs {r['units']:>8} units  ${r['fmv']:>12,.2f}")
    print("\nBy category (ASU 2020-07 disclosure roll-up)")
    for r in conn.execute("""SELECT im.category, SUM(r.qty) units, ROUND(SUM(r.qty*im.fmv_unit),2) fmv
                             FROM receipts r JOIN item_master im ON im.gtin=r.gtin
                             WHERE im.fmv_status='verified' GROUP BY im.category ORDER BY fmv DESC"""):
        print(f"  {(r['category'] or 'Uncategorized'):<35}{r['units']:>8} units  ${r['fmv']:>12,.2f}")
    q = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(units_waiting),0) u FROM v_review_queue").fetchone()
    print(f"\nReview queue: {q['n']} barcodes, {q['u']} units waiting\n")

# ---------------------------------------------------------------- cloud (Supabase Postgres, schema "gik")

CLOUD_TABLES = ["donors", "donations", "gl_map", "gs1_prefix", "locations", "item_master", "generic_items",
                "receipts", "offers", "item_links", "lookup_log"]
CLOUD_DEFAULT_URI = "postgresql://postgres.zwqlbnnvbcvktfcqwwvb:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
CLOUD_SEQUENCES = {"donors": ("donor_id", "donors_donor_id_seq"), "receipts": ("receipt_id", "receipts_receipt_id_seq"),
                   "offers": ("offer_id", "offers_offer_id_seq"), "lookup_log": ("log_id", "lookup_log_log_id_seq")}


def cloud_url():
    """Postgres connection URI from supabase_db.txt (one line) or the GIK_DATABASE_URL environment variable."""
    url = os.environ.get("GIK_DATABASE_URL", "").strip()
    if not url and os.environ.get("GIK_DB_PASSWORD"):
        from urllib.parse import quote
        url = CLOUD_DEFAULT_URI.replace("[YOUR-PASSWORD]", quote(os.environ["GIK_DB_PASSWORD"].strip(), safe=""))
    path = os.path.join(HERE, "supabase_db.txt")
    if not url and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip().lstrip("﻿") for ln in f if ln.strip() and not ln.lstrip("﻿").startswith("#")]
            url = lines[0] if lines else ""
            if url and "://" not in url:
                # The file holds just the password: use TEP's Session-pooler address.
                lines = [CLOUD_DEFAULT_URI, url]
                url = CLOUD_DEFAULT_URI
            # Convenience: the database password may be on the second line instead of edited into the URI.
            if "[YOUR-PASSWORD]" in url and len(lines) > 1:
                from urllib.parse import quote
                url = url.replace("[YOUR-PASSWORD]", quote(lines[1], safe=""))
    if not url or "[YOUR-PASSWORD]" in url:
        sys.exit("No cloud connection string. In Supabase: project tep-teacher-platform > Connect > Session pooler, copy the\n"
                 "URI, replace [YOUR-PASSWORD] with the database password, and save it as the only line of supabase_db.txt next to gik.py.")
    return url


def cloud_connect():
    try:
        import psycopg2  # noqa
    except ImportError:
        print("Installing psycopg2-binary (one time)...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "psycopg2-binary"])
        import psycopg2  # noqa
    import psycopg2
    pg = psycopg2.connect(cloud_url())
    pg.autocommit = False
    with pg.cursor() as cur:
        cur.execute("set search_path = gik, public")
    return pg


def cloud_columns(pg, table):
    with pg.cursor() as cur:
        cur.execute("""select column_name, data_type from information_schema.columns
                       where table_schema='gik' and table_name=%s order by ordinal_position""", (table,))
        cols = cur.fetchall()
        cur.execute("""select kcu.column_name from information_schema.table_constraints tc
                       join information_schema.key_column_usage kcu on kcu.constraint_name=tc.constraint_name and kcu.table_schema=tc.table_schema
                       where tc.table_schema='gik' and tc.table_name=%s and tc.constraint_type='PRIMARY KEY' order by kcu.ordinal_position""", (table,))
        pk = [r[0] for r in cur.fetchall()]
    return {c: t for c, t in cols}, pk


def _pg_value(v, dt):
    if v is None:
        return None
    if dt == "boolean":
        return bool(v)
    if isinstance(v, str):
        if dt in ("integer", "bigint", "numeric") and v.strip() == "":
            return None
        return v.replace("\x00", "")
    return v


def _sqlite_value(v):
    import decimal
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, decimal.Decimal):
        return float(v)
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ") if hasattr(v, "hour") else v.isoformat()
    return v


# How each table goes up from a computer's gik.db. Rows created on the web app get their ids from the cloud,
# so a computer never sends its own receipt/offer/log ids (they would collide); duplicates are caught by
# natural keys instead. Anything a person verified in the cloud is never overwritten.
PUSH_RULES = {
    "donors":        {"conflict": "do nothing"},
    "donations":     {"conflict": "do nothing"},
    "gl_map":        {"conflict": "do nothing"},
    "gs1_prefix":    {"conflict": "do nothing"},
    "locations":     {"conflict": "do nothing"},
    "generic_items": {"conflict": "do nothing"},
    "item_master":   {"conflict": "update unless verified"},
    "item_links":    {"conflict": "update"},
    "receipts":      {"drop": ["receipt_id"], "origin": True, "conflict": "do nothing"},
    "offers":        {"drop": ["offer_id"], "conflict": "do nothing"},
    "lookup_log":    {"drop": ["log_id"], "conflict": "do nothing"},
}


def _push_rows(cur, pg, table, local_cols, rows):
    """Insert rows (sequence of tuples matching local_cols) into gik.<table> following PUSH_RULES."""
    rule = PUSH_RULES[table]
    pg_cols, pk = cloud_columns(pg, table)
    cols = [c for c in local_cols if c in pg_cols and c not in rule.get("drop", [])]
    idx = [local_cols.index(c) for c in cols]
    out_cols = cols + (["origin"] if rule.get("origin") else [])
    data = []
    for r in rows:
        vals = [_pg_value(r[i], pg_cols[c]) for i, c in zip(idx, cols)]
        if rule.get("origin"):
            d = dict(zip(local_cols, r))
            vals.append(f"{str(d.get('scanned_at'))[:19]}|{d.get('gtin')}|{d.get('receipt_id')}")
        data.append(tuple(vals))
    if not data:
        return 0
    if rule["conflict"] == "do nothing":
        tail = "on conflict do nothing"
    else:
        upd = [c for c in cols if c not in pk]
        tail = f"on conflict ({','.join(pk)}) do update set " + ", ".join(f"{c}=excluded.{c}" for c in upd)
        if rule["conflict"] == "update unless verified":
            tail += f" where gik.{table}.fmv_status <> 'verified'"
    cur.executemany(f"insert into gik.{table} ({','.join(out_cols)}) values ({','.join(['%s'] * len(out_cols))}) {tail}", data)
    return len(data)


def cloud_pull(conn, pg):
    """Copy the cloud gik schema into this (scratch) gik.db; cloud wins. Used by `cloud sync`."""
    counts = {}
    with pg.cursor() as cur:
        for t in CLOUD_TABLES:
            local_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
            if not local_cols:
                continue
            pg_cols, _ = cloud_columns(pg, t)
            cols = [c for c in local_cols if c in pg_cols]
            pk = [r[1] for r in sorted(conn.execute(f"PRAGMA table_info({t})"), key=lambda r: r[5]) if r[5]]
            if t == "lookup_log":  # only what the run needs: last 90 days, raw text only for answers the rules re-read
                sel = ", ".join(f"case when source in ('upcitemdb','upcdatabase','googlebooks') then {c} end" if c == "raw_response" else c
                                for c in cols)
                cur.execute(f"select {sel} from gik.lookup_log where requested_at > now() - interval '90 days'")
            else:
                cur.execute(f"select {','.join(cols)} from gik.{t}")
            rows = cur.fetchall()
            upd = [c for c in cols if c not in pk]
            tail = (f"ON CONFLICT({','.join(pk)}) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in upd)) if pk and upd else "ON CONFLICT DO NOTHING"
            conn.executemany(f"INSERT INTO {t} ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))}) {tail}",
                             [tuple(_sqlite_value(v) for v in r) for r in rows])
            counts[t] = len(rows)
    conn.commit()
    return counts


def cmd_cloud(args):
    """cloud push: merge this computer's gik.db into the Supabase gik schema (safe to re-run, never clobbers web scans
       or verified values). cloud status: row counts. cloud sync: pull the cloud into a scratch db, run the free
       lookups for every barcode still missing a name/price, and push back only what the lookups found."""
    conn = connect()
    pg = cloud_connect()
    local_tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if args.action == "status":
        print(f"{'table':<14}{'local':>8}{'cloud':>8}")
        with pg.cursor() as cur:
            for t in CLOUD_TABLES:
                n_local = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] if t in local_tables else 0
                cur.execute(f"select count(*) from gik.{t}")
                print(f"{t:<14}{n_local:>8}{cur.fetchone()[0]:>8}")
        pg.close()
        return
    if args.action == "sync":
        return cloud_sync(conn, pg, args)
    total = 0
    try:
        with pg.cursor() as cur:
            for t in CLOUD_TABLES:
                if t not in local_tables:
                    continue
                local_cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
                rows = conn.execute(f"SELECT {','.join(local_cols)} FROM {t}").fetchall()
                n = _push_rows(cur, pg, t, local_cols, [tuple(r) for r in rows])
                total += n
                print(f"  {t}: {n} rows sent")
            cur.execute("select setval('gik.donors_donor_id_seq', greatest((select coalesce(max(donor_id),1) from gik.donors), 1))")
        pg.commit()
        print(f"\nDone: {total} rows merged into Supabase (schema gik). Run 'py gik.py cloud status' to compare counts.")
    except Exception as e:
        pg.rollback()
        sys.exit(f"\nPush failed, nothing changed in the cloud: {e}")
    finally:
        pg.close()


LOOKUP_COLS = ["name", "brand", "category", "raw_category", "fmv_unit", "fmv_basis", "fmv_source_url", "fmv_merchant",
               "offer_updated", "fmv_captured_at", "fmv_status", "lookup_source", "notes", "updated_at", "model_number"]


def cloud_sync(conn, pg, args):
    counts = cloud_pull(conn, pg)
    print("Pulled from cloud: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    max_offer = conn.execute("SELECT COALESCE(MAX(offer_id),0) FROM offers").fetchone()[0]
    max_log = conn.execute("SELECT COALESCE(MAX(log_id),0) FROM lookup_log").fetchone()[0]
    before = {r["gtin"]: tuple(r) for r in conn.execute(f"SELECT gtin,{','.join(LOOKUP_COLS)} FROM item_master")}
    conn.close()
    cmd_retry(args)
    cmd_refresh(args)
    if getattr(args, "seed", 0):  # catalog seed: pre-load likely items (catalog_seed.py + catalog_terms.txt)
        try:
            import catalog_seed
            c2 = connect()
            catalog_seed.run(c2, args.seed, sys.modules[__name__])
            c2.close()
        except Exception as e:
            print(f"Catalog seed skipped: {e}")
    conn = connect()
    changed = [tuple(r) for r in conn.execute(f"SELECT gtin,{','.join(LOOKUP_COLS)} FROM item_master") if before.get(r["gtin"]) != tuple(r)]
    new_offers = conn.execute("SELECT * FROM offers WHERE offer_id > ?", (max_offer,)).fetchall()
    new_logs = conn.execute("SELECT * FROM lookup_log WHERE log_id > ?", (max_log,)).fetchall()
    for attempt in range(1, 4):  # a deadlock with another writer (e.g. maintenance) is retried, never lost
      try:
        with pg.cursor() as cur:
            # only the lookup columns of changed barcodes; verified rows in the cloud are left alone
            cols = ["gtin"] + LOOKUP_COLS
            pg_cols, _ = cloud_columns(pg, "item_master")
            sets = ", ".join(f"{c}=excluded.{c}" for c in LOOKUP_COLS)
            cur.executemany(f"insert into gik.item_master ({','.join(cols)}) values ({','.join(['%s'] * len(cols))}) "
                            f"on conflict (gtin) do update set {sets} where gik.item_master.fmv_status <> 'verified'",
                            [tuple(_pg_value(v, pg_cols[c]) for c, v in zip(cols, r)) for r in changed])
            o_cols = list(new_offers[0].keys()) if new_offers else []
            n_off = _push_rows(cur, pg, "offers", o_cols, [tuple(r) for r in new_offers]) if new_offers else 0
            l_cols = list(new_logs[0].keys()) if new_logs else []
            ri = l_cols.index("raw_response") if "raw_response" in l_cols else -1
            keep_raw = ("upcitemdb", "upcdatabase", "googlebooks")  # re-read by the refresh rules; others: 2 KB is enough
            log_rows = [tuple((v[:2000] if (i == ri and v and r["source"] not in keep_raw and r["http_status"] == 200) else v)
                              for i, v in enumerate(r)) for r in new_logs]
            n_log = _push_rows(cur, pg, "lookup_log", l_cols, log_rows) if new_logs else 0
        pg.commit()
        print(f"\nSynced to cloud: {len(changed)} barcodes updated, {n_off} new prices, {n_log} lookup records.")
        break
      except Exception as e:
        pg.rollback()
        if getattr(e, "pgcode", None) in ("40P01", "40001") and attempt < 3:  # deadlock / serialization: wait, retry
            print(f"\nSync push hit a lock conflict (attempt {attempt}); retrying in {20 * attempt}s")
            time.sleep(20 * attempt)
            continue
        pg.close()
        sys.exit(f"\nSync push failed, nothing changed in the cloud: {e}")
    pg.close()


# ---------------------------------------------------------------- cli

def main():
    p = argparse.ArgumentParser(description="GIK barcode intake prototype")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)

    d = sub.add_parser("donor"); d.add_argument("action", choices=["add", "list"]); d.add_argument("name", nargs="?")
    d.add_argument("--type", default="corporate", choices=["corporate", "freight-in-kind", "individual", "nerc-partner", "other"])
    d.add_argument("--ns-id", dest="ns_id"); d.add_argument("--contact"); d.set_defaults(fn=cmd_donor)

    dn = sub.add_parser("donation"); dn.add_argument("action", choices=["new", "list"]); dn.add_argument("donation_id", nargs="?")
    dn.add_argument("--donor"); dn.add_argument("--date"); dn.add_argument("--bol"); dn.add_argument("--pallets", type=int)
    dn.add_argument("--restriction"); dn.add_argument("--notes"); dn.set_defaults(fn=cmd_donation)

    i = sub.add_parser("intake"); i.add_argument("donation_id"); i.add_argument("--by", default=os.environ.get("USER", ""))
    i.add_argument("--location", help="starting rack/bin code, e.g. C03Z (can be changed mid-session with @CODE)")
    i.add_argument("--mock", action="store_true", help="use mock_lookups.json instead of the live API"); i.set_defaults(fn=cmd_intake)

    l = sub.add_parser("lookup"); l.add_argument("gtin"); l.add_argument("--mock", action="store_true"); l.set_defaults(fn=cmd_lookup)

    rt = sub.add_parser("retry", help="look up barcodes that were throttled or deferred during intake")
    rt.add_argument("--all", action="store_true", help="also re-try barcodes the database reported as not found")
    rt.add_argument("--gap", type=float, default=7.0, help="seconds between lookups on the free tier (default 7)")
    rt.add_argument("--mock", action="store_true"); rt.set_defaults(fn=cmd_retry)
    sub.add_parser("refresh", help="re-apply current price/category rules to saved lookups (no web calls)").set_defaults(fn=cmd_refresh)
    te = sub.add_parser("test-ebay", help="check the eBay keys with one lookup"); te.add_argument("gtin", nargs="?"); te.set_defaults(fn=cmd_test_ebay)
    sub.add_parser("review").set_defaults(fn=cmd_review)

    v = sub.add_parser("verify"); v.add_argument("gtin"); v.add_argument("--fmv", type=float); v.add_argument("--url")
    v.add_argument("--category"); v.add_argument("--name"); v.add_argument("--brand")
    v.add_argument("--basis", choices=["retail", "wholesale", "appraised"]); v.add_argument("--dispute", action="store_true")
    v.set_defaults(fn=cmd_verify)

    e = sub.add_parser("export"); e.add_argument("what", choices=["je", "items", "master", "values", "receipts"]); e.add_argument("--out", default=os.path.join(HERE, "exports"))
    e.set_defaults(fn=cmd_export)

    m = sub.add_parser("master", help="master list CSV round-trip with the Google Sheet")
    m.add_argument("action", choices=["export", "import"]); m.add_argument("path", nargs="?")
    m.add_argument("--out", default=os.path.join(HERE, "exports")); m.add_argument("--by"); m.set_defaults(fn=cmd_master)

    lk = sub.add_parser("links"); lk.add_argument("gtin"); lk.set_defaults(fn=cmd_links)

    lo = sub.add_parser("locations", help="warehouse rack/bin codes"); lo.add_argument("action", nargs="?", default="list", choices=["list", "add", "retire"])
    lo.add_argument("code", nargs="?"); lo.add_argument("--rack"); lo.add_argument("--description"); lo.set_defaults(fn=cmd_locations)
    lb = sub.add_parser("labels", help="printable sheet of location + command barcodes"); lb.add_argument("--out", default=os.path.join(HERE, "exports")); lb.set_defaults(fn=cmd_labels)

    fd = sub.add_parser("find", help="search the generic (no-barcode) catalog"); fd.add_argument("words", nargs="+"); fd.set_defaults(fn=cmd_find)
    g = sub.add_parser("generic", help="manage non-barcoded items"); g.add_argument("action", choices=["add", "value", "pending"])
    g.add_argument("name", nargs="?", help="item name (add) or code like G-000123 (value)")
    g.add_argument("--category", choices=["Appliances", "Household goods", "Furniture", "Clothing", "Office", "Kids & toys", "Seasonal & decor", "Miscellaneous"])
    g.add_argument("--keywords"); g.add_argument("--low", type=float); g.add_argument("--high", type=float); g.add_argument("--unit")
    g.add_argument("--source"); g.add_argument("--url"); g.add_argument("--by"); g.set_defaults(fn=lambda a: cmd_generic(_generic_args(a)))

    pu = sub.add_parser("pushed"); pu.add_argument("batch"); pu.add_argument("--ns-tran", dest="ns_tran", required=True); pu.set_defaults(fn=cmd_pushed)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    cl = sub.add_parser("cloud", help="copy this computer's gik.db into the shared Supabase database (needs supabase_db.txt)")
    cl.add_argument("action", choices=["push", "status", "sync"])
    cl.add_argument("--max", type=int, default=0, help="sync: look up at most this many barcodes per run")
    cl.add_argument("--gap", type=float, default=7.0); cl.add_argument("--all", action="store_true"); cl.add_argument("--mock", action="store_true")
    cl.add_argument("--seed", type=int, default=0, help="sync: also seed the catalog with up to this many search terms (catalog_terms.txt)")
    cl.set_defaults(fn=cmd_cloud)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
