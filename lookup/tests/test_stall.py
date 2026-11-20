"""11/20/2026: the catalog stopped growing on 11/13 and two sources failed for days while every run showed green.
These tests keep it from happening silently again. No network: every outside service is a fake answer.

    cd lookup && python -m pytest -q tests
"""
import json
import os
import sys
import tempfile
from datetime import datetime

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("GIK_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import gik  # noqa: E402
import catalog_seed as cs  # noqa: E402
import extra_sources as es  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(gik, "DB_PATH", str(tmp_path / "gik.db"))
    gik.cmd_init(None)
    c = gik.connect()
    yield c
    c.close()


def _log(conn, gtin, source, status, when="now"):
    conn.execute("INSERT INTO lookup_log (gtin, source, http_status, requested_at) VALUES (?,?,?,datetime(?))",
                 (gtin, source, status, when))


def _isbn(prefix, i):
    base = f"978{prefix}{i:08d}"
    t = sum(int(c) * (1 if k % 2 == 0 else 3) for k, c in enumerate(base))
    return base + str((10 - t % 10) % 10)


def test_book_queries_keep_paging_instead_of_stopping(conn):
    _log(conn, "SEED:gbooks:old", "gbooks-seed", 200, "2026-11-13 10:00:00")    # before the fix: read further
    _log(conn, "SEED:gbooks:more", "gbooks-seed", 206)                          # more results remain
    _log(conn, "SEED:gbooks:done", "gbooks-seed", 204)                          # used up today: wait
    _log(conn, "SEED:gbooks:stale", "gbooks-seed", 204, "2026-09-01 10:00:00")  # used up 30+ days ago: start over
    _log(conn, "SEED:gbooks:deep", "gbooks-seed", 206)
    _log(conn, "SEED:gbooks:deep#from=800", "gbooks-seed", 206)                 # next start would pass the cap
    conn.commit()
    due = cs.due_book_queries(conn, ["new", "old", "more", "done", "deep", "stale"])
    assert due == [("new", 0), ("old", 200), ("more", 200), ("stale", 0)]


def test_book_seed_marks_progress_and_filters_learned_queries(conn, monkeypatch):
    monkeypatch.setattr(gik, "google_books_key", lambda: "k")
    monkeypatch.setattr(cs, "load_book_queries", lambda path=None: ["kids"])
    monkeypatch.setattr(cs, "scanned_book_queries", lambda c: ['inauthor:"lane"'])
    monkeypatch.setattr(cs, "BOOK_PAGES", 1)
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)

    def http_get(url, headers=None):
        kids = "kids" in url
        cat = "Juvenile Fiction" if kids else "Fiction / Romance"
        items = [{"volumeInfo": {"title": f"T{i}", "categories": [cat],
                                 "industryIdentifiers": [{"type": "ISBN_13", "identifier": _isbn(1 if kids else 2, i)}]}}
                 for i in range(40)]
        return 200, json.dumps({"items": items})
    monkeypatch.setattr(gik, "http_get", http_get)
    cs.seed_books(conn, gik, budget=5)
    codes = {r[0]: r[1] for r in conn.execute("SELECT gtin, http_status FROM lookup_log WHERE source='gbooks-seed'")}
    assert codes == {"SEED:gbooks:kids": 206, 'SEED:gbooks:inauthor:"lane"': 206}
    assert conn.execute("SELECT COUNT(*) FROM item_master WHERE gtin LIKE '09781%'").fetchone()[0] == 40   # kept
    assert conn.execute("SELECT COUNT(*) FROM item_master WHERE gtin LIKE '09782%'").fetchone()[0] == 0    # adult: skipped
    assert cs.due_book_queries(conn, ["kids"]) == [("kids", 40)]                                             # continues


def test_adult_books_are_not_learned_from(conn):
    conn.execute("INSERT INTO donors (name, type) VALUES ('d', 'other')")
    conn.execute("INSERT INTO donations (donation_id, donor_id, received_date) VALUES ('D1', 1, '2026-11-01')")
    conn.execute("INSERT INTO item_master (gtin, name, brand, raw_category) VALUES "
                 "('09780373298471','Romance - by Elizabeth Lane','Harlequin','Books > Fiction / Romance')")
    conn.execute("INSERT INTO receipts (donation_id, gtin, qty) VALUES ('D1','09780373298471',1)")
    conn.commit()
    assert cs.scanned_book_queries(conn) == []


