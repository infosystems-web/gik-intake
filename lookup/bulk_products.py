"""Reference catalog import: load known barcodes in bulk BEFORE anything is scanned, so a scan is named at once.

Sources (all free):
  openbeautyfacts   Open Beauty Facts CSV export: soap, shampoo, toothpaste, deodorant, lotion ...      (ODbL 1.0)
  openproductsfacts Open Products Facts CSV export: household, school, toys, electronics ...          (ODbL 1.0)
  openfoodfacts     Open Food Facts CSV export, US only, classroom-snack categories only              (ODbL 1.0)
  openlibrary       Open Library search API by subject: children's, young adult, education books (CC0)

Writes gik.products (names only; values still come from retail evidence) and one gik.import_runs row per source,
then gives scanned barcodes that still have no name the reference name (gik.apply_reference_names()).

  python bulk_products.py all            weekly job: every source
  python bulk_products.py openbeautyfacts openlibrary
  python bulk_products.py apply          only fill names of scanned items from the reference table
  python bulk_products.py <source> --dry-run --limit 5000   parse and count, write nothing
"""
import csv
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gik  # noqa: E402  (normalize_gtin, map_category, cloud_connect)

UA = "TEP-GIK-Intake/0.4 (The Education Partnership nonprofit, Pittsburgh; GIK catalog)"
ODBL = "ODbL 1.0 (Open Food Facts contributors)"
CSV_SOURCES = {
    "openbeautyfacts": "https://static.openbeautyfacts.org/data/en.openbeautyfacts.org.products.csv.gz",
    "openproductsfacts": "https://static.openproductsfacts.org/data/en.openproductsfacts.org.products.csv.gz",
    "openfoodfacts": "https://static.openfoodfacts.org/data/en.openfoodfacts.org.products.csv.gz",
}
PRODUCT_PAGE = {"openbeautyfacts": "https://world.openbeautyfacts.org/product/{code}",
                "openproductsfacts": "https://world.openproductsfacts.org/product/{code}",
                "openfoodfacts": "https://world.openfoodfacts.org/product/{code}"}
# Food: only what classrooms receive (snacks, drinks, breakfast), not the whole grocery store.
FOOD_KEEP = re.compile(r"en:(snacks|sweet-snacks|salty-snacks|crackers|cereal-bars|granola-bars|fruit-snacks|"
                       r"breakfast-cereals|beverages|juices|fruit-juices|waters|popcorn|pretzels|cookies|biscuits)\b")
US = re.compile(r"en:united-states")
BEAUTY_HEALTH = re.compile(r"sunscreen|sanitizer|first aid|bandage|antiseptic|thermometer|medicine", re.I)
OL_SUBJECTS_FILE = os.path.join(HERE, "catalog_openlibrary.txt")
OL_PAGES = int(os.environ.get("GIK_OL_PAGES", "30"))     # pages of 100 works per subject
OL_LIMIT = 100
FLUSH = 20000


def gtin14(code):
    """Barcode text -> 14-digit GTIN with a valid check digit, else None (bad scans and store codes are dropped)."""
    d = re.sub(r"\D", "", code or "")
    if len(d) not in (8, 12, 13, 14):
        return None
    g = gik.normalize_gtin(d)
    if not g or g.startswith("0000000") or g[1:4] in ("020", "021", "022", "023", "024", "025", "026", "027", "028", "029"):
        return None  # empty or in-store/random-weight codes
    return g


def is_us(row, code):
    if US.search(row.get("countries_tags") or ""):
        return True
    d = re.sub(r"\D", "", code or "")
    return len(d) == 12  # UPC-A: US/Canada barcode


def category_for(source, row):
    text = " ".join([row.get("categories_en") or "", row.get("categories_tags") or "", row.get("product_name") or ""])
    if source == "openbeautyfacts":
        return "Health & personal care" if BEAUTY_HEALTH.search(text) else "Apparel & hygiene"
    if source == "openfoodfacts":
        return "Food & beverage"
    return gik.map_category(text)


