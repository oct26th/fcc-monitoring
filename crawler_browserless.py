#!/usr/bin/env python3
"""
FCC Monitor - Browserless REST API Crawler (No External Dependencies)
Uses Browserless cloud service (/function endpoint) via standard urllib to scrape
FCC.gov equipment authorizations. Bypasses Akamai WAF protection.

No pip install required. Works with standard Python 3.
"""

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from urllib import request, parse, error
from typing import Optional, List, Dict, Any

# ============================================================================
# Configuration & Logging
# ============================================================================

PROJECT_ROOT = Path(__file__).parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = DATA_DIR / "fcc_monitor.db"
LAST_STATE_FILE = DATA_DIR / "last_state.json"
LOG_DIR = PROJECT_ROOT / "logs"

# Grantee codes to monitor
GRANTEE_CODES = [
    "UZ7", "HD5", "U4F", "U4G", 
    "V2X", "SS4", "HLE", 
    "2AOJL", "2AC6A", "2AR9L"
]

# Browserless configuration
BROWSERLESS_API_KEY = os.getenv("BROWSERLESS_API_KEY", "")
BROWSERLESS_REGION = os.getenv("BROWSERLESS_REGION", "sfo")  # sfo, lon, ams

# Telegram/Discord configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("fcc_monitor")

def setup_logging(verbose: bool = False):
    """Refine logging setup."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # Update console format
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    root_logger.addHandler(console)
    
    # File handler
    log_file = LOG_DIR / f"fcc_br_rest_{datetime.now().strftime('%Y-%m-%d')}.log"
    file_h = logging.FileHandler(log_file)
    file_h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s"))
    root_logger.addHandler(file_h)
    
    logger.info(f"NERV Logging active at {logging.getLevelName(level)} level")

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
    product_description: str
    grant_date: str
    filing_date: str
    application_type: str
    status: str = "Granted"
    equipment_class: Optional[str] = None
    
    def fingerprint(self) -> str:
        """Generate unique fingerprint for this record."""
        return hashlib.sha256(
            f"{self.fcc_id}:{self.grantee_code}:{self.product_code}:{self.application_type}".encode()
        ).hexdigest()[:16]

# ============================================================================
# Database Functions
# ============================================================================

def init_database():
    """Initialize the SQLite database (shared with other crawlers)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(str(DB_PATH))
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

    # Ensure metadata columns exist
    cursor.execute("PRAGMA table_info(fcc_records)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'product_description' not in columns:
        cursor.execute("ALTER TABLE fcc_records ADD COLUMN product_description TEXT")
    if 'grant_date' not in columns:
        cursor.execute("ALTER TABLE fcc_records ADD COLUMN grant_date TEXT")
    if 'filing_date' not in columns:
        cursor.execute("ALTER TABLE fcc_records ADD COLUMN filing_date TEXT")
    if 'application_type' not in columns:
        cursor.execute("ALTER TABLE fcc_records ADD COLUMN application_type TEXT")

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

def save_record(record: FCCRecord) -> bool:
    """Save record, return True if new."""
    conn = sqlite3.connect(str(DB_PATH))
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
                certification_date = COALESCE(?, certification_date),
                application_type = COALESCE(?, application_type),
                product_description = COALESCE(?, product_description),
                product_name = COALESCE(?, product_name),
                last_updated = ?
            WHERE fingerprint = ?
        """, (record.grant_date, record.filing_date, record.grant_date, record.application_type,
              record.product_description, record.product_description, now, fingerprint))
        conn.commit()
        conn.close()
        return False
    else:
        cursor.execute("""
            INSERT INTO fcc_records (
                fcc_id, grantee_code, product_code, applicant_name,
                product_description, product_name, grant_date, filing_date, certification_date, application_type,
                status, equipment_class, fingerprint, first_seen, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record.fcc_id, record.grantee_code, record.product_code, record.applicant_name,
            record.product_description, record.product_description, record.grant_date, record.filing_date,
            record.grant_date, record.application_type, record.status, record.equipment_class,
            fingerprint, now, now
        ))
        conn.commit()
        conn.close()
        return True

