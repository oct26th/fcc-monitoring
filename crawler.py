#!/usr/bin/env python3
"""
FCC Monitoring Crawler
Crawls FCC.gov for new equipment approvals using SpiderCloud API.
Monitors specified grantee codes, detects changes, and sends Telegram notifications.

Usage:
    python crawler.py              # Run once
    python crawler.py --daemon     # Run as daemon with scheduler

Environment Variables:
    SPIDERCLOUD_API_KEY: SpiderCloud API key (Bearer token)
    TELEGRAM_BOT_TOKEN: Telegram bot token
    TELEGRAM_CHAT_ID: Target chat ID for notifications
"""

import argparse
import json
import os
import sqlite3
import sys
import hashlib
from datetime import datetime, date
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional
import logging

import httpx
from loguru import logger

# Project root
PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "fcc_monitor.db"
LAST_STATE_FILE = DATA_DIR / "last_state.json"

# ============================================================================
# Configuration
# ============================================================================

# Grantee codes to monitor (as specified in requirements)
GRANTEE_CODES = [
    "UZ7", "HD5", "U4F", "U4G", 
    "V2X", "SS4", "HLE", 
    "2AOJL", "2AC6A", "2AR9L"
]

# SpiderCloud API configuration
SPIDERCLOUD_API_KEY = os.getenv("SPIDERCLOUD_API_KEY", "sk-ac0423ad-1897-4094-bc2c-5e5a92777375")
SPIDERCLOUD_BASE_URL = "https://api.spidercloud.com/v1"

# Telegram configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================================
# Data Models
# ============================================================================

@dataclass
class FCCRecord:
    """FCC Equipment Authorization record."""
    fcc_id: str
    grantee_code: str
    product_code: str
    applicant_name: str
    product_name: str
    certification_date: str
    status: str
    expires_on: Optional[str] = None
    equipment_class: Optional[str] = None
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'FCCRecord':
        return cls(
            fcc_id=data.get("fcc_id", ""),
            grantee_code=data.get("grantee_code", ""),
            product_code=data.get("product_code", ""),
            applicant_name=data.get("applicant_name", ""),
            product_name=data.get("product_name", ""),
            certification_date=data.get("certification_date", ""),
            status=data.get("status", "Granted"),
            expires_on=data.get("expires_on"),
            equipment_class=data.get("equipment_class")
        )
    
    def fingerprint(self) -> str:
        """Generate unique fingerprint for this record."""
        return hashlib.sha256(
            f"{self.fcc_id}:{self.grantee_code}:{self.product_code}".encode()
        ).hexdigest()[:16]


@dataclass
class CrawlResult:
    """Result of a crawl operation."""
    timestamp: str
    grantee_code: str
    record_count: int
    records: list[FCCRecord]
    errors: list[str] = field(default_factory=list)


# ============================================================================
# Database Management
# ============================================================================

def init_database():
    """Initialize SQLite database."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # Create records table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fcc_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fcc_id TEXT NOT NULL,
            grantee_code TEXT NOT NULL,
            product_code TEXT,
            applicant_name TEXT,
            product_name TEXT,
            certification_date TEXT,
            status TEXT,
            expires_on TEXT,
            equipment_class TEXT,
            fingerprint TEXT UNIQUE,
            first_seen TEXT,
            last_updated TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # Create crawl history table
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
    
    # Create index for faster lookups
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_grantee_code ON fcc_records(grantee_code)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_fcc_id ON fcc_records(fcc_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_fingerprint ON fcc_records(fingerprint)
    """)
    
    conn.commit()
    conn.close()
    
    logger.info(f"Database initialized at {DB_PATH}")


