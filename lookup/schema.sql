-- GIK barcode intake: staging dataset that sits outside NetSuite.
-- SQLite today; the same DDL runs on Postgres with minor type changes.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS donors (
    donor_id        INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    type            TEXT NOT NULL CHECK (type IN ('corporate','freight-in-kind','individual','nerc-partner','other')),
    ns_customer_id  TEXT,              -- NetSuite customer/donor internal ID (approach 3)
    contact         TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS donations (
    donation_id     TEXT PRIMARY KEY,  -- e.g. DON-2026-0042; becomes NetSuite External ID
    donor_id        INTEGER NOT NULL REFERENCES donors(donor_id),
    received_date   TEXT NOT NULL,     -- YYYY-MM-DD
    bol_number      TEXT,
    pallets         INTEGER,
    restriction     TEXT,              -- donor-imposed restriction, disclosed under ASU 2020-07
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Category -> GL accounts (approach 1). Account IDs are NetSuite INTERNAL IDs, not numbers.
CREATE TABLE IF NOT EXISTS gl_map (
    category            TEXT PRIMARY KEY,
    debit_account_id    TEXT NOT NULL,
    debit_account_name  TEXT NOT NULL,
    credit_account_id   TEXT NOT NULL,
    credit_account_name TEXT NOT NULL,
    class_id            TEXT,
    class_name          TEXT
);

-- Standard item catalog (approach 2). One row per barcode, researched once.
CREATE TABLE IF NOT EXISTS item_master (
    gtin             TEXT PRIMARY KEY,  -- 14-digit normalized GTIN
    name             TEXT,
    brand            TEXT,
    category         TEXT REFERENCES gl_map(category),
    raw_category     TEXT,              -- category string as the lookup source returned it
    fmv_unit         REAL,
    fmv_basis        TEXT NOT NULL DEFAULT 'retail' CHECK (fmv_basis IN ('retail','wholesale','appraised')),
    fmv_source_url   TEXT,
    fmv_captured_at  TEXT,
    fmv_status       TEXT NOT NULL DEFAULT 'unverified' CHECK (fmv_status IN ('unverified','verified','disputed','not_found')),
    lookup_source    TEXT,              -- upcitemdb | manual | deferred | ...
    fmv_merchant     TEXT,              -- seller the FMV offer came from
    offer_updated    TEXT,              -- date of the newest offer the lookup returned
    notes            TEXT,              -- flags for the reviewer (used listing, stale offers, pending lookup)
    ns_item_id       TEXT,              -- NetSuite item internal ID once created
    model_number     TEXT,              -- manufacturer model number (from UPCitemdb); proves a store listing is this item
    page_count       INTEGER,           -- books: page count from Google Books (sorts picture books from chapter books)
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every scan event.
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id   INTEGER PRIMARY KEY,
    donation_id  TEXT NOT NULL REFERENCES donations(donation_id),
    gtin         TEXT NOT NULL REFERENCES item_master(gtin),
    qty          INTEGER NOT NULL DEFAULT 1 CHECK (qty > 0),
    condition    TEXT NOT NULL DEFAULT 'new' CHECK (condition IN ('new','like-new','used','damaged')),
    scanned_at   TEXT NOT NULL DEFAULT (datetime('now')),
    scanned_by   TEXT,
    push_status  TEXT NOT NULL DEFAULT 'staged' CHECK (push_status IN ('staged','exported','pushed','error')),
    ns_tran_id   TEXT,
    export_batch TEXT
);

-- Audit trail of every external lookup, raw response kept.
CREATE TABLE IF NOT EXISTS lookup_log (
    log_id        INTEGER PRIMARY KEY,
    gtin          TEXT NOT NULL,
    source        TEXT NOT NULL,
    requested_at  TEXT NOT NULL DEFAULT (datetime('now')),
    http_status   INTEGER,
    raw_response  TEXT
);

-- Retailer product pages per barcode (Amazon / Walmart / Target / other).
-- search_url is generated; product_url and price are what a reviewer confirmed.
CREATE TABLE IF NOT EXISTS item_links (
    gtin          TEXT NOT NULL REFERENCES item_master(gtin),
    retailer      TEXT NOT NULL CHECK (retailer IN ('amazon','walmart','target','other')),
    search_url    TEXT NOT NULL,
    product_url   TEXT,
    price         REAL,
    in_stock      INTEGER,            -- 1/0/NULL
    checked_at    TEXT,
    checked_by    TEXT,
    PRIMARY KEY (gtin, retailer)
);

CREATE INDEX IF NOT EXISTS idx_receipts_donation ON receipts(donation_id);
CREATE INDEX IF NOT EXISTS idx_receipts_push ON receipts(push_status);
CREATE INDEX IF NOT EXISTS idx_item_status ON item_master(fmv_status);

-- Review queue: anything that cannot be pushed yet.
CREATE VIEW IF NOT EXISTS v_review_queue AS
SELECT im.gtin, im.name, im.brand, im.raw_category, im.category, im.fmv_unit,
       im.fmv_status, im.fmv_source_url,
       COALESCE(SUM(r.qty), 0) AS units_waiting
FROM item_master im
LEFT JOIN receipts r ON r.gtin = im.gtin AND r.push_status = 'staged'
WHERE im.fmv_status <> 'verified' OR im.category IS NULL
GROUP BY im.gtin;

-- Journal lines: one debit + one credit per donation per category, verified items only.
CREATE VIEW IF NOT EXISTS v_journal_lines AS
SELECT d.donation_id, d.received_date, dn.name AS donor_name, dn.ns_customer_id,
       im.category, g.debit_account_id, g.debit_account_name,
       g.credit_account_id, g.credit_account_name, g.class_id,
       SUM(r.qty) AS units,
       ROUND(SUM(r.qty * im.fmv_unit), 2) AS amount
FROM receipts r
JOIN item_master im ON im.gtin = r.gtin
JOIN gl_map g ON g.category = im.category
JOIN donations d ON d.donation_id = r.donation_id
JOIN donors dn ON dn.donor_id = d.donor_id
WHERE r.push_status = 'staged' AND im.fmv_status = 'verified' AND im.category IS NOT NULL
GROUP BY d.donation_id, im.category;
