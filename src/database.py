"""Lightweight SQLite persistence for FCC Monitor.

Schema is intentionally minimal: we store exactly what FCCRecord carries,
plus a received_at timestamp so we can tell when each ID first appeared.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from .models import FCCRecord


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS fcc_records (
    fcc_id              TEXT PRIMARY KEY,
    grantee_code        TEXT NOT NULL,
    product_code        TEXT NOT NULL DEFAULT '',
    applicant_name      TEXT NOT NULL DEFAULT '',
    product_name        TEXT NOT NULL DEFAULT '',
    product_description TEXT NOT NULL DEFAULT '',
    certification_date  TEXT NOT NULL DEFAULT '',
    grant_date          TEXT NOT NULL DEFAULT '',
    filing_date         TEXT NOT NULL DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'Granted',
    application_type    TEXT NOT NULL DEFAULT '',
    expires_on          TEXT,
    received_at         TEXT NOT NULL
);
"""

_CREATE_IDX = """
CREATE INDEX IF NOT EXISTS idx_grantee ON fcc_records (grantee_code);
"""


class Database:
    """Thin SQLite wrapper for FCCRecord persistence."""

    def __init__(self, db_path: str = "data/fcc_monitor.db"):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self):
        conn = self._connect()
        conn.execute(_CREATE_TABLE)
        conn.execute(_CREATE_IDX)
        conn.commit()
        logger.debug(f"Database ready at {self._path}")

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_known_ids(self, grantee_code: Optional[str] = None) -> set[str]:
        """Return all FCC IDs already stored (optionally filtered by grantee)."""
        conn = self._connect()
        if grantee_code:
            rows = conn.execute(
                "SELECT fcc_id FROM fcc_records WHERE grantee_code = ?",
                (grantee_code,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT fcc_id FROM fcc_records").fetchall()
        return {r["fcc_id"] for r in rows}

    def filter_new(self, records: list[FCCRecord]) -> list[FCCRecord]:
        """Return only records whose fcc_id is NOT yet in the database."""
        if not records:
            return []
        grantee_code = records[0].grantee_code
        known = self.get_known_ids(grantee_code)
        new = [r for r in records if r.fcc_id not in known]
        logger.info(
            f"[DB] {grantee_code}: {len(records)} fetched, "
            f"{len(known)} known, {len(new)} new"
        )
        return new

    def save_records(self, records: list[FCCRecord]) -> int:
        """
        Insert records that are not already stored.

        Returns number of rows actually inserted.
        """
        if not records:
            return 0

        conn = self._connect()
        now = datetime.now(timezone.utc).isoformat()
        inserted = 0

        for r in records:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO fcc_records (
                        fcc_id, grantee_code, product_code, applicant_name,
                        product_name, product_description, certification_date,
                        grant_date, filing_date, status, application_type,
                        expires_on, received_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r.fcc_id, r.grantee_code, r.product_code,
                        r.applicant_name, r.product_name, r.product_description,
                        r.certification_date, r.grant_date, r.filing_date,
                        r.status, r.application_type, r.expires_on, now,
                    ),
                )
                inserted += conn.execute("SELECT changes()").fetchone()[0]
            except sqlite3.Error as exc:
                logger.error(f"[DB] Failed to insert {r.fcc_id}: {exc}")

        conn.commit()
        logger.info(f"[DB] Saved {inserted}/{len(records)} records")
        return inserted

    def get_record(self, fcc_id: str) -> Optional[FCCRecord]:
        """Fetch a single record by FCC ID, or None if not found."""
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM fcc_records WHERE fcc_id = ?", (fcc_id,)
        ).fetchone()
        if row is None:
            return None
        return FCCRecord(
            fcc_id=row["fcc_id"],
            grantee_code=row["grantee_code"],
            product_code=row["product_code"],
            applicant_name=row["applicant_name"],
            product_name=row["product_name"],
            product_description=row["product_description"],
            certification_date=row["certification_date"],
            grant_date=row["grant_date"],
            filing_date=row["filing_date"],
            status=row["status"],
            application_type=row["application_type"],
            expires_on=row["expires_on"],
        )

    def record_count(self, grantee_code: Optional[str] = None) -> int:
        """Total stored record count (optionally filtered by grantee)."""
        conn = self._connect()
        if grantee_code:
            return conn.execute(
                "SELECT COUNT(*) FROM fcc_records WHERE grantee_code = ?",
                (grantee_code,),
            ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM fcc_records").fetchone()[0]
