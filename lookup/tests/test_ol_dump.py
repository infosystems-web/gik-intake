"""Open Library full-dump loader (ol_dump.py). No network: the three dumps are a few fake lines each."""
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("GIK_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import gik  # noqa: E402
import ol_dump  # noqa: E402
import bulk_products as bp  # noqa: E402


def _line(kind, key, rec):
    rec = dict(rec, key=key)
    return f"/type/{kind}\t{key}\t3\t2026-10-01T00:00:00\t{json.dumps(rec)}\n"


WORKS = [
    _line("work", "/works/OL1W", {"title": "The Very Hungry Caterpillar", "subjects": ["Juvenile fiction", "Caterpillars"],
                                   "authors": [{"author": {"key": "/authors/OL1A"}, "type": {"key": "/type/author_role"}}]}),
    _line("work", "/works/OL2W", {"title": "Romance", "subjects": ["Fiction", "Love stories"],
                                   "authors": [{"author": {"key": "/authors/OL2A"}}]}),
    _line("work", "/works/OL3W", {"title": "Calculus", "subjects": ["Mathematics", "Study and teaching (Higher)"]}),
    _line("work", "/works/OL4W", {"title": "History", "subjects": ["Seventeenth century", "History"]}),
]
AUTHORS = [_line("author", "/authors/OL1A", {"name": "Eric Carle"}), _line("author", "/authors/OL2A", {"name": "Someone"})]
EDITIONS = [
    _line("edition", "/books/OL1M", {"title": "The Very Hungry Caterpillar", "works": [{"key": "/works/OL1W"}],
                                     "isbn_13": ["978-0-399-22690-8"], "isbn_10": ["0399226907"], "publishers": ["Philomel"],
                                     "number_of_pages": 26, "languages": [{"key": "/languages/eng"}], "covers": [12345]}),
    _line("edition", "/books/OL2M", {"title": "La oruga muy hambrienta", "works": [{"key": "/works/OL1W"}],
                                     "isbn_10": ["0399227202"], "languages": [{"key": "/languages/spa"}]}),
    _line("edition", "/books/OL3M", {"title": "Die kleine Raupe", "works": [{"key": "/works/OL1W"}],
                                     "isbn_13": ["9783806741100"], "languages": [{"key": "/languages/ger"}]}),
    _line("edition", "/books/OL4M", {"title": "Romance", "works": [{"key": "/works/OL2W"}], "isbn_13": ["9780373298471"]}),
    _line("edition", "/books/OL5M", {"title": "Math Workbook Grade 2", "subjects": ["Mathematics, Workbooks"],
                                     "isbn_13": ["9781483813424"], "by_statement": "Carson Dellosa."}),
    _line("edition", "/books/OL6M", {"title": "No ISBN", "works": [{"key": "/works/OL1W"}]}),
    "broken line\n",
]


def _opener(url):
    data = {"works": WORKS, "authors": AUTHORS, "editions": EDITIONS}
    return io.StringIO("".join(next(v for k, v in data.items() if f"_{k}_" in url)))


def test_rows_keep_children_classroom_books_only():
    rows = list(ol_dump.rows(gik, opener=_opener))
    by = {r[0]: r for r in rows}
    assert set(by) == {"09780399226908", "09780399227202", "09781483813424"}   # adult, German, college, no-ISBN skipped
    g = by["09780399226908"]
    assert g[1] == "The Very Hungry Caterpillar - by Eric Carle" and g[2] == "Philomel" and g[3] == "26 pages"
    assert g[4] == "Books & media" and g[5] == "Books > Juvenile fiction" and g[6].endswith("/b/id/12345-M.jpg")
    assert g[7] == "https://openlibrary.org/books/OL1M"
    assert by["09781483813424"][1] == "Math Workbook Grade 2 - by Carson Dellosa"   # own subjects, by-statement
    assert ol_dump.rows.stats["works_kept"] == 1 and ol_dump.rows.stats["kept"] == 3


def test_isbn10_converts_and_dedupes():
    assert ol_dump.isbn13_from_10("0-399-22690-7") == "9780399226908"
    assert ol_dump.edition_isbns({"isbn_13": ["9780399226908"], "isbn_10": ["0399226907"]}) == ["9780399226908"]
    assert ol_dump.isbn13_from_10("123") is None


def test_subject_filter_skips_college_and_false_teens():
    assert ol_dump.kid_subjects(["Juvenile fiction", "Dogs"]) == ["Juvenile fiction"]
    assert ol_dump.kid_subjects(["Seventeenth century"]) == []
    assert ol_dump.kid_subjects(["Education", "Study and teaching (Higher)"]) == []
    assert ol_dump.kid_subjects(["Readers (Primary)"]) == ["Readers (Primary)"]


def test_guard_stops_before_the_database_gets_too_big(monkeypatch):
    sizes = iter([1.0, 9.0])
    monkeypatch.setattr(ol_dump, "db_size_gb", lambda pg: next(sizes))
    out = list(ol_dump.guarded(iter(range(10)), None, every=5, max_gb=6))
    assert out == [0, 1, 2, 3, 4]


def test_dry_run_counts_without_a_database(monkeypatch, capsys):
    monkeypatch.setattr(ol_dump, "open_dump", _opener)
    monkeypatch.setattr(ol_dump.rows, "__defaults__", (_opener,))
    assert bp.run_source(None, "openlibrary-dump", dry_run=True) == 3
