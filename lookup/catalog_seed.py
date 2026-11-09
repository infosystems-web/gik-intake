"""Catalog seed: pre-load item_master with products TEP is likely to receive, before anyone scans them, so a scan
of a common item is already named and priced (with a proof link) the moment it lands.

Source 1 (live): Kroger product search by keyword. Free API, same KROGER_CLIENT_ID/SECRET as extra_sources.py,
priced at the nearest Kroger-family store to TEP (regular shelf price, not promo).

Search terms live in catalog_terms.txt, one "Category | search term" per line (Category must match gl_map).
Each `gik.py cloud sync --seed N` run seeds up to N terms that were not seeded in the last 30 days. Progress is
kept in lookup_log as gtin='SEED:kroger:<term>', which the sync pulls from the cloud, so runs resume on their own.

Seeded rows: fmv_status 'unverified', lookup_source 'kroger-seed', notes start with "Catalog seed". A barcode that
already has a price, or is verified, is never overwritten; its Kroger price is just saved as one more offer.
"""
import json
import os
import re
import time
import urllib.parse
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TERMS_FILE = os.path.join(HERE, "catalog_terms.txt")
PAGES = int(os.environ.get("GIK_SEED_PAGES", "5"))   # Kroger returns up to 50 products per page (start max 250)
REDO_DAYS = 30
# Kroger store departments that are groceries; skipped unless the term itself is a food term.
FOOD_DEPTS = re.compile(r"produce|meat|seafood|dairy|bakery|deli|frozen|beverage|snack|pantry|breakfast|candy|"
                        r"adult beverage|wine|beer|natural & organic", re.I)


def load_terms(path=TERMS_FILE):
    terms = []
    if not os.path.exists(path):
        return terms
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "|" not in line:
                continue
            cat, term = (s.strip() for s in line.split("|", 1))
            if cat and term:
                terms.append((cat, term))
    return terms


def gtin_from_kroger(upc):
    """Kroger's 13-digit upc is the GTIN without its check digit (UPC-A 011110417005 -> 0001111041700)."""
    d = re.sub(r"\D", "", upc or "")
    if len(d) != 13 or d.startswith("00000") or d.startswith("002"):   # PLU / random-weight store codes
        return None
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(d))
    return d + str((10 - total % 10) % 10)


def brand_terms(conn, terms):
    """Expansion: every brand that showed up 3+ times in the seeded catalog becomes its own search term, in the
    category where it appeared most, so the catalog keeps widening after the starter list is done."""
    have = {t.lower() for _, t in terms}
    rows = conn.execute("""SELECT brand, category, COUNT(*) n FROM item_master
                           WHERE lookup_source='kroger-seed' AND brand IS NOT NULL AND brand <> '' AND category IS NOT NULL
                           GROUP BY brand, category HAVING n >= 3 ORDER BY n DESC""").fetchall()
    out, seen = [], set()
    for r in rows:
        b = r["brand"].strip()
        if b.lower() in have or b.lower() in seen or b.lower() == "kroger":
            continue
        seen.add(b.lower())
        out.append((r["category"], b))
    return out


def _due_terms(conn, terms):
    done = {r[0] for r in conn.execute(
        "SELECT gtin FROM lookup_log WHERE source='kroger-seed' AND requested_at > datetime('now', ?)", (f"-{REDO_DAYS} days",))}
    allt = terms + brand_terms(conn, terms)
    return [(c, t) for c, t in allt if f"SEED:kroger:{t.lower()}" not in done]


def _kroger_search(es, http_get, term, start, loc, token):
    params = {"filter.term": term, "filter.limit": 50, **({"filter.locationId": loc} if loc else {})}
    if start:  # products to skip; Kroger allows 1..250, so the first page leaves it out
        params["filter.start"] = min(start, 250)
    q = urllib.parse.urlencode(params)
    return http_get(f"{es.KROGER_API}/products?{q}", {"Authorization": f"Bearer {es._kroger_token()}"})  # token auto-renews


