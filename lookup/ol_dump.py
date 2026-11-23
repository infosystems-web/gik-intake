"""Open Library full data dump -> gik.products: every children's, teen, classroom and teacher book Open Library knows
that has an ISBN, so a book scan is named, sized (page count) and sorted the moment it lands. Free, CC0.

Three streamed passes over Open Library's monthly dumps (nothing is saved to disk, nothing is held but small sets):
  1. works dump    (~3 GB)  keep works whose subjects say juvenile / children's / young adult / education / readers ...
  2. authors dump  (~1 GB)  names for the authors of the works kept
  3. editions dump (~10 GB) every edition of a kept work (or whose own subjects match), English or Spanish or unstated,
                            with an ISBN -> one row per ISBN-13: "Title - by Author", publisher, "NN pages", subjects,
                            cover image, Open Library page.
Run by the "GIK reference catalog" workflow: `python bulk_products.py openlibrary-dump` (about 1-2 hours on a runner).
In the weekly `all` run it is skipped when it already ran in the last 28 days (the dump is monthly).
A database-size guard stops the load before it crowds the shared Supabase project (GIK_DB_MAX_GB, default 6).
"""
import gzip
import io
import json
import os
import re
import urllib.request

DUMPS = {t: f"https://openlibrary.org/data/ol_dump_{t}_latest.txt.gz" for t in ("works", "authors", "editions")}
UA = "TEP-GIK-Intake/0.5 (The Education Partnership nonprofit, Pittsburgh; GIK catalog)"
LICENSE = "CC0 (Open Library)"
# Subjects that mark a book TEP's partners would hand to a child, a teen or a teacher.
KID = re.compile(r"juvenile|children'?s|for children|young adult|\bteens?\b|picture books?|board books?|easy reading|"
                 r"readers \(|beginning reader|early reader|chapter books?|stories in rhyme|nursery rhymes|fairy tales|"
                 r"\beducation\b|study and teaching|\bteaching\b|textbooks?|workbooks?|activity books?|coloring books?|"
                 r"phonics|reading \(|spanish language materials|bilingual|school stories|"
                 r"preschool|kindergarten|elementary school|\(elementary\)|\(primary\)|middle school|high school|"
                 r"graphic novels?", re.I)
# ...but not college and university material, which TEP's classroom partners do not use.
ADULT = re.compile(r"\(higher\)|higher education|universit|graduate|college", re.I)
LANGS = {"/languages/eng", "/languages/spa"}
MAX_SUBJECTS = 6
REDO_DAYS = 28


def open_dump(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    resp = urllib.request.urlopen(req, timeout=300)
    return io.TextIOWrapper(gzip.GzipFile(fileobj=resp), encoding="utf-8", errors="replace", newline="\n")


def _record(line):
    """One dump line: type, key, revision, last_modified, JSON -> dict (or None)."""
    parts = line.rstrip("\n").split("\t", 4)
    if len(parts) < 5:
        return None
    try:
        rec = json.loads(parts[4])
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _txt(v):
    """Text from a field that Open Library stores as a string or as {"type": "/type/text", "value": ...}."""
    if isinstance(v, dict):
        v = v.get("value")
    return v.strip() if isinstance(v, str) else ""


def _key(v):
    """A key from "/authors/OL1A", {"key": ...} or {"author": {"key": ...}} / {"author": "/authors/OL1A"}."""
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, dict):
        return _key(v.get("key")) if "key" in v else _key(v.get("author"))
    return None


def _subjects(rec):
    out = []
    for s in rec.get("subjects") or []:
        s = _txt(s.get("name") if isinstance(s, dict) else s)
        if s:
            out.append(s)
    return out


def kid_subjects(subjects):
    if any(ADULT.search(s) for s in subjects):
        return []
    return [s for s in subjects if KID.search(s)]


def isbn13_from_10(isbn10):
    d = re.sub(r"[^0-9Xx]", "", isbn10 or "")
    if len(d) != 10:
        return None
    body = "978" + d[:9]
    total = sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(body))
    return body + str((10 - total % 10) % 10)


def edition_isbns(rec):
    out = []
    for i in rec.get("isbn_13") or []:
        d = re.sub(r"\D", "", i if isinstance(i, str) else "")
        if len(d) == 13 and d.startswith(("978", "979")):
            out.append(d)
    for i in rec.get("isbn_10") or []:
        d = isbn13_from_10(i if isinstance(i, str) else "")
        if d:
            out.append(d)
    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def scan_works(lines, stats):
    """Pass 1: {work key: (subjects text, [author keys])} for children's / teen / classroom works."""
    works = {}
    for line in lines:
        stats["works_read"] += 1
        if "subjects" not in line or not KID.search(line):   # cheap test first: most works never get parsed
            continue
        rec = _record(line)
        if not rec:
            continue
        try:
            subs = kid_subjects(_subjects(rec))
            if not subs:
                continue
            authors = [_key(a) for a in rec.get("authors") or []]
            works[_key(rec.get("key"))] = (" / ".join(subs[:MAX_SUBJECTS]), [a for a in authors if a][:2])
        except Exception:  # one odd record must never stop the load
            stats["bad_records"] = stats.get("bad_records", 0) + 1
    stats["works_kept"] = len(works)
    return works


