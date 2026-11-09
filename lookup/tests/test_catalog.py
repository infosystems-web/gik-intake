"""Safety net for the lookup job. Runs before every scheduled run; if anything here fails, the job stops before it
touches the cloud database. No network: every outside service is replaced by a fake answer.

    cd lookup && python -m pytest -q tests
"""
import io
import json
import os
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ["GIK_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
for k in ("KROGER_CLIENT_ID", "KROGER_CLIENT_SECRET", "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "SERPER_API_KEY",
          "GOOGLE_BOOKS_KEY", "WALMART_CONSUMER_ID", "BESTBUY_API_KEY"):
    os.environ.pop(k, None)

import gik  # noqa: E402
import catalog_seed as cs  # noqa: E402
import bulk_products as bp  # noqa: E402
import extra_sources as es  # noqa: E402


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(gik, "DB_PATH", str(tmp_path / "gik.db"))
    gik.cmd_init(None)
    c = gik.connect()
    yield c
    c.close()


# ---------------------------------------------------------------- barcodes

def test_check_digits():
    assert gik.normalize_gtin("036000291452") == "00036000291452"
    assert gik.normalize_gtin("036000291453") is None            # wrong check digit
    assert cs.gtin_from_kroger("0003600029145") == "00036000291452"
    assert cs.gtin_from_kroger("0000000004011") is None          # produce PLU
    assert bp.gtin14("9780399226908") == "09780399226908"
    assert bp.gtin14("2012345678906") is None                    # in-store code
    assert bp.gtin14("abc") is None


# ---------------------------------------------------------------- bulk reference import

TSV = ("code\tproduct_name\tbrands\tquantity\tcategories_tags\tcategories_en\tcountries_tags\timage_url\n"
       "036000291452\tKleenex Facial Tissue\tKleenex,Kimberly-Clark\t160 ct\ten:tissues\tTissues\ten:united-states\thttp://img\n"
       "3600523000004\tFrench shampoo\tL'Oreal\t250 ml\ten:shampoos\tShampoos\ten:france\t\n"
       "036000291453\tBad check digit\tX\t\t\t\ten:united-states\t\n"
       "037000127864\tCrest Toothpaste\tCrest\t4.1 oz\ten:toothpastes\tToothpastes\t\t\n")


def test_parse_csv_keeps_us_valid_named():
    rows = list(bp.parse_csv("openbeautyfacts", io.StringIO(TSV)))
    assert [r[0] for r in rows] == ["00036000291452", "00037000127864"]
    assert rows[0][1] == "Kleenex Facial Tissue" and rows[0][2] == "Kleenex"
    assert rows[1][4] == "Apparel & hygiene"
    assert rows[0][7].startswith("https://world.openbeautyfacts.org/product/")


def test_parse_csv_food_only_classroom_categories():
    tsv = ("code\tproduct_name\tbrands\tcategories_tags\tcountries_tags\n"
           "028400090858\tCheetos\tFrito-Lay\ten:snacks,en:salty-snacks\ten:united-states\n"
           "070038000563\tGround beef\tX\ten:meats\ten:united-states\n")
    rows = list(bp.parse_csv("openfoodfacts", io.StringIO(tsv)))
    assert [r[1] for r in rows] == ["Cheetos"] and rows[0][4] == "Food & beverage"


def test_parse_csv_header_change_fails_loudly():
    with pytest.raises(ValueError):
        list(bp.parse_csv("openproductsfacts", io.StringIO("barcode\tname\n1\tx\n")))


def test_open_library_docs():
    docs = [{"key": "/works/OL1W", "title": "The Very Hungry Caterpillar", "author_name": ["Eric Carle"],
             "publisher": ["Philomel"], "isbn": ["0399226907", "9780399226908", "9780399226908", "9780000000000"]}]
    rows = list(bp.parse_ol_docs(docs, "Picture books"))
    assert [r[0] for r in rows] == ["09780399226908"]
    assert rows[0][1] == "The Very Hungry Caterpillar - by Eric Carle" and rows[0][4] == "Books & media"


def test_open_library_retries_then_moves_on(monkeypatch):
    monkeypatch.setattr(bp.time, "sleep", lambda s: None)
    calls = []

    def get(url):
        calls.append(url)
        raise OSError("timeout")
    assert list(bp.ol_rows(["Picture books"], pages=2, get=get)) == []
    assert len(calls) == 3


# ---------------------------------------------------------------- catalog seed

def _kroger(monkeypatch, data):
    monkeypatch.setattr(es, "kroger_keys", lambda: ("id", "secret"))
    monkeypatch.setattr(es, "_kroger_token", lambda: "token")
    monkeypatch.setattr(es, "_kroger_location", lambda h: "01400943")
    es._kroger["store_name"] = "Kroger Test"
    monkeypatch.setattr(gik, "http_get", lambda url, headers=None: (200, json.dumps({"data": data if "start" not in url else []})))


def test_kroger_seed_adds_priced_items_and_skips_groceries(conn, monkeypatch, tmp_path):
    _kroger(monkeypatch, [
        {"upc": "0003600029145", "description": "Kleenex Tissue", "brand": "Kleenex", "categories": ["Paper"],
         "items": [{"size": "160 ct", "price": {"regular": 2.49}}]},
        {"upc": "0001111041700", "description": "Milk", "categories": ["Dairy"], "items": [{"price": {"regular": 3}}]}])
    terms = tmp_path / "t.txt"
    terms.write_text("Household & lighting | tissues\n")
    monkeypatch.setattr(cs, "TERMS_FILE", str(terms))
    monkeypatch.setattr(cs, "load_terms", lambda path=str(terms): [("Household & lighting", "tissues")])
    cs.seed_kroger(conn, 5, gik)
    rows = conn.execute("SELECT gtin, name, fmv_unit, lookup_source, category FROM item_master WHERE lookup_source='kroger-seed'").fetchall()
    assert [tuple(r) for r in rows] == [("00036000291452", "Kleenex Tissue, 160 ct", 2.49, "kroger-seed", "Household & lighting")]
    cs.seed_kroger(conn, 5, gik)  # second run: term already done, nothing new, no error
    assert conn.execute("SELECT COUNT(*) FROM lookup_log WHERE source='kroger-seed'").fetchone()[0] == 1


def test_seed_never_overwrites_a_verified_value(conn, monkeypatch):
    conn.execute("INSERT INTO item_master (gtin, name, fmv_unit, fmv_status) VALUES ('00036000291452','Kleenex',9.99,'verified')")
    conn.commit()
    _kroger(monkeypatch, [{"upc": "0003600029145", "description": "Kleenex Tissue", "items": [{"price": {"regular": 2.49}}]}])
    monkeypatch.setattr(cs, "load_terms", lambda path=None: [("Household & lighting", "tissues")])
    cs.seed_kroger(conn, 5, gik)
    r = conn.execute("SELECT fmv_unit, fmv_status FROM item_master WHERE gtin='00036000291452'").fetchone()
    assert (r["fmv_unit"], r["fmv_status"]) == (9.99, "verified")


def test_learn_from_scans(conn):
    conn.execute("INSERT INTO donors (name, type) VALUES ('d', 'other')")
    conn.execute("INSERT INTO donations (donation_id, donor_id, received_date) VALUES ('D1', 1, '2026-11-01')")
    conn.execute("INSERT INTO item_master (gtin, name, brand, category) VALUES ('00071662005107','Crayons','Crayola','School supplies')")
    conn.execute("INSERT INTO item_master (gtin, name, brand, category) VALUES ('09780399226908','Caterpillar - by Eric Carle','Philomel','Books & media')")
    conn.execute("INSERT INTO receipts (donation_id, gtin, qty) VALUES ('D1','00071662005107',3), ('D1','09780399226908',1)")
    conn.commit()
    assert ("School supplies", "Crayola") in cs.brand_terms(conn, [])
    assert cs.scanned_book_queries(conn) == ['inauthor:"Eric Carle"', 'inpublisher:"Philomel"']


# ---------------------------------------------------------------- three places

def test_serper_offers_match_and_dedupe():
    res = [{"title": "Crayola Crayons 24 Count", "source": "Walmart", "price": "$1.97", "link": "https://w"},
           {"title": "Crayola Crayons 24 Count", "source": "Walmart", "price": "$2.10", "link": "https://w2"},
           {"title": "Crayola Crayons 24 ct (Renewed)", "source": "Amazon", "price": "$1.50", "link": "https://a"},
           {"title": "Sharpie markers", "source": "Target", "price": "$5.00", "link": "https://t"},
           {"title": "Crayola 24 Crayons", "source": "Target", "price": "$1.99", "link": "https://t2"}]
    offers = cs.serper_offers(res, "Crayola Crayons, 24 ct")
    assert [(o["merchant"], o["price"]) for o in offers] == [("Walmart", 1.97), ("Target", 1.99)]
    assert cs.parse_price("$1,299.00") == 1299.0 and cs.parse_price("free") is None


def test_three_places_stops_when_out_of_credits(conn, monkeypatch):
    monkeypatch.setenv("SERPER_API_KEY", "k")
    for g in ("00071662005107", "00036000291452"):
        conn.execute("INSERT INTO item_master (gtin, name, fmv_unit) VALUES (?, 'Item', 1.0)", (g,))
    conn.commit()
    calls = []

    def post(key, q):
        calls.append(q)
        return 402, '{"message":"Not enough credits"}'
    cs.three_places(conn, gik, 10, post=post)
    assert len(calls) == 1


# ---------------------------------------------------------------- regression: the 11/8 crash

def test_lookup_result_when_open_prices_prices_an_item(conn, monkeypatch):
    monkeypatch.setattr(gik, "extra_sources", None)
    monkeypatch.setattr(gik, "fetch_upcitemdb", lambda g, k: (404, "{}"))
    monkeypatch.setattr(gik, "fetch_openprices", lambda g: (200, "{}", [
        {"merchant": "Store", "price": 3.5, "link": "http://x", "condition": "New", "title": "Thing", "updated_t": 0}]))
    res = gik.do_lookup(conn, "00043168425445", thorough=True)
    assert res["fmv"] == 3.5 and res["offers"] == 1


# ---------------------------------------------------------------- efficiency: spend lookups where the dollars are

def test_scanned_items_first_and_unscanned_preloads_skipped(conn):
    conn.execute("INSERT INTO donors (name, type) VALUES ('d', 'other')")
    conn.execute("INSERT INTO donations (donation_id, donor_id, received_date) VALUES ('D1', 1, '2026-11-01')")
    conn.execute("INSERT INTO item_master (gtin) VALUES ('00036000291452'), ('00037000127864')")
    conn.execute("INSERT INTO item_master (gtin, name, lookup_source) VALUES ('00071662005107', 'Crayons', 'kroger-seed')")
    conn.execute("INSERT INTO receipts (donation_id, gtin, qty) VALUES ('D1','00036000291452',1), ('D1','00037000127864',9)")
    conn.commit()
    todo = gik.pending_lookups(conn)
    assert todo[:2] == ["00037000127864", "00036000291452"]   # most units first
    assert "00071662005107" not in todo                        # pre-loaded, never scanned: left to the free price fill


def test_refresh_survives_trimmed_raw_text(conn):
    conn.execute("INSERT INTO item_master (gtin) VALUES ('00036000291452')")
    conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES ('00036000291452','kroger',200,NULL)")
    conn.execute("INSERT INTO lookup_log (gtin, source, http_status, raw_response) VALUES ('00036000291452','googlebooks',200,NULL)")
    conn.commit()

    class A:
        mock = False
    gik.cmd_refresh(A())


def test_more_places_counts_ebay_once_and_stops_at_three(conn, monkeypatch):
    conn.execute("INSERT INTO item_master (gtin, name, fmv_unit) VALUES ('00036000291452', 'Kleenex', 2.49)")
    conn.commit()
    gik.record_offers(conn, "00036000291452", [
        {"merchant": "eBay (a)", "price": 3, "link": "https://e/1", "condition": "New"},
        {"merchant": "eBay (b)", "price": 3.1, "link": "https://e/2", "condition": "New"}], "ebay")
    calls = []
    monkeypatch.setattr(es, "PRICE_SOURCES", {
        "kroger": lambda g, h: (calls.append("kroger") or 200, "{}", [{"merchant": "Kroger", "price": 2.49, "link": "https://k", "condition": "New"}], {}),
        "walmart": lambda g, h: (calls.append("walmart") or 200, "{}", [{"merchant": "Walmart", "price": 2.29, "link": "https://w", "condition": "New"}], {}),
        "bestbuy": lambda g, h: (calls.append("bestbuy") or 200, "{}", [], {})})
    monkeypatch.setattr(gik, "fetch_openprices", lambda g: (calls.append("openprices") or 200, "{}", []))
    cs.evidence_fill(conn, gik, 10)
    assert calls == ["kroger", "walmart"]          # eBay sellers = 1 place; Kroger + Walmart make 3; then it stops
    cs.evidence_fill(conn, gik, 10)
    assert calls == ["kroger", "walmart"]          # three places reached: nothing asked again
