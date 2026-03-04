"""Database management for FCC Monitor."""
import sqlite3
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from .models import FCCRecord

logger = logging.getLogger("fcc_monitor.database")

class DatabaseManager:
    """Manages SQLite database for FCC records."""
    
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self._ensure_data_dir()
        self.init_database()
        
    def _ensure_data_dir(self):
        """Create data directory if it doesn't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
    def init_database(self):
        """Initialize the SQLite database schema."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        # Records table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS fcc_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fcc_id TEXT NOT NULL,
                grantee_code TEXT NOT NULL,
                product_code TEXT,
                applicant_name TEXT,
                product_name TEXT,
                product_description TEXT,
                grant_date TEXT,
                filing_date TEXT,
                certification_date TEXT,
                application_type TEXT,
                status TEXT,
                expires_on TEXT,
                equipment_class TEXT,
                fingerprint TEXT UNIQUE,
                first_seen TEXT,
                last_updated TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fcc_id ON fcc_records(fcc_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_grantee_code ON fcc_records(grantee_code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fingerprint ON fcc_records(fingerprint)")
        
        # Crawl history
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS crawl_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                grantee_code TEXT NOT NULL,
                crawl_timestamp TEXT NOT NULL,
                record_count INTEGER,
                errors TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()

    def save_record(self, record: FCCRecord) -> bool:
        """
        Save an FCC record to the database.
        Returns True if the record is new (wasn't in DB with same fingerprint).
        """
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        
        fingerprint = record.fingerprint()
        now = datetime.now().isoformat()
        
        cursor.execute("SELECT id FROM fcc_records WHERE fingerprint = ?", (fingerprint,))
        existing = cursor.fetchone()
        
        if existing:
            cursor.execute("""
                UPDATE fcc_records 
                SET grant_date = ?,
                    filing_date = ?,
                    certification_date = ?,
                    application_type = ?,
                    product_description = ?,
                    product_name = ?,
                    status = ?,
                    last_updated = ?
                WHERE fingerprint = ?
            """, (
                record.grant_date, record.filing_date, record.certification_date, 
                record.application_type, record.product_description, record.product_name,
                record.status, now, fingerprint
            ))
            conn.commit()
            conn.close()
            return False
        else:
            cursor.execute("""
                INSERT INTO fcc_records (
                    fcc_id, grantee_code, product_code, applicant_name,
                    product_description, product_name, grant_date, filing_date, 
                    certification_date, application_type, status, equipment_class, 
                    expires_on, fingerprint, first_seen, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.fcc_id, record.grantee_code, record.product_code, record.applicant_name,
                record.product_description, record.product_name, record.grant_date, record.filing_date,
                record.certification_date, record.application_type, record.status, record.equipment_class,
                record.expires_on, fingerprint, now, now
            ))
            conn.commit()
            conn.close()
            return True

    def log_crawl_history(self, grantee_code: str, record_count: int, errors: str = None):
        """Log a crawl operation in history."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO crawl_history (grantee_code, crawl_timestamp, record_count, errors)
            VALUES (?, ?, ?, ?)
        """, (grantee_code, datetime.now().isoformat(), record_count, errors))
        conn.commit()
        conn.close()

    def get_latest_records(self, limit: int = 20) -> List[FCCRecord]:
        """Get latest records added to the database."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM fcc_records ORDER BY created_at DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        
        records = []
        for row in rows:
            # Handle mapping database row to FCCRecord dataclass
            data = dict(row)
            # Remove DB specific fields
            db_fields = ['id', 'first_seen', 'last_updated', 'created_at', 'fingerprint']
            for field in db_fields:
                data.pop(field, None)
            records.append(FCCRecord(**data))
            
        conn.close()
        return records