def parse_csv(source, lines):
    """Yield (gtin, name, brand, quantity, category, raw_categories, image_url, source_url) from an Open*Facts
    tab-separated export. Generator over a text stream, so a 9 GB file is never held in memory."""
    csv.field_size_limit(10 ** 9)
    reader = csv.reader(lines, delimiter="\t", quoting=csv.QUOTE_NONE)
    header = next(reader)
    idx = {h: i for i, h in enumerate(header)}
    need = ("code", "product_name")
    if any(k not in idx for k in need):
        raise ValueError(f"{source}: export header changed, missing {[k for k in need if k not in idx]}")
    stats = {"read": 0, "kept": 0}
    for cols in reader:
        stats["read"] += 1
        row = {k: (cols[i] if i < len(cols) else "") for k, i in idx.items()}
        code = row.get("code")
        g = gtin14(code)
        name = (row.get("product_name") or "").strip()
        if not g or not name or len(name) < 2:
            continue
        if source == "openfoodfacts":
            if not US.search(row.get("countries_tags") or "") or not FOOD_KEEP.search(row.get("categories_tags") or ""):
                continue
        elif not is_us(row, code):
            continue
        brand = (row.get("brands") or "").split(",")[0].strip() or None
        stats["kept"] += 1
        yield (g, name[:300], brand and brand[:120], (row.get("quantity") or "").strip()[:60] or None,
               category_for(source, row), (row.get("categories_en") or "")[:300] or None,
               (row.get("image_url") or "").strip() or None, PRODUCT_PAGE[source].format(code=re.sub(r"\D", "", code)))
    parse_csv.stats = stats


def open_stream(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    resp = urllib.request.urlopen(req, timeout=120)
    return io.TextIOWrapper(gzip.GzipFile(fileobj=resp), encoding="utf-8", errors="replace", newline="")


# ---------------------------------------------------------------- Open Library (search API by subject)

def load_ol_subjects(path=OL_SUBJECTS_FILE):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]


def parse_ol_docs(docs, subject):
    """Open Library search docs -> product rows, one per ISBN-13 (every edition of a work)."""
    for d in docs:
        title = (d.get("title") or "").strip()
        if not title:
            continue
        author = ", ".join((d.get("author_name") or [])[:2])
        name = f"{title} - by {author}" if author else title
        publisher = ((d.get("publisher") or [None])[0] or None)
        seen = set()
        for isbn in d.get("isbn") or []:
            isbn = re.sub(r"\D", "", isbn)
            if len(isbn) != 13 or not isbn.startswith(("978", "979")) or isbn in seen:
                continue
            seen.add(isbn)
            g = gik.normalize_gtin("0" + isbn)
            if g:
                yield (g, name[:300], publisher and publisher[:120], None, "Books & media", f"Books > {subject}"[:300], None,
                       f"https://openlibrary.org{d.get('key', '')}")


def ol_rows(subjects, pages=OL_PAGES, get=None):
    get = get or (lambda url: json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                                                timeout=60).read().decode("utf-8")))
    stats = {"read": 0, "kept": 0}
    for subject in subjects:
        for page in range(1, pages + 1):
            url = "https://openlibrary.org/search.json?" + urllib.parse.urlencode(
                {"subject": subject, "language": "eng", "fields": "key,title,author_name,publisher,isbn",
                 "limit": OL_LIMIT, "page": page})
            for attempt in range(3):
                try:
                    data = get(url)
                    break
                except Exception as e:  # Open Library is a volunteer-run service: back off and retry, then move on
                    print(f"  openlibrary '{subject}' p{page}: {e}; retry {attempt + 1}")
                    time.sleep(10 * (attempt + 1))
            else:
                data = {"docs": []}
            docs = data.get("docs") or []
            stats["read"] += len(docs)
            for r in parse_ol_docs(docs, subject):
                stats["kept"] += 1
                yield r
            time.sleep(1.0)  # be polite: about one request a second
            if len(docs) < OL_LIMIT:
                break
    ol_rows.stats = stats


# ---------------------------------------------------------------- write

COLS = ("gtin", "name", "brand", "quantity", "category_hint", "raw_categories", "image_url", "source_url")