def save_records(records: list[FCCRecord], grantee_code: str):
    """Save or update records in database."""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    now = datetime.utcnow().isoformat()
    
    for record in records:
        fingerprint = record.fingerprint()
        
        # Check if record exists
        cursor.execute(
            "SELECT id, last_updated FROM fcc_records WHERE fingerprint = ?",
            (fingerprint,)
        )
        existing = cursor.fetchone()
        
        if existing:
            # Update existing record
            cursor.execute("""
                UPDATE fcc_records 
                SET product_code = ?, applicant_name = ?, product_name = ?,
                    certification_date = ?, status = ?, expires_on = ?,
                    equipment_class = ?, last_updated = ?
                WHERE fingerprint = ?
            """, (
                record.product_code, record.applicant_name, record.product_name,
                record.certification_date, record.status, record.expires_on,
                record.equipment_class, now, fingerprint
            ))
        else:
            # Insert new record
            cursor.execute("""
                INSERT INTO fcc_records (
                    fcc_id, grantee_code, product_code, applicant_name,
                    product_name, certification_date, status, expires_on,
                    equipment_class, fingerprint, first_seen, last_updated
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.fcc_id, record.grantee_code, record.product_code,
                record.applicant_name, record.product_name, record.certification_date,
                record.status, record.expires_on, record.equipment_class,
                fingerprint, now, now
            ))
    
    conn.commit()
    conn.close()
    
    logger.info(f"Saved {len(records)} records for grantee {grantee_code}")


def get_all_records() -> dict[str, list[FCCRecord]]:
    """Get all current records grouped by grantee code."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT * FROM fcc_records 
        WHERE grantee_code IN ({})
        ORDER BY grantee_code, certification_date DESC
    """.format(",".join("?" * len(GRANTEE_CODES))), GRANTEE_CODES)
    
    rows = cursor.fetchall()
    conn.close()
    
    # Group by grantee code
    result = {code: [] for code in GRANTEE_CODES}
    for row in rows:
        record = FCCRecord(
            fcc_id=row["fcc_id"],
            grantee_code=row["grantee_code"],
            product_code=row["product_code"],
            applicant_name=row["applicant_name"],
            product_name=row["product_name"],
            certification_date=row["certification_date"],
            status=row["status"],
            expires_on=row["expires_on"],
            equipment_class=row["equipment_class"]
        )
        result[row["grantee_code"]].append(record)
    
    return result


def get_new_records_since(timestamp: str) -> list[FCCRecord]:
    """Get records added since a specific timestamp."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT * FROM fcc_records 
        WHERE first_seen > ? AND grantee_code IN ({})
        ORDER BY first_seen DESC
    """.format(",".join("?" * len(GRANTEE_CODES))), [timestamp] + GRANTEE_CODES)
    
    rows = cursor.fetchall()
    conn.close()
    
    return [
        FCCRecord(
            fcc_id=row["fcc_id"],
            grantee_code=row["grantee_code"],
            product_code=row["product_code"],
            applicant_name=row["applicant_name"],
            product_name=row["product_name"],
            certification_date=row["certification_date"],
            status=row["status"],
            expires_on=row["expires_on"],
            equipment_class=row["equipment_class"]
        )
        for row in rows
    ]


def log_crawl(grantee_code: str, record_count: int, errors: list[str]):
    """Log crawl operation to history."""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    timestamp = datetime.utcnow().isoformat()
    errors_json = json.dumps(errors) if errors else None
    
    cursor.execute("""
        INSERT INTO crawl_history (grantee_code, crawl_timestamp, record_count, errors)
        VALUES (?, ?, ?, ?)
    """, (grantee_code, timestamp, record_count, errors_json))
    
    conn.commit()
    conn.close()


# ============================================================================
# API Client
# ============================================================================

class SpiderCloudClient:
    """SpiderCloud API client for FCC data."""
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = SPIDERCLOUD_BASE_URL
        self._session: Optional[httpx.Client] = None
    
    @property
    def session(self) -> httpx.Client:
        if self._session is None:
            self._session = httpx.Client(
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": "NERV-FCC-Monitor/1.0",
                    "Accept": "application/json"
                }
            )
        return self._session
    
    def search_by_grantee(self, grantee_code: str, limit: int = 500) -> list[FCCRecord]:
        """
        Search FCC records by grantee code.
        
        SpiderCloud API endpoint for FCC equipment authorization search.
        """
        url = f"{self.base_url}/fcc/search"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        params = {
            "grantee_code": grantee_code.upper(),
            "limit": limit
        }
        
        try:
            logger.info(f"Fetching FCC data for grantee: {grantee_code}")
            response = self.session.get(url, headers=headers, params=params)
            response.raise_for_status()
            
            data = response.json()
            return self._parse_response(data, grantee_code)
            
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error for {grantee_code}: {e.response.status_code}")
            if e.response.status_code == 401:
                logger.error("Invalid API key - check SPIDERCLOUD_API_KEY")
            return []
        except httpx.RequestError as e:
            logger.error(f"Request error for {grantee_code}: {e}")
            return []
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error for {grantee_code}: {e}")
            return []
    
    def _parse_response(self, data: dict, grantee_code: str) -> list[FCCRecord]:
        """Parse SpiderCloud API response into FCCRecord objects."""
        records = []
        
        # Handle various response formats
        items = data.get("results", data.get("data", data.get("items", [])))
        
        if not items:
            logger.warning(f"No results in response for {grantee_code}")
            return records
        
        for item in items:
            try:
                record = FCCRecord(
                    fcc_id=item.get("fcc_id", item.get("fcc_id_number", "")),
                    grantee_code=item.get("grantee_code", grantee_code),
                    product_code=item.get("product_code", item.get("product_code_number", "")),
                    applicant_name=item.get("applicant_name", item.get("applicant", "")),
                    product_name=item.get("product_name", item.get("device_name", "")),
                    certification_date=item.get("certification_date", item.get("cert_date", "")),
                    status=item.get("status", "Granted"),
                    expires_on=item.get("expires_on", item.get("expiration_date")),
                    equipment_class=item.get("equipment_class", item.get("equipment_class_code"))
                )
                records.append(record)
            except Exception as e:
                logger.warning(f"Failed to parse record: {e}")
                continue
        
        logger.info(f"Parsed {len(records)} records for {grantee_code}")
        return records
    
    def close(self):
        """Close HTTP session."""
        if self._session:
            self._session.close()
            self._session = None


# ============================================================================
# Telegram Notifications
# ============================================================================

class TelegramNotifier:
    """Send Telegram notifications for new FCC records."""
    
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._session: Optional[httpx.Client] = None
    
    @property
    def session(self) -> httpx.Client:
        if self._session is None:
            self._session = httpx.Client(timeout=10.0)
        return self._session
    
    def is_configured(self) -> bool:
        """Check if Telegram is properly configured."""
        return bool(self.bot_token and self.chat_id)
    
    def send_new_records_notification(self, records: list[FCCRecord]) -> bool:
        """
        Send notification about new FCC records.
        Only sends for NEW entries as required.
        """
        if not self.is_configured():
            logger.warning("Telegram not configured, skipping notification")
            return False
        
        if not records:
            logger.info("No new records to notify about")
            return False
        
        # Build message
        message = self._build_message(records)
        
        return self._send_message(message)
    
    def _build_message(self, records: list[FCCRecord]) -> str:
        """Build Telegram message for new records."""
        # Group by grantee code
        by_grantee: dict[str, list[FCCRecord]] = {}
        for record in records:
            if record.grantee_code not in by_grantee:
                by_grantee[record.grantee_code] = []
            by_grantee[record.grantee_code].append(record)
        
        lines = [
            "🔔 <b>FCC New Equipment Alert</b>",
            f"📅 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
            f"✨ <b>{len(records)} new authorization(s)</b>",
            ""
        ]
        
        for grantee_code, recs in by_grantee.items():
            lines.append(f"📡 <b>{grantee_code}</b> ({len(recs)} new)")
            
            # Show up to 5 records per grantee
            for rec in recs[:5]:
                lines.append(f"  • {rec.fcc_id}: {rec.product_name[:40]}")
                lines.append(f"    Applicant: {rec.applicant_name[:30]}")
                lines.append(f"    Certified: {rec.certification_date}")
            
            if len(recs) > 5:
                lines.append(f"  ... and {len(recs) - 5} more")
            lines.append("")
        
        lines.append("— NERV FCC Monitor")
        
        return "\n".join(lines)
    
    def _send_message(self, text: str) -> bool:
        """Send message via Telegram API."""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        
        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            
            logger.info(f"Telegram notification sent successfully")
            return True
            
        except httpx.HTTPStatusError as e:
            logger.error(f"Telegram API error: {e.response.status_code}")
            if e.response.status_code == 401:
                logger.error("Invalid bot token")
            elif e.response.status_code == 400:
                logger.error("Invalid chat_id")
            return False
        except httpx.RequestError as e:
            logger.error(f"Telegram request error: {e}")
            return False
    
    def close(self):
        """Close HTTP session."""
        if self._session:
            self._session.close()
            self._session = None


# ============================================================================
# State Management
# ============================================================================

def load_last_state() -> dict:
    """Load last known state from JSON file."""
    if LAST_STATE_FILE.exists():
        try:
            with open(LAST_STATE_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    
    return {
        "last_crawl": None,
        "last_notification_count": 0
    }


def save_last_state(state: dict):
    """Save last known state to JSON file."""
    with open(LAST_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ============================================================================
# Main Crawler
# ============================================================================

class FCCMonitorCrawler:
    """Main crawler for FCC monitoring."""
    
    def __init__(self, api_key: str, telegram_token: str = "", telegram_chat_id: str = ""):
        self.client = SpiderCloudClient(api_key)
        self.notifier = TelegramNotifier(telegram_token, telegram_chat_id)
        self.state = load_last_state()
    
    def run(self):
        """Execute a single crawl cycle."""
        logger.info("=" * 60)
        logger.info("FCC Monitor - Starting crawl cycle")
        logger.info("=" * 60)
        
        all_new_records = []
        errors = []
        
        for grantee_code in GRANTEE_CODES:
            logger.info(f"Processing grantee code: {grantee_code}")
            
            try:
                # Fetch records from SpiderCloud
                records = self.client.search_by_grantee(grantee_code)
                
                if records:
                    # Save to database
                    save_records(records, grantee_code)
                    
                    # Check for new records
                    new_for_grantee = self._get_new_records(records, grantee_code)
                    all_new_records.extend(new_for_grantee)
                    
                    logger.info(f"  Found {len(records)} total, {len(new_for_grantee)} new")
                else:
                    logger.warning(f"  No records found for {grantee_code}")
                
                # Log crawl history
                log_crawl(grantee_code, len(records), errors if errors else [])
                
            except Exception as e:
                error_msg = f"Error processing {grantee_code}: {e}"
                logger.error(error_msg)
                errors.append(error_msg)
                log_crawl(grantee_code, 0, [error_msg])
        
        # Send notifications for new records only
        if all_new_records:
            logger.info(f"Sending notification for {len(all_new_records)} new records")
            self.notifier.send_new_records_notification(all_new_records)
        
        # Update state
        self.state["last_crawl"] = datetime.utcnow().isoformat()
        self.state["last_notification_count"] = len(all_new_records)
        save_last_state(self.state)
        
        # Summary
        logger.info("=" * 60)
        logger.info(f"Crawl complete. Total: {sum(len(self.client.search_by_grantee(c)) for c in GRANTEE_CODES)}, New: {len(all_new_records)}")
        logger.info("=" * 60)
        
        return all_new_records
    
    def _get_new_records(self, current_records: list[FCCRecord], grantee_code: str) -> list[FCCRecord]:
        """Identify new records not previously seen."""
        conn = sqlite3.connect(str(DB_PATH))
        cursor = conn.cursor()
        
        new_records = []
        
        for record in current_records:
            fingerprint = record.fingerprint()
            
            cursor.execute(
                "SELECT id FROM fcc_records WHERE fingerprint = ?",
                (fingerprint,)
            )
            
            if cursor.fetchone() is None:
                new_records.append(record)
        
        conn.close()
        return new_records
    
    def cleanup(self):
        """Clean up resources."""
        self.client.close()
        self.notifier.close()


# ============================================================================
# Entry Point
# ============================================================================

def setup_logging():
    """Configure logging."""
    LOG_DIR = PROJECT_ROOT / "logs"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    logger.remove()
    logger.add(
        sys.stderr,
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"
    )
    logger.add(
        LOG_DIR / "fcc_monitor.log",
        rotation="10 MB",
        retention="30 days",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}"
    )


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="FCC Monitoring Crawler")
    parser.add_argument(
        "--daemon", 
        action="store_true",
        help="Run as daemon with daily scheduler"
    )
    parser.add_argument(
        "--api-key",
        default=SPIDERCLOUD_API_KEY,
        help="SpiderCloud API key"
    )
    parser.add_argument(
        "--telegram-token",
        default=TELEGRAM_BOT_TOKEN,
        help="Telegram bot token"
    )
    parser.add_argument(
        "--telegram-chat-id",
        default=TELEGRAM_CHAT_ID,
        help="Telegram chat ID"
    )
    
    args = parser.parse_args()
    
    # Setup
    setup_logging()
    init_database()
    
    if args.daemon:
        run_daemon(args)
    else:
        run_once(args)


def run_once(args):
    """Run a single crawl cycle."""
    crawler = FCCMonitorCrawler(
        api_key=args.api_key,
        telegram_token=args.telegram_token,
        telegram_chat_id=args.telegram_chat_id
    )
    
    try:
        crawler.run()
    finally:
        crawler.cleanup()


def run_daemon(args):
    """Run as daemon with scheduler."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    
    logger.info("Starting FCC Monitor in daemon mode")
    logger.info(f"Schedule: Daily at 14:00 UTC")
    
    scheduler = BlockingScheduler(timezone="UTC")
    
    scheduler.add_job(
        lambda: run_once(args),
        "cron",
        hour=14,
        minute=0,
        id="daily_crawl"
    )
    
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