def test_kroger_refreshes_old_terms_instead_of_sitting_idle(conn):
    _log(conn, "SEED:kroger:tissues", "kroger-seed", 200, "2026-10-01 10:00:00")
    _log(conn, "SEED:kroger:crayons", "kroger-seed", 200)
    conn.commit()
    terms = [("Household & lighting", "tissues"), ("School supplies", "crayons")]
    assert cs._refresh_terms(conn, terms, 5) == [("Household & lighting", "tissues")]   # run this week: left alone


def test_extra_term_files_are_read(tmp_path, monkeypatch):
    (tmp_path / "catalog_terms.txt").write_text("School supplies | crayons\n")
    (tmp_path / "catalog_terms_more.txt").write_text("School supplies | Crayons\nTechnology | webcam\n")
    monkeypatch.setattr(cs, "HERE", str(tmp_path))
    monkeypatch.setattr(cs, "TERMS_FILE", str(tmp_path / "catalog_terms.txt"))
    assert cs.load_terms(str(tmp_path / "catalog_terms.txt")) == [("School supplies", "crayons"), ("Technology", "webcam")]


def test_kroger_and_bestbuy_are_never_asked_about_books(monkeypatch):
    monkeypatch.setenv("KROGER_CLIENT_ID", "x")
    monkeypatch.setenv("KROGER_CLIENT_SECRET", "y")
    monkeypatch.setenv("BESTBUY_API_KEY", "z")
    called = []
    assert es.fetch_kroger("09780399226908", lambda *a: called.append(a) or (200, "{}"))[0] is None
    assert es.fetch_bestbuy("09780399226908", lambda *a: called.append(a) or (200, "{}"))[0] is None
    assert not called


def test_health_alerts_on_stall_and_dead_sources(conn):
    conn.execute("INSERT INTO item_master (gtin, name, created_at) VALUES ('00036000291452','x', datetime('now','-10 days'))")
    for i in range(25):
        _log(conn, f"0003600029{i:04d}", "serper", 400)
        _log(conn, f"0003600030{i:04d}", "kroger", 404)
        _log(conn, f"0003600031{i:04d}", "ebay", 200 if i == 0 else 404)
    conn.commit()
    cs.health(conn)
    text = " ".join(cs.ALERTS)
    assert "no new catalog items" in text and "kroger" in text and "ebay" not in text
    assert "serper" not in text   # turned off 11/20: its old errors must not set off the alarm
    assert cs.should_fail(datetime(2026, 11, 21, cs.ALERT_HOUR_UTC, 17))
    assert not cs.should_fail(datetime(2026, 11, 21, (cs.ALERT_HOUR_UTC + 1) % 24, 17))
    conn.execute("INSERT INTO item_master (gtin, name) VALUES ('00071662005107','new')")
    conn.execute("DELETE FROM lookup_log")
    conn.commit()
    cs.health(conn)
    assert cs.ALERTS == []


def test_three_places_stops_on_serper_400_out_of_credits(conn, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "k")
    for g in ("00071662005107", "00036000291452"):
        conn.execute("INSERT INTO item_master (gtin, name, fmv_unit) VALUES (?, 'Item', 1.0)", (g,))
    conn.commit()
    calls = []
    cs.three_places(conn, gik, 10, post=lambda k, q: calls.append(q) or (400, '{"message":"Not enough credits"}'))
    assert len(calls) == 1


def test_serper_is_off_by_default(conn, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "k")
    called = []
    monkeypatch.setattr(cs, "_serper_post", lambda k, q: called.append(q) or (200, "{}"))
    conn.execute("INSERT INTO item_master (gtin, name, fmv_unit) VALUES ('00071662005107', 'Item', 1.0)")
    conn.commit()
    assert cs.SERPER_ON is False
    cs.run(conn, 0, gik)
    assert called == []