def scan_authors(lines, wanted, stats):
    """Pass 2: names for the author keys the kept works point to."""
    names = {}
    for line in lines:
        key = line.split("\t", 2)[1] if line.count("\t") >= 2 else ""
        if key not in wanted:
            continue
        rec = _record(line)
        if rec and _txt(rec.get("name")):
            names[key] = _txt(rec.get("name"))
    stats["authors_named"] = len(names)
    return names


def edition_rows(lines, works, authors, stats, gik):
    """Pass 3: one product row per ISBN-13 of every kept edition."""
    for line in lines:
        stats["editions_read"] += 1
        if "isbn_1" not in line:
            continue
        rec = _record(line)
        if not rec:
            continue
        try:
            row = _edition(rec, works, authors)
        except Exception:  # one odd record must never stop the load
            stats["bad_records"] = stats.get("bad_records", 0) + 1
            continue
        if not row:
            continue
        name, pub, qty, subjects, image, url, isbns = row
        for isbn in isbns:
            g = gik.normalize_gtin("0" + isbn)
            if not g:
                continue
            stats["kept"] += 1
            yield (g, name[:300], pub and pub[:120], qty, "Books & media", f"Books > {subjects}"[:300], image, url)
    stats["read"] = stats["editions_read"]


def _edition(rec, works, authors):
    """One edition record -> (name, publisher, quantity, subjects, image, url, isbns), or None to skip it."""
    langs = {_key(l) for l in rec.get("languages") or []}
    langs.discard(None)
    if langs and not langs & LANGS:
        return None
    work_keys = [_key(w) for w in rec.get("works") or []]
    work = next((works[k] for k in work_keys if k in works), None)
    own = kid_subjects(_subjects(rec))
    if not work and not own:
        return None
    isbns = edition_isbns(rec)
    title = _txt(rec.get("title"))
    if not isbns or not title:
        return None
    subtitle = _txt(rec.get("subtitle"))
    if subtitle and len(title) + len(subtitle) < 120:
        title = f"{title}: {subtitle}"
    akeys = [k for k in (_key(a) for a in rec.get("authors") or []) if k] or (work[1] if work else [])
    author = ", ".join(authors[k] for k in akeys if k in authors) or _txt(rec.get("by_statement")).rstrip(".")
    name = f"{title} - by {author}" if author else title
    pub = _txt((rec.get("publishers") or [None])[0]) or None
    pages = rec.get("number_of_pages")
    fmt = _txt(rec.get("physical_format"))   # Paperback, Hardcover, Board book ... (sets the used-book value)
    qty = ", ".join(x for x in (f"{pages} pages" if isinstance(pages, int) and 0 < pages < 5000 else "", fmt[:30]) if x) or None
    subjects = work[0] if work else " / ".join(own[:MAX_SUBJECTS])
    covers = [c for c in rec.get("covers") or [] if isinstance(c, int) and c > 0]
    image = f"https://covers.openlibrary.org/b/id/{covers[0]}-M.jpg" if covers else None
    url = f"https://openlibrary.org{_key(rec.get('key')) or ''}"
    return name, pub, qty, subjects, image, url, isbns


def rows(gik, opener=open_dump):
    """All three passes; a generator of product rows. Stats on rows.stats as it goes."""
    stats = {"works_read": 0, "works_kept": 0, "authors_named": 0, "editions_read": 0, "read": 0, "kept": 0}
    rows.stats = stats
    works = scan_works(opener(DUMPS["works"]), stats)
    print(f"  openlibrary-dump: {stats['works_kept']:,} children's/teen/classroom works of {stats['works_read']:,}")
    wanted = {a for _, al in works.values() for a in al}
    authors = scan_authors(opener(DUMPS["authors"]), wanted, stats)
    print(f"  openlibrary-dump: {stats['authors_named']:,} author names")
    yield from edition_rows(opener(DUMPS["editions"]), works, authors, stats, gik)


def ran_recently(pg, days=REDO_DAYS):
    with pg.cursor() as cur:
        cur.execute("select 1 from gik.import_runs where source = 'openlibrary-dump' and status = 'ok' "
                    "and finished_at > now() - make_interval(days => %s) limit 1", (days,))
        return cur.fetchone() is not None


def db_size_gb(pg):
    with pg.cursor() as cur:
        cur.execute("select pg_database_size(current_database())")
        return cur.fetchone()[0] / 1e9


def guarded(gen, pg, every=200000, max_gb=None):
    """Pass rows through, checking the database size now and then; stop cleanly before it gets too big."""
    max_gb = max_gb if max_gb is not None else float(os.environ.get("GIK_DB_MAX_GB", "6"))
    for i, r in enumerate(gen):
        if i % every == 0:
            size = db_size_gb(pg)
            if size > max_gb:
                print(f"  openlibrary-dump: stopping at {i:,} rows, database is {size:.1f} GB (limit {max_gb} GB, GIK_DB_MAX_GB)")
                return
        yield r
