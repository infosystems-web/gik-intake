"""Open Library records with odd field shapes must not stop the dump load (run #4 failed on 11/23 with
"'str' object has no attribute 'get'": a work whose author was "/authors/OL1A" instead of {"key": ...})."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gik  # noqa: E402
import ol_dump  # noqa: E402


def line(kind, key, rec):
    return "\t".join([kind, key, "1", "2026-11-01", json.dumps(rec)])


def run(works, authors, editions):
    dumps = {"works": works, "authors": authors, "editions": editions}
    opener = lambda url: next(v for k, v in dumps.items() if ol_dump.DUMPS[k] == url)
    return list(ol_dump.rows(gik, opener=opener)), ol_dump.rows.stats


def test_string_author_and_text_fields():
    works = [
        line("/type/work", "/works/OL1W", {"key": "/works/OL1W", "subjects": ["Juvenile fiction"],
                                           "authors": [{"author": "/authors/OL1A"}]}),            # string author (old format)
        line("/type/work", "/works/OL2W", {"key": "/works/OL2W", "subjects": [{"name": "Picture books"}],
                                           "authors": ["/authors/OL2A", None, 7]}),               # bare strings, junk
        line("/type/work", "/works/OL3W", {"key": "/works/OL3W", "subjects": ["Juvenile fiction"],
                                           "authors": [{"author": {"key": "/authors/OL1A"}}]}),  # normal
    ]
    authors = [line("/type/author", "/authors/OL1A", {"name": "Ann Author"}),
               line("/type/author", "/authors/OL2A", {"name": {"type": "/type/text", "value": "Bea Writer"}})]
    editions = [
        line("/type/edition", "/books/OL1M", {"key": "/books/OL1M", "works": [{"key": "/works/OL1W"}],
             "title": {"type": "/type/text", "value": "Odd Title"}, "isbn_13": ["9780000000002", 12345],
             "publishers": [{"name": "x"}], "physical_format": None, "number_of_pages": "32"}),
        line("/type/edition", "/books/OL2M", {"key": "/books/OL2M", "works": ["/works/OL2W"], "title": "Second",
             "isbn_10": ["0306406152"], "languages": ["/languages/eng"], "by_statement": {"value": "by B."}}),
        line("/type/edition", "/books/OL3M", {"key": "/books/OL3M", "works": [{"key": "/works/OL3W"}], "title": "Third",
             "isbn_13": ["9780000000019"], "physical_format": "Board book", "number_of_pages": 24,
             "publishers": ["Pub Co"], "languages": [{"key": "/languages/eng"}]}),
    ]
    rows, stats = run(works, authors, editions)
    names = {r[0]: r[1] for r in rows}
    assert stats["works_kept"] == 3
    assert names["09780000000002"] == "Odd Title - by Ann Author"
    assert names["09780306406157"] == "Second - by Bea Writer"
    third = next(r for r in rows if r[0] == "09780000000019")
    assert third[1] == "Third - by Ann Author" and third[2] == "Pub Co" and third[3] == "24 pages, Board book"
