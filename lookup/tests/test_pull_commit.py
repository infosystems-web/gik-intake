"""cloud_pull must end its read transaction on Supabase, so the long lookup phase never holds locks (11/23)."""
import os, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gik


class Cur:
    def __init__(self, pg): self.pg = pg
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.pg.log.append("exec")
    def fetchall(self): return []


class PG:
    def __init__(self): self.log = []
    def cursor(self): return Cur(self)
    def commit(self): self.log.append("commit")
    def rollback(self): self.log.append("rollback")


def test_cloud_pull_ends_read_transaction(monkeypatch):
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(gik, "CLOUD_TABLES", ["donors"])
    conn.execute("create table donors (donor_id integer primary key, name text)")
    monkeypatch.setattr(gik, "cloud_columns", lambda pg, t: ({"donor_id": "integer", "name": "text"}, ["donor_id"]))
    pg = PG()
    gik.cloud_pull(conn, pg)
    assert "exec" in pg.log and pg.log[-1] == "commit"