def seed_term(conn, gik, es, category, term, now, loc, token):
    """Search one term; returns (http_status, products_seen, new_items, extra_offers)."""
    seen = new = more = 0
    status = 0
    for page in range(PAGES):
        status, body = _kroger_search(es, gik.http_get, term, page * 50, loc, token)
        if status != 200:
            break
        data = (json.loads(body) or {}).get("data") or []
        for p in data:
            gtin = gtin_from_kroger(p.get("upc") or p.get("productId"))
            if not gtin:
                continue
            depts = " ".join(p.get("categories") or [])
            if category != "Food & beverage" and FOOD_DEPTS.search(depts):
                continue
            seen += 1
            it = (p.get("items") or [{}])[0]
            price = (it.get("price") or {}).get("regular") or (it.get("nationalPrice") or {}).get("regular")
            name = (p.get("description") or "").strip()
            size = (it.get("size") or "").strip()
            if size and size.lower() not in name.lower():
                name = f"{name}, {size}"
            link = ("https://www.kroger.com" + p["productPageURI"]) if p.get("productPageURI") else \
                   f"https://www.kroger.com/search?query={gtin.lstrip('0')}"
            store = es._kroger.get("store_name") or "Kroger"
            row = conn.execute("SELECT fmv_unit, fmv_status FROM item_master WHERE gtin=?", (gtin,)).fetchone()
            if price:
                gik.record_offers(conn, gtin, [{"merchant": store, "price": float(price), "link": link, "condition": "New",
                                                "title": name, "updated_t": int(time.time())}], "kroger")
            if row and (row["fmv_unit"] is not None or row["fmv_status"] == "verified"):
                more += 1 if price else 0
                continue
            note = (f"Catalog seed (search '{term}'): Kroger regular shelf price at {store}; not scanned yet; confirm at a store"
                    if price else f"Catalog seed (search '{term}'): name from Kroger, no shelf price yet")
            conn.execute("""INSERT INTO item_master (gtin, name, brand, category, raw_category, fmv_unit, fmv_source_url, fmv_merchant,
                                                     fmv_captured_at, fmv_status, lookup_source, notes, updated_at)
                            VALUES (?,?,?,?,?,?,?,?,?,'unverified','kroger-seed',?,?)
                            ON CONFLICT(gtin) DO UPDATE SET name=COALESCE(item_master.name, excluded.name),
                              brand=COALESCE(item_master.brand, excluded.brand), category=COALESCE(item_master.category, excluded.category),
                              raw_category=COALESCE(item_master.raw_category, excluded.raw_category),
                              fmv_unit=excluded.fmv_unit, fmv_source_url=excluded.fmv_source_url, fmv_merchant=excluded.fmv_merchant,
                              fmv_captured_at=excluded.fmv_captured_at, lookup_source=excluded.lookup_source,
                              notes=excluded.notes, updated_at=excluded.updated_at""",
                         (gtin, name or None, p.get("brand"), category, depts[:200] or None, float(price) if price else None,
                          link if price else None, store if price else None, now if price else None, note, now))
            gik.ensure_links(conn, gtin)
            new += 1
        if len(data) < 50:
            break
        time.sleep(0.2)
    return status, seen, new, more


def run(conn, budget, gik):
    """Seed up to `budget` search terms. Never raises: a seed problem must not stop the lookup sync."""
    try:
        import extra_sources as es
    except Exception as e:
        print(f"Catalog seed skipped: extra_sources.py not available ({e})")
        return
    if not es.kroger_keys()[0]:
        print("Catalog seed skipped: no Kroger keys")
        return
    terms = load_terms()
    due = _due_terms(conn, terms)[:budget]
    if not due:
        print(f"Catalog seed: all {len(terms)} terms seeded in the last {REDO_DAYS} days")
        return
    try:
        token = es._kroger_token()
        loc = es._kroger_location(gik.http_get)
    except Exception as e:
        print(f"Catalog seed skipped: Kroger sign-in failed ({e})")
        return
    now = datetime.now().isoformat(timespec="seconds")
    tot_new = tot_seen = 0
    for category, term in due:
        try:
            st, seen, new, more = seed_term(conn, gik, es, category, term, now, loc, token)
        except Exception as e:
            st, seen, new, more = 0, 0, 0, 0
            print(f"  seed '{term}': error {e}")
        conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES (?,?,?,?)",
                     (f"SEED:kroger:{term.lower()}", "kroger-seed", st,
                      json.dumps({"term": term, "category": category, "products": seen, "new_items": new, "extra_offers": more})))
        conn.commit()
        tot_new += new
        tot_seen += seen
        print(f"  seed '{term}' ({category}): {seen} products, {new} new catalog items")
    print(f"Catalog seed: {len(due)} terms, {tot_seen} products seen, {tot_new} new catalog items "
          f"({len(_due_terms(conn, terms))} terms still to do)")
