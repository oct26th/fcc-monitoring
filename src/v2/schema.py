"""
V2 競品情報網 — Data Lake Schema
==================================
SQLite schema definitions for the brand intelligence Data Lake.

Design philosophy:
  - SQLite stores structured/indexed metadata (fast query, dedup, history)
  - JSON blobs stored both inline (SQLite TEXT col) AND as files on disk
    at data/v2/raw/{brand_id}/{date}/{url_hash}.json
  - This dual-store gives us: queryability + raw backup + LLM re-processing

Tables:
  brand_sources   — Registry of monitored brands (seed data, rarely changes)
  crawl_runs      — Audit log: when did we crawl what, and did it succeed?
  raw_pages       — Raw scraped content (markdown/HTML) per URL per run
  news_articles   — Parsed news items extracted from raw_pages
  products        — Parsed product entries extracted from raw_pages
  intelligence    — LLM-extracted insights (key_points, specs, launch_date)
"""

import sqlite3
from pathlib import Path


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

DDL_STATEMENTS = [

    # ------------------------------------------------------------------
    # brand_sources — Static registry (populated from brand_sources.py)
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS brand_sources (
        brand_id        TEXT PRIMARY KEY,         -- e.g. "zebra"
        display_name    TEXT NOT NULL,            -- e.g. "Zebra Technologies"
        grantee_codes   TEXT NOT NULL,            -- JSON array e.g. '["UZ7"]'
        newsroom_urls   TEXT NOT NULL,            -- JSON array of URLs
        products_urls   TEXT NOT NULL,            -- JSON array of URLs
        crawl_type      TEXT NOT NULL DEFAULT 'spider',
        js_required     INTEGER NOT NULL DEFAULT 1,
        notes           TEXT DEFAULT '',
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,

    # ------------------------------------------------------------------
    # crawl_runs — Audit log for every crawl attempt
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS crawl_runs (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id          TEXT NOT NULL,            -- UUID per pipeline execution
        brand_id        TEXT NOT NULL,
        url             TEXT NOT NULL,
        url_type        TEXT NOT NULL,            -- 'newsroom' | 'products'
        status          TEXT NOT NULL,            -- 'success' | 'error' | 'blocked' | 'empty'
        http_status     INTEGER,                  -- HTTP response code (if available)
        content_length  INTEGER,                  -- bytes of content returned
        error_msg       TEXT,                     -- error detail if status != success
        duration_ms     INTEGER,                  -- request duration
        started_at      TEXT NOT NULL DEFAULT (datetime('now')),
        completed_at    TEXT,
        FOREIGN KEY (brand_id) REFERENCES brand_sources(brand_id)
    )
    """,

    # ------------------------------------------------------------------
    # raw_pages — One row per URL per crawl run
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS raw_pages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id          TEXT NOT NULL,            -- links to crawl_runs.run_id
        brand_id        TEXT NOT NULL,
        url             TEXT NOT NULL,
        url_type        TEXT NOT NULL,            -- 'newsroom' | 'products'
        content_format  TEXT NOT NULL DEFAULT 'markdown',  -- 'html' | 'markdown' | 'text'
        content         TEXT,                     -- actual scraped content (may be large)
        content_hash    TEXT,                     -- sha256 of content (dedup)
        file_path       TEXT,                     -- optional: path to disk backup
        crawled_at      TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (brand_id, url, content_hash),     -- dedup: don't store identical pages twice
        FOREIGN KEY (brand_id) REFERENCES brand_sources(brand_id)
    )
    """,

    # ------------------------------------------------------------------
    # news_articles — Structured news items parsed from raw_pages
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS news_articles (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_page_id     INTEGER,                  -- FK to raw_pages.id
        brand_id        TEXT NOT NULL,
        article_url     TEXT,                     -- canonical URL of the article
        title           TEXT NOT NULL,
        published_date  TEXT,                     -- ISO 8601 date string
        summary         TEXT,                     -- brief abstract / lede
        tags            TEXT DEFAULT '[]',        -- JSON array of tags/categories
        source_url      TEXT NOT NULL,            -- page it was found on
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (brand_id, article_url),           -- one row per brand+article
        FOREIGN KEY (raw_page_id) REFERENCES raw_pages(id),
        FOREIGN KEY (brand_id) REFERENCES brand_sources(brand_id)
    )
    """,

    # ------------------------------------------------------------------
    # products — Structured product entries parsed from raw_pages
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS products (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_page_id     INTEGER,
        brand_id        TEXT NOT NULL,
        product_url     TEXT,                     -- canonical product page URL
        product_name    TEXT NOT NULL,
        model_number    TEXT,                     -- SKU / model number if extractable
        category        TEXT,                     -- e.g. "mobile computer", "scanner"
        description     TEXT,
        specs_json      TEXT DEFAULT '{}',        -- JSON blob of raw specs
        image_url       TEXT,
        source_url      TEXT NOT NULL,
        first_seen_at   TEXT NOT NULL DEFAULT (datetime('now')),
        last_seen_at    TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (brand_id, product_url),
        FOREIGN KEY (raw_page_id) REFERENCES raw_pages(id),
        FOREIGN KEY (brand_id) REFERENCES brand_sources(brand_id)
    )
    """,

    # ------------------------------------------------------------------
    # intelligence — LLM-extracted competitive insights
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS intelligence (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source_type     TEXT NOT NULL,            -- 'news_article' | 'product'
        source_id       INTEGER NOT NULL,         -- FK to news_articles.id or products.id
        brand_id        TEXT NOT NULL,
        intel_type      TEXT NOT NULL,            -- 'key_points' | 'spec_summary' | 'launch_event' | 'competitor_mention'
        content         TEXT NOT NULL,            -- extracted intelligence text
        metadata_json   TEXT DEFAULT '{}',        -- extra structured data
        model_used      TEXT,                     -- which LLM extracted this
        confidence      REAL,                     -- 0.0–1.0 confidence score
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (brand_id) REFERENCES brand_sources(brand_id)
    )
    """,

    # ------------------------------------------------------------------
    # Indexes for common query patterns
    # ------------------------------------------------------------------
    "CREATE INDEX IF NOT EXISTS idx_crawl_runs_brand_id ON crawl_runs(brand_id)",
    "CREATE INDEX IF NOT EXISTS idx_crawl_runs_run_id ON crawl_runs(run_id)",
    "CREATE INDEX IF NOT EXISTS idx_crawl_runs_started_at ON crawl_runs(started_at)",
    "CREATE INDEX IF NOT EXISTS idx_raw_pages_brand_id ON raw_pages(brand_id)",
    "CREATE INDEX IF NOT EXISTS idx_raw_pages_content_hash ON raw_pages(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_news_articles_brand_id ON news_articles(brand_id)",
    "CREATE INDEX IF NOT EXISTS idx_news_articles_published_date ON news_articles(published_date)",
    "CREATE INDEX IF NOT EXISTS idx_products_brand_id ON products(brand_id)",
    "CREATE INDEX IF NOT EXISTS idx_products_first_seen ON products(first_seen_at)",
    "CREATE INDEX IF NOT EXISTS idx_intelligence_brand_id ON intelligence(brand_id)",
    "CREATE INDEX IF NOT EXISTS idx_intelligence_intel_type ON intelligence(intel_type)",
]


# ---------------------------------------------------------------------------
# Schema setup
# ---------------------------------------------------------------------------

def init_db(db_path: str | Path) -> sqlite3.Connection:
    """
    Initialize the Data Lake SQLite database.

    Creates all tables and indexes if they don't already exist.
    Returns an open connection (caller is responsible for closing).

    Usage:
        conn = init_db("data/v2/intelligence.db")
        # ... do work ...
        conn.close()
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row          # dict-like rows
    conn.execute("PRAGMA journal_mode=WAL")  # write-ahead logging for concurrency
    conn.execute("PRAGMA foreign_keys=ON")   # enforce FK constraints

    with conn:
        for stmt in DDL_STATEMENTS:
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)

    return conn


def get_schema_version(conn: sqlite3.Connection) -> dict:
    """Return a summary of the current schema (table names + row counts)."""
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()

    result = {}
    for (table_name,) in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
        result[table_name] = count
    return result
