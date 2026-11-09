"""Catalog seed: pre-load item_master with products TEP is likely to receive, before anyone scans them, so a scan
of a common item is already named and priced (with a proof link) the moment it lands.

Source 1 (live): Kroger product search by keyword. Free API, same KROGER_CLIENT_ID/SECRET as extra_sources.py,
priced at the nearest Kroger-family store to TEP (regular shelf price, not promo).

Search terms live in catalog_terms.txt, one "Category | search term" per line (Category must match gl_map).
Each `gik.py cloud sync --seed N` run seeds up to N terms that were not seeded in the last 30 days. Progress is
kept in lookup_log as gtin='SEED:kroger:<term>', which the sync pulls from the cloud, so runs resume on their own.

Source 2 (live): Google Books search (GOOGLE_BOOKS_KEY) for children's, classroom and teacher books, queries in
catalog_books.txt; ISBN-13 becomes the GTIN, publisher list price is the price when Google has one.
Price fill: catalog items still without a price are sent through the normal price chain (Kroger, Walmart, Best Buy,
Open Prices, then eBay median of new listings), up to --fill N per run, so the free daily limits get used.

Seeded rows: fmv_status 'unverified', lookup_source 'kroger-seed' or 'gbooks-seed', notes start with "Catalog seed". A barcode that
already has a price, or is verified, is never overwritten; its Kroger price is just saved as one more offer.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
TERMS_FILE = os.path.join(HERE, "catalog_terms.txt")
BOOKS_FILE = os.path.join(HERE, "catalog_books.txt")
BOOK_PAGES = int(os.environ.get("GIK_SEED_BOOK_PAGES", "5"))  # Google Books returns up to 40 per page
BOOKS_PER_RUN = int(os.environ.get("GIK_SEED_BOOKS", "15"))   # 15 queries x 5 pages = 75 calls/run, ~600/day of 1,000
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
    # learned from scans first: any brand TEP has actually received (1 scan is enough), then brands seen 3+ times
    # in the seeded catalog. Books are left to the book queries.
    rows = conn.execute("""SELECT im.brand, im.category, 1000 + COUNT(*) n FROM item_master im
                           WHERE im.gtin IN (SELECT gtin FROM receipts) AND im.brand IS NOT NULL AND im.brand <> ''
                             AND im.category IS NOT NULL AND im.category <> 'Books & media' AND im.gtin NOT LIKE 'G-%'
                           GROUP BY im.brand, im.category
                           UNION ALL
                           SELECT brand, category, COUNT(*) n FROM item_master
                           WHERE lookup_source='kroger-seed' AND brand IS NOT NULL AND brand <> '' AND category IS NOT NULL
                           GROUP BY brand, category HAVING COUNT(*) >= 3
                           ORDER BY n DESC""").fetchall()
    out, seen = [], set()
    for r in rows:
        b = r["brand"].strip()
        if b.lower() in have or b.lower() in seen or b.lower() in ("kroger", "unknown", "generic") or len(b) < 3:
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


FILL_PER_RUN = int(os.environ.get("GIK_FILL", "400"))   # eBay allows 5,000/day; 8 runs x 400 = 3,200


def run(conn, budget, gik):
    """Books, then Kroger terms, then price fill. Never raises: a seed problem must not stop the lookup sync."""
    for step in (lambda: seed_books(conn, gik), lambda: seed_kroger(conn, budget, gik),
                 lambda: fill_prices(conn, gik, FILL_PER_RUN), lambda: three_places(conn, gik, SERPER_PER_RUN)):
        try:
            step()
        except Exception as e:
            print(f"Catalog step skipped: {e}")


def seed_kroger(conn, budget, gik):
    """Seed up to `budget` Kroger search terms."""
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


# ---------------------------------------------------------------- books (Google Books search)

def load_book_queries(path=None):
    path = path or BOOKS_FILE
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]


def scanned_book_queries(conn):
    """Learn from scans: every author and publisher of a book TEP actually received becomes a Google Books query."""
    out, seen = [], set()
    for r in conn.execute("""SELECT name, brand FROM item_master WHERE (gtin LIKE '0978%' OR gtin LIKE '0979%')
                             AND gtin IN (SELECT gtin FROM receipts) ORDER BY updated_at DESC""").fetchall():
        author = (r["name"] or "").split(" - by ")[1].split(",")[0].strip() if " - by " in (r["name"] or "") else ""
        for q in ((f'inauthor:"{author}"' if len(author) > 3 else None),
                  (f'inpublisher:"{r["brand"].strip()}"' if r["brand"] and len(r["brand"].strip()) > 3 else None)):
            if q and q.lower() not in seen:
                seen.add(q.lower())
                out.append(q)
    return out


def seed_books(conn, gik, budget=None):
    budget = BOOKS_PER_RUN if budget is None else budget
    key = gik.google_books_key()
    if not key or budget <= 0:
        return
    done = {r[0] for r in conn.execute(
        "SELECT gtin FROM lookup_log WHERE source='gbooks-seed' AND requested_at > datetime('now', ?)", (f"-{REDO_DAYS} days",))}
    due = [q for q in load_book_queries() + scanned_book_queries(conn) if f"SEED:gbooks:{q.lower()}" not in done][:budget]
    now = datetime.now().isoformat(timespec="seconds")
    tot = 0
    for q in due:
        seen = new = 0
        st = 0
        for page in range(BOOK_PAGES):
            url = ("https://www.googleapis.com/books/v1/volumes?" + urllib.parse.urlencode(
                {"q": q, "printType": "books", "langRestrict": "en", "country": "US", "maxResults": 40,
                 "startIndex": page * 40, "key": key}))
            st, body = gik.http_get(url)
            if st != 200:
                break
            items = (json.loads(body) or {}).get("items") or []
            for v in items:
                ids = {i.get("type"): i.get("identifier") for i in (v.get("volumeInfo") or {}).get("industryIdentifiers") or []}
                isbn = re.sub(r"\D", "", ids.get("ISBN_13") or "")
                if len(isbn) != 13 or not isbn.startswith(("978", "979")):
                    continue
                gtin = "0" + isbn
                if not gik.normalize_gtin(gtin):
                    continue
                seen += 1
                if conn.execute("SELECT 1 FROM item_master WHERE gtin=?", (gtin,)).fetchone():
                    continue
                b = gik.parse_googlebooks({"items": [v]})
                if not b or not b.get("title"):
                    continue
                gik.upsert_book(conn, gtin, b, now, "gbooks-seed")
                note = (f"Catalog seed (books '{q}'): {b.get('merchant')}; not scanned yet; confirm"
                        if b.get("price") else f"Catalog seed (books '{q}'): title from Google Books, no price yet")
                conn.execute("UPDATE item_master SET notes=? WHERE gtin=?", (note, gtin))
                gik.ensure_links(conn, gtin)
                new += 1
            if len(items) < 40:
                break
            time.sleep(0.3)
        conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES (?,?,?,?)",
                     (f"SEED:gbooks:{q.lower()}", "gbooks-seed", st, json.dumps({"query": q, "books": seen, "new_items": new})))
        conn.commit()
        tot += new
        print(f"  books '{q}': {seen} books, {new} new catalog items")
    if due:
        print(f"Book seed: {len(due)} queries, {tot} new catalog books")


# ---------------------------------------------------------------- price fill for catalog items without a price

def fill_prices(conn, gik, budget):
    """Run the normal price chain (extra sources, Open Prices, eBay) on seeded items that still have no price.
    Each source keeps its own 7-day re-ask rule, so the same item is not hammered."""
    if budget <= 0:
        return
    rows = conn.execute("""SELECT gtin FROM item_master
                           WHERE fmv_unit IS NULL AND fmv_status <> 'verified' AND lookup_source IN ('kroger-seed','gbooks-seed')
                             AND gtin NOT IN (SELECT gtin FROM lookup_log WHERE source='ebay' AND requested_at > datetime('now','-7 days'))
                           ORDER BY updated_at LIMIT ?""", (budget,)).fetchall()
    now = datetime.now().isoformat(timespec="seconds")
    priced = 0
    for r in rows:
        try:
            if gik.maybe_ebay(conn, r["gtin"], now):
                priced += 1
                conn.execute("UPDATE item_master SET notes='Catalog seed: ' || COALESCE(notes,'') WHERE gtin=? AND COALESCE(notes,'') NOT LIKE 'Catalog seed%'",
                             (r["gtin"],))
        except Exception as e:
            print(f"  fill {r['gtin']}: {e}")
        conn.commit()
    if rows:
        print(f"Price fill: tried {len(rows)} unpriced catalog items, priced {priced}")


# ---------------------------------------------------------------- three places: Google Shopping via Serper

SERPER_PER_RUN = int(os.environ.get("GIK_SERPER_PER_RUN", "20"))   # 2 credits each; free tier is 2,500 credits
SERPER_URL = "https://google.serper.dev/shopping"
USED_WORDS = re.compile(r"\b(used|refurbished|renewed|pre-?owned|open[- ]box|like new)\b", re.I)


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2 and w not in ("the", "and", "for", "with")}


def same_item(name, title):
    """A shopping result counts only when most of our item's words are in its title (guards against look-alikes)."""
    a, b = _words(name.split(" - by ")[0]), _words(title)
    return bool(a) and len(a & b) / len(a) >= 0.6


