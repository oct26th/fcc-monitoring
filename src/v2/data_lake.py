"""
V2 競品情報網 — Data Lake CRUD
================================
SQLite read/write operations for the brand intelligence Data Lake.

Follows the same lightweight CRUD pattern as V1's database.py.
No ORM — plain sqlite3 with dict-like rows.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from .brand_sources import BRAND_SOURCES, BrandSource
from .schema import init_db


# ---------------------------------------------------------------------------
# Default DB path
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = Path("data/v2/intelligence.db")
DEFAULT_RAW_DIR = Path("data/v2/raw")


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

class DataLake:
    """
    Thin wrapper around the SQLite Data Lake.

    Usage:
        lake = DataLake()
        with lake:
            lake.upsert_brand_sources()
            run_id = lake.start_run(brand_id, url, url_type)
            ...

    Or use as a plain context manager:
        with DataLake("data/v2/intelligence.db") as lake:
            ...
    """

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        raw_dir: str | Path = DEFAULT_RAW_DIR,
    ):
        self.db_path = Path(db_path)
        self.raw_dir = Path(raw_dir)
        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> "DataLake":
        self.connect()
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def connect(self):
        """Open (or reuse) a database connection."""
        if self._conn is None:
            self._conn = init_db(self.db_path)
            logger.info(f"[DataLake] Connected to {self.db_path}")
        return self._conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.connect()
        return self._conn

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.debug("[DataLake] Connection closed.")

    # ------------------------------------------------------------------
    # Brand Sources — seed data
    # ------------------------------------------------------------------

    def upsert_brand_sources(self, sources: list[BrandSource] = BRAND_SOURCES):
        """
        Upsert the brand registry into brand_sources table.
        Call once at startup to keep the registry in sync.
        """
        now = _now()
        with self.conn:
            for b in sources:
                self.conn.execute(
                    """
                    INSERT INTO brand_sources
                        (brand_id, display_name, grantee_codes, newsroom_urls,
                         products_urls, crawl_type, js_required, notes, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(brand_id) DO UPDATE SET
                        display_name  = excluded.display_name,
                        grantee_codes = excluded.grantee_codes,
                        newsroom_urls = excluded.newsroom_urls,
                        products_urls = excluded.products_urls,
                        crawl_type    = excluded.crawl_type,
                        js_required   = excluded.js_required,
                        notes         = excluded.notes,
                        updated_at    = excluded.updated_at
                    """,
                    (
                        b.brand_id,
                        b.display_name,
                        json.dumps(b.grantee_codes),
                        json.dumps(b.newsroom_urls),
                        json.dumps(b.products_urls),
                        b.crawl_type,
                        1 if b.js_required else 0,
                        b.notes,
                        now,
                        now,
                    ),
                )
        logger.info(f"[DataLake] Upserted {len(sources)} brand sources.")

    # ------------------------------------------------------------------
    # Crawl Runs — audit trail
    # ------------------------------------------------------------------

    def start_run(
        self,
        run_id: str,
        brand_id: str,
        url: str,
        url_type: str,
    ) -> int:
        """
        Insert a new crawl_run row with status='running'.
        Returns the row id.
        """
        now = _now()
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO crawl_runs
                    (run_id, brand_id, url, url_type, status, started_at)
                VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (run_id, brand_id, url, url_type, now),
            )
        return cur.lastrowid

    def finish_run(
        self,
        run_id: str,
        url: str,
        *,
        status: str,
        http_status: Optional[int] = None,
        content_length: Optional[int] = None,
        error_msg: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ):
        """Update a crawl_run row when the fetch completes (success or failure)."""
        now = _now()
        with self.conn:
            self.conn.execute(
                """
                UPDATE crawl_runs SET
                    status         = ?,
                    http_status    = ?,
                    content_length = ?,
                    error_msg      = ?,
                    duration_ms    = ?,
                    completed_at   = ?
                WHERE run_id = ? AND url = ?
                """,
                (status, http_status, content_length, error_msg, duration_ms, now, run_id, url),
            )

    def get_recent_runs(self, brand_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Return recent crawl_runs, optionally filtered by brand."""
        if brand_id:
            rows = self.conn.execute(
                "SELECT * FROM crawl_runs WHERE brand_id = ? ORDER BY started_at DESC LIMIT ?",
                (brand_id, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM crawl_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Raw Pages — scraped content storage
    # ------------------------------------------------------------------

    def save_raw_page(
        self,
        run_id: str,
        brand_id: str,
        url: str,
        url_type: str,
        content: str,
        content_format: str = "markdown",
        write_file: bool = True,
    ) -> Optional[int]:
        """
        Persist a scraped page.

        Deduplication: if exact same content (same hash) already stored for
        this brand+url, skip the insert and return None.

        If write_file=True, also write a JSON backup to disk at:
          data/v2/raw/{brand_id}/{date}/{url_hash[:8]}.json
        """
        content_hash = _sha256(content)
        now = _now()
        date_str = now[:10]  # YYYY-MM-DD

        file_path: Optional[str] = None
        if write_file:
            out_dir = self.raw_dir / brand_id / date_str
            out_dir.mkdir(parents=True, exist_ok=True)
            url_slug = _url_hash(url)[:8]
            file_path_obj = out_dir / f"{url_type}_{url_slug}.json"
            payload = {
                "run_id": run_id,
                "brand_id": brand_id,
                "url": url,
                "url_type": url_type,
                "content_format": content_format,
                "content_hash": content_hash,
                "crawled_at": now,
                "content": content,
            }
            file_path_obj.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
            file_path = str(file_path_obj)
            logger.debug(f"[DataLake] Raw page written to {file_path}")

        try:
            with self.conn:
                cur = self.conn.execute(
                    """
                    INSERT INTO raw_pages
                        (run_id, brand_id, url, url_type, content_format,
                         content, content_hash, file_path, crawled_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id, brand_id, url, url_type,
                        content_format, content, content_hash, file_path, now,
                    ),
                )
            row_id = cur.lastrowid
            logger.info(
                f"[DataLake] Saved raw_page id={row_id} for {brand_id} / {url_type}"
            )
            return row_id

        except sqlite3.IntegrityError:
            # UNIQUE constraint: identical content already stored
            logger.info(
                f"[DataLake] Skipped duplicate raw_page for {brand_id} / {url[:60]}"
            )
            return None

    def get_latest_raw_page(self, brand_id: str, url: str) -> Optional[dict]:
        """Return the most recent raw_page row for a brand+url."""
        row = self.conn.execute(
            """
            SELECT * FROM raw_pages
            WHERE brand_id = ? AND url = ?
            ORDER BY crawled_at DESC LIMIT 1
            """,
            (brand_id, url),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # News Articles
    # ------------------------------------------------------------------

    def upsert_article(
        self,
        raw_page_id: Optional[int],
        brand_id: str,
        title: str,
        source_url: str,
        *,
        article_url: Optional[str] = None,
        published_date: Optional[str] = None,
        summary: Optional[str] = None,
        tags: Optional[list] = None,
    ) -> int:
        """
        Insert or update a news article.
        UNIQUE constraint on (brand_id, article_url) — update if exists.
        """
        now = _now()
        tags_json = json.dumps(tags or [])
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO news_articles
                    (raw_page_id, brand_id, article_url, title,
                     published_date, summary, tags, source_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(brand_id, article_url) DO UPDATE SET
                    title          = excluded.title,
                    published_date = excluded.published_date,
                    summary        = excluded.summary,
                    tags           = excluded.tags,
                    source_url     = excluded.source_url
                """,
                (
                    raw_page_id, brand_id, article_url, title,
                    published_date, summary, tags_json, source_url, now,
                ),
            )
        return cur.lastrowid

    def get_articles(
        self,
        brand_id: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Query news articles. since is an ISO date string."""
        conditions = []
        params = []
        if brand_id:
            conditions.append("brand_id = ?")
            params.append(brand_id)
        if since:
            conditions.append("published_date >= ?")
            params.append(since)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self.conn.execute(
            f"SELECT * FROM news_articles {where} ORDER BY published_date DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Products
    # ------------------------------------------------------------------

    def upsert_product(
        self,
        raw_page_id: Optional[int],
        brand_id: str,
        product_name: str,
        source_url: str,
        *,
        product_url: Optional[str] = None,
        model_number: Optional[str] = None,
        category: Optional[str] = None,
        description: Optional[str] = None,
        specs: Optional[dict] = None,
        image_url: Optional[str] = None,
    ) -> int:
        """
        Insert or update a product entry.
        UNIQUE constraint on (brand_id, product_url).
        """
        now = _now()
        specs_json = json.dumps(specs or {})
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO products
                    (raw_page_id, brand_id, product_url, product_name, model_number,
                     category, description, specs_json, image_url, source_url,
                     first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(brand_id, product_url) DO UPDATE SET
                    product_name  = excluded.product_name,
                    model_number  = excluded.model_number,
                    category      = excluded.category,
                    description   = excluded.description,
                    specs_json    = excluded.specs_json,
                    image_url     = excluded.image_url,
                    source_url    = excluded.source_url,
                    last_seen_at  = excluded.last_seen_at
                """,
                (
                    raw_page_id, brand_id, product_url, product_name, model_number,
                    category, description, specs_json, image_url, source_url,
                    now, now,
                ),
            )
        return cur.lastrowid

    def get_products(
        self,
        brand_id: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict]:
        """Query products with optional brand/category filter."""
        conditions = []
        params = []
        if brand_id:
            conditions.append("brand_id = ?")
            params.append(brand_id)
        if category:
            conditions.append("category LIKE ?")
            params.append(f"%{category}%")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self.conn.execute(
            f"SELECT * FROM products {where} ORDER BY first_seen_at DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Intelligence
    # ------------------------------------------------------------------

    def save_intelligence(
        self,
        source_type: str,
        source_id: int,
        brand_id: str,
        intel_type: str,
        content: str,
        *,
        metadata: Optional[dict] = None,
        model_used: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> int:
        """Persist an LLM-extracted intelligence item."""
        now = _now()
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT INTO intelligence
                    (source_type, source_id, brand_id, intel_type,
                     content, metadata_json, model_used, confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_type, source_id, brand_id, intel_type,
                    content, json.dumps(metadata or {}), model_used, confidence, now,
                ),
            )
        return cur.lastrowid

    # ------------------------------------------------------------------
    # Summary / Stats
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        """Return row counts for all tables (quick health check)."""
        tables = ["brand_sources", "crawl_runs", "raw_pages",
                  "news_articles", "products", "intelligence"]
        result = {}
        for t in tables:
            try:
                count = self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                result[t] = count
            except Exception:
                result[t] = -1
        return result


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    """ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()