# ============================================================================
# Notification Functions (urllib based)
# ============================================================================

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }).encode()
    try:
        req = request.Request(url, data=data)
        with request.urlopen(req, timeout=10) as response:
            if response.status == 200: logger.info("Telegram sent")
    except Exception as e:
        logger.error(f"Telegram failed: {e}")

def send_discord(message: str):
    if not DISCORD_WEBHOOK_URL: return
    data = json.dumps({"content": message, "username": "NERV Monitor"}).encode('utf-8')
    try:
        req = request.Request(DISCORD_WEBHOOK_URL, data=data, headers={'Content-Type': 'application/json'})
        with request.urlopen(req, timeout=10) as response:
            if response.status in [200, 204]: logger.info("Discord sent")
    except Exception as e:
        logger.error(f"Discord failed: {e}")

# ============================================================================
# Crawler (Browserless REST)
# ============================================================================

BROWSERLESS_JS_TEMPLATE = """
export default async ({ page, context }) => {
  const granteeCode = context.granteeCode;
  const searchUrl = 'https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm';
  
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
  await page.setViewport({ width: 1280, height: 800 });

  try {
    await page.goto(searchUrl, { waitUntil: 'networkidle2', timeout: 60000 });
    
    // Check for redirection or block
    const title = await page.title();
    if (title.toLowerCase().includes('access denied')) throw new Error('Blocked by Akamai');

    // Fill grantee code
    await page.waitForSelector('input[name="grantee_code"]', { timeout: 15000 });
    await page.type('input[name="grantee_code"]', granteeCode, { delay: 100 });
    
    // Set max records to 500 to fetch newer records instead of only old ones
    await page.evaluate(() => {
      const showRecs = document.querySelector('input[name="show_records"]');
      if (showRecs) showRecs.value = '';
    });
    await page.type('input[name="show_records"]', '500', { delay: 50 });
    
    // Click search
    await Promise.all([
      page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 60000 }),
      page.click('input[type="submit"]')
    ]);

    // Parse results
    const data = await page.evaluate(() => {
      const tables = Array.from(document.querySelectorAll('table'));
      let resultsTable = null;
      let headers = [];
      
      for (const table of tables) {
        const firstRow = table.querySelector('tr');
        if (!firstRow) continue;
        const headText = firstRow.innerText.toLowerCase();
        if (headText.includes('fcc id') || headText.includes('grant date') || headText.includes('final action')) {
          resultsTable = table;
          headers = Array.from(firstRow.querySelectorAll('th, td')).map(el => el.innerText.trim().replace(/\\n/g, ' ').toLowerCase());
          break;
        }
      }
      
      if (!resultsTable) return { error: 'Results table not found' };
      
      const colMap = {};
      headers.forEach((h, i) => {
        if (h.includes('fcc') && h.includes('id')) colMap.fcc_id = i;
        else if ((h.includes('final') && h.includes('action')) || (h.includes('grant') && h.includes('date'))) colMap.grant_date = i;
        else if (h.includes('filing') && h.includes('date')) colMap.filing_date = i;
        else if (h.includes('applicant') || h.includes('name')) {
            if (!colMap.applicant) colMap.applicant = i;
        }
        else if (h.includes('product') || h.includes('description')) colMap.product_desc = i;
        else if (h.includes('purpose') || h.includes('type')) colMap.app_type = i;
        else if (h.includes('city')) colMap.city = i;
        else if (h.includes('state')) colMap.state = i;
      });
      
      const rows = Array.from(resultsTable.querySelectorAll('tr')).slice(1);
      const records = rows.map(row => {
        const cells = Array.from(row.querySelectorAll('td'));
        if (cells.length < 3) return null;
        const rect = {};
        for (const [key, idx] of Object.entries(colMap)) {
          if (cells[idx]) rect[key] = cells[idx].innerText.trim();
        }
        return rect;
      }).filter(r => r && r.fcc_id);
      
      return { records, headers };
    });

    return data;
  } catch (e) {
    return { error: e.message };
  }
}
"""