def parse_price(text):
    m = re.search(r"\$\s?([\d,]+(?:\.\d{1,2})?)", text or "")
    return float(m.group(1).replace(",", "")) if m else None


def serper_offers(results, name):
    """Google Shopping results -> at most one new-condition offer per merchant that matches the item."""
    out, seen = [], set()
    for r in results or []:
        title, merchant, price = r.get("title") or "", (r.get("source") or "").strip(), parse_price(r.get("price"))
        if not merchant or not price or USED_WORDS.search(title) or not same_item(name, title) or merchant.lower() in seen:
            continue
        seen.add(merchant.lower())
        out.append({"merchant": merchant, "price": price, "link": r.get("link") or "", "condition": "New", "title": title,
                    "updated_t": int(time.time())})
    return out


def _serper_post(key, q):
    req = urllib.request.Request(SERPER_URL, data=json.dumps({"q": q, "gl": "us", "hl": "en"}).encode(),
                                 headers={"X-API-KEY": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, json.dumps({"error": str(e)})


def three_places(conn, gik, budget, post=None):
    """For the items worth the most (units scanned x value), find more merchants until each has three with proof.
    Adds offers only; the value itself changes only when a reviewer approves it."""
    key = os.environ.get("SERPER_API_KEY")
    if not key or budget <= 0:
        return
    post = post or _serper_post
    rows = conn.execute("""SELECT im.gtin, im.name, im.brand, COALESCE(SUM(r.qty), 0) * COALESCE(im.fmv_unit, 0) AS impact
                           FROM item_master im LEFT JOIN receipts r ON r.gtin = im.gtin
                           WHERE im.name IS NOT NULL AND im.gtin NOT LIKE 'G-%' AND im.fmv_status <> 'verified'
                             AND im.gtin NOT IN (SELECT gtin FROM lookup_log WHERE source='serper'
                                                 AND requested_at > datetime('now','-60 days'))
                             AND (SELECT COUNT(DISTINCT lower(o.merchant)) FROM offers o WHERE o.gtin = im.gtin AND o.usable = 1
                                  AND o.url IS NOT NULL AND o.url <> '') < 3
                           GROUP BY im.gtin ORDER BY impact DESC, COALESCE(im.fmv_unit, 0) DESC LIMIT ?""", (budget,)).fetchall()
    added = asked = 0
    for r in rows:
        name = r["name"]
        q = name if not r["brand"] or r["brand"].lower() in name.lower() else f"{r['brand']} {name}"
        st, body = post(key, q.split(" - by ")[0][:120])
        asked += 1
        conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES (?,?,?,?)",
                     (r["gtin"], "serper", st, (body or "")[:20000]))
        conn.commit()
        if st in (401, 402, 403, 429):  # bad key, out of credits or throttled: stop for this run, try next run
            print(f"Three places: Serper answered {st}; stopping for this run")
            break
        if st != 200:
            continue
        offers = serper_offers((json.loads(body) or {}).get("shopping"), name)
        gik.record_offers(conn, r["gtin"], offers, "serper")
        conn.commit()
        added += len(offers)
    if asked:
        print(f"Three places: searched {asked} high-value items, added {added} merchant prices")