def write_rows(pg, source, license_, rows):
    """COPY rows into a temp table in batches, then upsert. Same-source rows refresh; a row from another source
    only fills blanks, so the first good name wins. Returns rows written."""
    written = 0
    batch = {}
    with pg.cursor() as cur:
        cur.execute("create temp table if not exists tmp_products (gtin text, name text, brand text, quantity text, "
                    "category_hint text, raw_categories text, image_url text, source_url text) on commit preserve rows")

        def flush():
            nonlocal written
            if not batch:
                return
            buf = io.StringIO()
            for r in batch.values():
                buf.write("\t".join("\\N" if v is None else str(v).replace("\\", "\\\\").replace("\t", " ").replace("\n", " ")
                                    .replace("\r", " ") for v in r) + "\n")
            buf.seek(0)
            cur.execute("truncate tmp_products")
            cur.copy_expert(f"copy tmp_products ({','.join(COLS)}) from stdin", buf)
            cur.execute("""insert into gik.products (gtin, name, brand, quantity, category_hint, raw_categories, image_url,
                                                     source, source_url, license)
                           select t.gtin, t.name, t.brand, t.quantity,
                                  case when t.category_hint in (select category from gik.gl_map) then t.category_hint end,
                                  t.raw_categories, t.image_url, %s, t.source_url, %s from tmp_products t
                           on conflict (gtin) do update set
                             name = case when gik.products.source = excluded.source then excluded.name else gik.products.name end,
                             brand = coalesce(case when gik.products.source = excluded.source then excluded.brand end, gik.products.brand),
                             quantity = coalesce(gik.products.quantity, excluded.quantity),
                             category_hint = coalesce(gik.products.category_hint, excluded.category_hint),
                             raw_categories = coalesce(gik.products.raw_categories, excluded.raw_categories),
                             image_url = coalesce(gik.products.image_url, excluded.image_url),
                             updated_at = now()""", (source, license_))
            written += cur.rowcount
            pg.commit()
            batch.clear()
            print(f"  {source}: {written:,} written so far")

        for r in rows:
            batch[r[0]] = r  # last row for a barcode in this batch wins (no duplicate-key errors in one statement)
            if len(batch) >= FLUSH:
                flush()
        flush()
    return written


def run_source(pg, source, dry_run=False, limit=None):
    run_id = None
    if not dry_run:
        with pg.cursor() as cur:
            cur.execute("insert into gik.import_runs (source) values (%s) returning run_id", (source,))
            run_id = cur.fetchone()[0]
        pg.commit()
    try:
        if source == "openlibrary":
            gen, lic, statobj = ol_rows(load_ol_subjects()), "CC0 (Open Library)", ol_rows
        else:
            gen, lic, statobj = parse_csv(source, open_stream(CSV_SOURCES[source])), ODBL, parse_csv
        if limit:
            gen = (r for i, r in zip(range(limit), gen))
        if dry_run:
            n = sum(1 for _ in gen)
            print(f"{source}: dry run, {n:,} rows would be written; stats {getattr(statobj, 'stats', {})}")
            return n
        n = write_rows(pg, source, lic, gen)
        st = getattr(statobj, "stats", {})
        with pg.cursor() as cur:
            cur.execute("update gik.import_runs set finished_at = now(), status = 'ok', rows_read = %s, rows_kept = %s, "
                        "rows_written = %s where run_id = %s", (st.get("read", 0), st.get("kept", 0), n, run_id))
        pg.commit()
        print(f"{source}: read {st.get('read', 0):,}, kept {st.get('kept', 0):,}, written {n:,}")
        return n
    except Exception as e:
        pg.rollback()
        if run_id:
            with pg.cursor() as cur:
                cur.execute("update gik.import_runs set finished_at = now(), status = 'failed', detail = %s where run_id = %s",
                            (str(e)[:2000], run_id))
            pg.commit()
        raise


def apply_names(pg):
    with pg.cursor() as cur:
        cur.execute("select gik.apply_reference_names()")
        n = cur.fetchone()[0]
    pg.commit()
    print(f"Named {n} scanned barcodes from the reference catalog")
    return n


def main(argv):
    dry = "--dry-run" in argv
    limit = None
    if "--limit" in argv:
        limit = int(argv[argv.index("--limit") + 1])
    names = [a for a in argv if not a.startswith("--") and not a.isdigit()]
    if not names:
        print(__doc__)
        return 2
    pg = None if (dry and names != ["apply"]) else gik.cloud_connect()
    if names == ["apply"]:
        apply_names(pg)
        return 0
    sources = list(CSV_SOURCES) + ["openlibrary"] if names == ["all"] else names
    failed = []
    for s in sources:
        if s not in CSV_SOURCES and s != "openlibrary":
            print(f"unknown source {s}")
            failed.append(s)
            continue
        try:
            run_source(pg, s, dry, limit)
        except Exception as e:  # one source failing must not stop the others; the job still ends red
            print(f"!! {s} failed: {e}")
            failed.append(s)
    if pg is not None:
        apply_names(pg)
    if failed:
        print(f"Failed sources: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