def crawl_grantee_rest(grantee_code: str):
    """Crawl a grantee via Browserless REST /function endpoint."""
    if not BROWSERLESS_API_KEY:
        logger.error("BROWSERLESS_API_KEY missing!")
        return []
    
    url = f"https://production-{BROWSERLESS_REGION}.browserless.io/function?token={BROWSERLESS_API_KEY}"
    
    # Prepare script and context
    payload = {
        "code": BROWSERLESS_JS_TEMPLATE,
        "context": {"granteeCode": grantee_code}
    }
    
    data = json.dumps(payload).encode('utf-8')
    headers = {
        'Content-Type': 'application/json',
        'Cache-Control': 'no-cache'
    }
    
    logger.info(f"Dispatching NERV probe for {grantee_code} to Browserless cloud...")
    
    start_time = time.time()
    try:
        req = request.Request(url, data=data, headers=headers)
        with request.urlopen(req, timeout=120) as response:
            result = json.loads(response.read().decode())
            
            if "error" in result:
                logger.error(f"Browserless error: {result['error']}")
                return []
            
            raw_recs = result.get("records", [])
            logger.info(f"Probe successful. Captured {len(raw_recs)} records for {grantee_code}")
            
            records = []
            for item in raw_recs:
                fcc_id = item.get("fcc_id", "")
                if not fcc_id: continue
                
                # Cleanup ID (strip spaces)
                fcc_id = fcc_id.replace(" ", "")
                
                # Product code derivation
                p_code = fcc_id[len(grantee_code):] if fcc_id.startswith(grantee_code) else fcc_id
                
                # Applicant formatting
                name = item.get("applicant", "Unknown")
                city = item.get("city")
                state = item.get("state")
                formatted_name = name
                if city and state and city.lower() not in name.lower():
                    formatted_name = f"{name} ({city}, {state})"
                
                records.append(FCCRecord(
                    fcc_id=fcc_id,
                    grantee_code=grantee_code,
                    product_code=p_code,
                    applicant_name=formatted_name,
                    product_description=item.get("product_desc", ""),
                    grant_date=item.get("grant_date", ""),
                    filing_date=item.get("filing_date", ""),
                    application_type=item.get("app_type", "")
                ))
            return records
            
    except error.HTTPError as e:
        error_body = e.read().decode()
        logger.error(f"REST call failed: {e} - Body: {error_body}")
        return []
    except Exception as e:
        logger.error(f"REST call failed: {e}")
        return []

# ============================================================================
# Main Logic
# ============================================================================

def run_crawl(grantee_codes=None, notify=True):
    if not grantee_codes: grantee_codes = GRANTEE_CODES
    init_database()
    
    all_new = []
    for code in grantee_codes:
        logger.info(f"=== Objective: {code} ===")
        records = crawl_grantee_rest(code)
        
        new_count = 0
        for r in records:
            if save_record(r):
                all_new.append(r)
                new_count += 1
        
        logger.info(f"Status: {len(records)} found, {new_count} new entries archived.")
        
        # Save simple history
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("INSERT INTO crawl_history (grantee_code, crawl_timestamp, record_count) VALUES (?, ?, ?)",
                     (code, datetime.now().isoformat(), len(records)))
        conn.commit()
        conn.close()
        
        if code != grantee_codes[-1]: time.sleep(1)

    if notify and all_new:
        msg = f"📡 <b>NERV FCC Monitor - 新型號授權回報</b>\n\n抓取到 {len(all_new)} 筆新紀錄：\n"
        for r in all_new[:10]:
            msg += f"• <code>{r.fcc_id}</code>: {r.applicant_name[:30]}\n"
        send_telegram(msg)
        send_discord(msg)

    logger.info(f"Operation complete. Synchronization Rate: {len(all_new)} new targets.")
    return all_new

def main():
    parser = argparse.ArgumentParser(description="NERV Browserless REST Crawler")
    parser.add_argument("--grantee", "-g", help="Target grantee")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    
    targets = [args.grantee] if args.grantee else GRANTEE_CODES
    run_crawl(targets, notify=not args.no_notify)

if __name__ == "__main__":
    main()
