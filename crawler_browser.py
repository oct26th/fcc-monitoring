#!/usr/bin/env python3
"""
FCC Monitor - Browser-Based Crawler
Uses Playwright for browser automation to scrape FCC.gov equipment authorizations.

This crawler bypasses anti-bot protection by using a real browser instance.

Usage:
    python crawler_browser.py              # Run once
    python crawler_browser.py --daemon     # Run as daemon
    python crawler_browser.py --grantee UZ7 # Test with single grantee

Environment Variables:
    PLAYWRIGHT_BROWSER: Browser to use (chromium, firefox, webkit) - default: chromium
    TELEGRAM_BOT_TOKEN: Telegram bot token for notifications
    TELEGRAM_CHAT_ID: Target chat ID for notifications
    DISCORD_WEBHOOK_URL: Discord webhook for notifications
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import sqlite3
from loguru import logger

# Try to import Playwright - handle if not installed
try:
    from playwright.async_api import async_playwright, Playwright, Browser, Page, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

# Try to import stealth plugin
try:
    from playwright_stealth import stealth_async
    STEALTH_AVAILABLE = True
except ImportError:
    STEALTH_AVAILABLE = False
    logger.warning("playwright-stealth not installed. Run: pip install playwright-stealth")

# Try to import SpiderCloud for fallback
SPIDERCLOUD_AVAILABLE = False
try:
    import httpx
    SPIDERCLOUD_AVAILABLE = True
except ImportError:
    logger.warning("httpx not available for SpiderCloud fallback")

# ============================================================================
# Configuration
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

# FCC Search URL
FCC_SEARCH_URL = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm"

# Telegram/Discord configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

# Browser configuration
PLAYWRIGHT_BROWSER = os.getenv("PLAYWRIGHT_BROWSER", "chromium")
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

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
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'FCCRecord':
        return cls(
            fcc_id=data.get("fcc_id", ""),
            grantee_code=data.get("grantee_code", ""),
            product_code=data.get("product_code", ""),
            applicant_name=data.get("applicant_name", ""),
            product_description=data.get("product_description", ""),
            grant_date=data.get("grant_date", ""),
            filing_date=data.get("filing_date", ""),
            application_type=data.get("application_type", ""),
            status=data.get("status", "Granted"),
            equipment_class=data.get("equipment_class")
        )
    
    def fingerprint(self) -> str:
        """Generate unique fingerprint for this record."""
        return hashlib.sha256(
            f"{self.fcc_id}:{self.grantee_code}:{self.product_code}:{self.application_type}".encode()
        ).hexdigest()[:16]


@dataclass
class CrawlResult:
    """Result of a crawl operation."""
    grantee_code: str
    records: list = field(default_factory=list)
    new_records: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    duration_seconds: float = 0.0

# ============================================================================
# Database Functions
# ============================================================================

def init_database():
    """Initialize the SQLite database with required tables."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # Check existing columns in fcc_records
    cursor.execute("PRAGMA table_info(fcc_records)")
    existing_cols = [row[1] for row in cursor.fetchall()]
    
    # Main FCC records table - add columns if they don't exist
    if 'product_description' not in existing_cols:
        cursor.execute("ALTER TABLE fcc_records ADD COLUMN product_description TEXT")
    if 'grant_date' not in existing_cols:
        cursor.execute("ALTER TABLE fcc_records ADD COLUMN grant_date TEXT")
    if 'filing_date' not in existing_cols:
        cursor.execute("ALTER TABLE fcc_records ADD COLUMN filing_date TEXT")
    if 'application_type' not in existing_cols:
        cursor.execute("ALTER TABLE fcc_records ADD COLUMN application_type TEXT")
    
    # Ensure fingerprint column exists
    if 'fingerprint' not in existing_cols:
        cursor.execute("ALTER TABLE fcc_records ADD COLUMN fingerprint TEXT")
    if 'first_seen' not in existing_cols:
        cursor.execute("ALTER TABLE fcc_records ADD COLUMN first_seen TEXT")
    if 'last_updated' not in existing_cols:
        cursor.execute("ALTER TABLE fcc_records ADD COLUMN last_updated TEXT")
    
    # Create indexes if they don't exist
    try:
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fcc_id ON fcc_records(fcc_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_grantee_code ON fcc_records(grantee_code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_fingerprint ON fcc_records(fingerprint)")
    except:
        pass
    
    # Crawl history table - ensure it exists
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
    logger.info(f"Database initialized at {DB_PATH}")


def save_record(record: FCCRecord) -> bool:
    """Save a record to the database. Returns True if new, False if existing."""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    fingerprint = record.fingerprint()
    now = datetime.now().isoformat()
    
    # Check if record already exists
    cursor.execute(
        "SELECT id, last_updated FROM fcc_records WHERE fingerprint = ?",
        (fingerprint,)
    )
    existing = cursor.fetchone()
    
    if existing:
        # Update existing record
        cursor.execute("""
            UPDATE fcc_records 
            SET grant_date = COALESCE(?, grant_date),
                filing_date = COALESCE(?, filing_date),
                application_type = COALESCE(?, application_type),
                product_description = COALESCE(?, product_description),
                product_name = COALESCE(?, product_name),
                last_updated = ?
            WHERE fingerprint = ?
        """, (record.grant_date, record.filing_date, record.application_type,
              record.product_description, record.product_description, now, fingerprint))
        conn.commit()
        conn.close()
        return False
    else:
        # Insert new record
        # Parse product code from FCC ID (grantee_code + product_code)
        product_code = ""
        if record.fcc_id and len(record.fcc_id) > len(record.grantee_code):
            product_code = record.fcc_id[len(record.grantee_code):]
        
        cursor.execute("""
            INSERT INTO fcc_records (
                fcc_id, grantee_code, product_code, applicant_name,
                product_description, product_name, grant_date, filing_date, application_type,
                status, equipment_class, fingerprint, first_seen, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record.fcc_id, record.grantee_code, product_code, record.applicant_name,
            record.product_description, record.product_description, record.grant_date, record.filing_date,
            record.application_type, record.status, record.equipment_class,
            fingerprint, now, now
        ))
        conn.commit()
        conn.close()
        return True


def get_all_records(grantee_code: str = None) -> list:
    """Get all records from database, optionally filtered by grantee code."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    if grantee_code:
        cursor.execute(
            "SELECT * FROM fcc_records WHERE grantee_code = ? ORDER BY grant_date DESC",
            (grantee_code,)
        )
    else:
        cursor.execute("SELECT * FROM fcc_records ORDER BY grant_date DESC")
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def save_crawl_history(result: CrawlResult):
    """Save crawl result to history."""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # Check if new schema columns exist, if not use legacy schema
    cursor.execute("PRAGMA table_info(crawl_history)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if 'records_count' in columns:
        # New schema
        cursor.execute("""
            INSERT INTO crawl_history (
                grantee_code, records_count, new_records_count,
                duration_seconds, timestamp, errors
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            result.grantee_code,
            len(result.records),
            len(result.new_records),
            result.duration_seconds,
            result.timestamp,
            json.dumps(result.errors) if result.errors else None
        ))
    else:
        # Legacy schema
        cursor.execute("""
            INSERT INTO crawl_history (
                grantee_code, crawl_timestamp, record_count, errors
            ) VALUES (?, ?, ?, ?)
        """, (
            result.grantee_code,
            result.timestamp,
            len(result.records),
            json.dumps(result.errors) if result.errors else None
        ))
    
    conn.commit()
    conn.close()


def get_last_state() -> dict:
    """Load last run state."""
    if LAST_STATE_FILE.exists():
        try:
            with open(LAST_STATE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load last state: {e}")
    return {}


def save_last_state(state: dict):
    """Save last run state."""
    with open(LAST_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)


# ============================================================================
# Browser Automation
# ============================================================================

class FCCBrowserCrawler:
    """Browser-based FCC crawler using Playwright."""
    
    def __init__(self, browser_type: str = "chromium", headless: bool = True):
        self.browser_type = browser_type
        self.headless = headless
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        
    async def __aenter__(self):
        """Setup browser."""
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright is not installed. Run: pip install playwright && playwright install")
        
        self.playwright = await async_playwright().start()
        
        # Launch browser with anti-detection measures
        if self.browser_type == "firefox":
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless,
                firefox_user_prefs={
                    "media.navigator.streams.fake": True,
                    "media.navigator.enabled": True,
                }
            )
        elif self.browser_type == "webkit":
            self.browser = await self.playwright.webkit.launch(
                headless=self.headless,
                args=['--no-sandbox']
            )
        else:
            # Chromium with extensive anti-detection args
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-web-security',
                    '--disable-features=IsolateOrigins,site-per-process',
                    '--allow-running-insecure-content',
                    '--disable-webgl',
                    '--disable-popup-blocking',
                    '--disable-infobars',
                    '--disable-speech-recognition',
                    '--disable-speech-api',
                    '--disable-features=AudioService',
                    '--no-first-run',
                    '--no-zygote',
                    '--disable-gpu',
                ]
            )
        
        # Create context with realistic viewport and settings
        self.context = await self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            locale='en-US',
            timezone_id='America/New_York',
            permissions=['geolocation'],
            ignore_https_errors=True,
        )
        
        # Add extra HTTP headers to appear more legitimate
        await self.context.set_extra_http_headers({
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
        })
        
        # Create new page
        self.page = await self.context.new_page()
        
        # Apply stealth mode if available
        if STEALTH_AVAILABLE:
            await stealth_async(self.page)
            logger.info("Applied stealth mode to browser")
        
        # Set longer timeout for slow FCC pages
        self.page.set_default_timeout(120000)
        
        # Listen for console messages for debugging
        self.page.on("console", lambda msg: logger.debug(f"Browser console: {msg.type} - {msg.text}") if msg.type == "error" else None)
        
        logger.info(f"Browser started: {self.browser_type} (headless={self.headless})")
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Cleanup browser."""
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Browser closed")
    
    async def search_grantee(self, grantee_code: str, max_retries: int = 3) -> list[FCCRecord]:
        """Search for a grantee code and return parsed results."""
        records = []
        last_error = None
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Navigating to FCC search page for {grantee_code} (attempt {attempt + 1}/{max_retries})...")
                
                # Navigate to FCC search page with network idle to handle redirects
                response = await self.page.goto(FCC_SEARCH_URL, wait_until="networkidle", timeout=90000)
                
                # Check for HTTP errors
                if response and response.status >= 400:
                    logger.warning(f"HTTP error {response.status} on attempt {attempt + 1}")
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                
                # The ColdFusion site may have a meta refresh redirect
                # Wait a bit and check if we were redirected
                await asyncio.sleep(2)
                
                # Handle potential meta refresh redirect
                try:
                    meta_refresh = await self.page.query_selector('meta[http-equiv="refresh"]')
                    if meta_refresh:
                        content = await meta_refresh.get_attribute('content')
                        if content:
                            # Extract URL from meta refresh (format: "0;url=...")
                            if 'url=' in content.lower():
                                redirect_url = content.split('url=')[-1].strip()
                                logger.info(f"Following meta refresh to: {redirect_url}")
                                await self.page.goto(redirect_url, wait_until="networkidle", timeout=90000)
                except:
                    pass
                
                # Check for access denied
                page_title = await self.page.title()
                if 'access denied' in page_title.lower() or 'forbidden' in page_title.lower():
                    logger.warning(f"Access denied on attempt {attempt + 1}, retrying...")
                    # Wait before retry with exponential backoff
                    await asyncio.sleep(5 * (attempt + 1))
                    # Reload the page
                    await self.page.reload(wait_until="networkidle")
                    continue
                
                # Wait for the form to be ready (ColdFusion forms can be slow)
                # Try multiple selectors to find the search form
                form_found = False
                for selector in ['form', 'table', '#searchForm', 'form[name="search"]']:
                    try:
                        await self.page.wait_for_selector(selector, timeout=10000)
                        form_found = True
                        logger.debug(f"Found form element: {selector}")
                        break
                    except:
                        continue
                
                if not form_found:
                    # Dump page HTML for debugging
                    html = await self.page.content()
                    logger.error(f"Could not find search form. Page title: {await self.page.title()}")
                    logger.debug(f"Page URL: {self.page.url}")
                    logger.debug(f"Page HTML (first 2000 chars): {html[:2000]}")
                    raise Exception("Could not find search form")
                
                # Try multiple selector strategies for the grantee code input
                grantee_input = None
                for selector in [
                    'input[name="grantee_code"]',
                    'input[id="grantee_code"]',
                    'input[name="granteeCode"]', 
                    'input[id="granteeCode"]',
                    'input[placeholder*="Grantee"]',
                    'input[type="text"]:nth-of-type(1)',
                    'input[type="text"]',
                ]:
                    try:
                        grantee_input = await self.page.query_selector(selector)
                        if grantee_input:
                            break
                    except:
                        continue
                
                if not grantee_input:
                    # Dump page HTML for debugging
                    html = await self.page.content()
                    logger.error(f"Could not find grantee code input. Page title: {await self.page.title()}")
                    logger.debug(f"Page HTML (first 2000 chars): {html[:2000]}")
                    raise Exception("Could not find grantee code input field")
                
                # Clear and fill the input with human-like typing
                await grantee_input.fill("")
                await grantee_input.type(grantee_code, delay=100)
                
                # Find and click the search button
                search_button = None
                for selector in [
                    'input[type="submit"][value*="Search"]',
                    'input[type="submit"]',
                    'button[type="submit"]',
                    'button:has-text("Search")',
                    'input[type="button"][value*="Search"]',
                ]:
                    try:
                        search_button = await self.page.query_selector(selector)
                        if search_button:
                            break
                    except:
                        continue
                
                if not search_button:
                    raise Exception("Could not find search button")
                
                logger.info(f"Clicking search for grantee: {grantee_code}")
                await search_button.click()
                
                # Wait for results (either results table or "no results" message)
                await asyncio.sleep(2)  # Brief pause for form submission
                
                # Wait for either results table or no results message
                try:
                    await self.page.wait_for_selector('table, .noResults, .norecords, text=No records found', timeout=30000)
                except Exception as e:
                    logger.warning(f"Timeout waiting for results: {e}")
                
                # Parse the results
                records = await self._parse_results(grantee_code)
                
                logger.info(f"Found {len(records)} records for {grantee_code}")
                
                # If we get here, we succeeded - break out of retry loop
                break
            
            except Exception as e:
                last_error = e
                logger.error(f"Error on attempt {attempt + 1} for {grantee_code}: {e}")
                
                # If this was the last attempt, take screenshot and re-raise
                if attempt == max_retries - 1:
                    try:
                        screenshot_path = LOG_DIR / f"error_{grantee_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                        LOG_DIR.mkdir(parents=True, exist_ok=True)
                        await self.page.screenshot(path=str(screenshot_path))
                        logger.info(f"Screenshot saved: {screenshot_path}")
                    except:
                        pass
                else:
                    await asyncio.sleep(3)
        
        # If we have no records and had an error, raise the last error
        if not records and last_error:
            raise last_error
        elif not records:
            # No records and no error - likely blocked all attempts
            raise Exception(f"Failed to access FCC website after {max_retries} attempts - likely blocked by anti-bot protection")
            
        return records
    
    async def _parse_results(self, grantee_code: str) -> list[FCCRecord]:
        """Parse the results table from the FCC search results page."""
        records = []
        
        try:
            # Find the results table
            table = await self.page.query_selector('table')
            
            if not table:
                logger.info("No results table found")
                return records
            
            # Get all rows from the table
            rows = await table.query_selector_all('tr')
            
            if len(rows) < 2:
                logger.info("No data rows in results table")
                return records
            
            # Parse header row to find column indices
            header_row = rows[0]
            headers = await header_row.query_selector_all('th, td')
            header_texts = []
            for h in headers:
                text = await h.inner_text()
                header_texts.append(text.strip().lower())
            
            logger.debug(f"Table headers: {header_texts}")
            
            # Find column indices
            col_map = {}
            for i, h in enumerate(header_texts):
                if 'fcc' in h and 'id' in h:
                    col_map['fcc_id'] = i
                elif 'product' in h or 'description' in h:
                    col_map['product_desc'] = i
                elif 'grant' in h and 'date' in h:
                    col_map['grant_date'] = i
                elif 'filing' in h and 'date' in h:
                    col_map['filing_date'] = i
                elif 'application' in h or 'type' in h:
                    col_map['app_type'] = i
                elif 'applicant' in h or 'name' in h:
                    col_map['applicant'] = i
            
            # Parse data rows (skip header)
            for row in rows[1:]:
                cells = await row.query_selector_all('td')
                
                if not cells:
                    continue
                
                try:
                    # Extract cell values
                    fcc_id = cells[col_map.get('fcc_id', 0)].inner_text().strip() if col_map.get('fcc_id', 0) < len(cells) else ""
                    product_desc = cells[col_map.get('product_desc', 1)].inner_text().strip() if col_map.get('product_desc', 1) < len(cells) else ""
                    grant_date = cells[col_map.get('grant_date', 2)].inner_text().strip() if col_map.get('grant_date', 2) < len(cells) else ""
                    filing_date = cells[col_map.get('filing_date', 3)].inner_text().strip() if col_map.get('filing_date', 3) < len(cells) else ""
                    app_type = cells[col_map.get('app_type', 4)].inner_text().strip() if col_map.get('app_type', 4) < len(cells) else ""
                    applicant = cells[col_map.get('applicant', 5)].inner_text().strip() if col_map.get('applicant', 5) < len(cells) else ""
                    
                    if fcc_id:
                        # Extract product code from FCC ID
                        product_code = ""
                        if len(fcc_id) > len(grantee_code):
                            product_code = fcc_id[len(grantee_code):]
                        
                        record = FCCRecord(
                            fcc_id=fcc_id,
                            grantee_code=grantee_code,
                            product_code=product_code,
                            applicant_name=applicant,
                            product_description=product_desc,
                            grant_date=grant_date,
                            filing_date=filing_date,
                            application_type=app_type,
                            status="Granted" if grant_date else "Pending"
                        )
                        records.append(record)
                        
                except Exception as e:
                    logger.warning(f"Error parsing row: {e}")
                    continue
                    
        except Exception as e:
            logger.error(f"Error parsing results: {e}")
        
        return records


# ============================================================================
# Notification Functions
# ============================================================================

async def send_telegram_notification(message: str):
    """Send notification via Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured, skipping notification")
        return
    
    try:
        import httpx
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(url, data=data, timeout=10)
            if response.status_code == 200:
                logger.info("Telegram notification sent")
            else:
                logger.error(f"Telegram error: {response.text}")
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")


async def send_discord_notification(message: str):
    """Send notification via Discord webhook."""
    if not DISCORD_WEBHOOK_URL:
        logger.debug("Discord not configured, skipping notification")
        return
    
    try:
        import httpx
        data = {
            "content": message,
            "username": "FCC Monitor"
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(DISCORD_WEBHOOK_URL, data=data, timeout=10)
            if response.status_code in [200, 204]:
                logger.info("Discord notification sent")
            else:
                logger.error(f"Discord error: {response.text}")
    except Exception as e:
        logger.error(f"Failed to send Discord notification: {e}")


async def notify_new_records(new_records: list[FCCRecord]):
    """Send notifications for new FCC records."""
    if not new_records:
        return
    
    # Build notification message
    c2pc_records = [r for r in new_records if 'class ii' in r.application_type.lower() or 'c2pc' in r.application_type.lower()]
    other_records = [r for r in new_records if r not in c2pc_records]
    
    message = "📡 <b>FCC Monitor - New Equipment Authorizations</b>\n\n"
    
    if c2pc_records:
        message += "🔄 <b>Class II Permissive Changes (C2PC):</b>\n"
        for r in c2pc_records[:5]:  # Limit to 5
            message += f"• {r.fcc_id}: {r.product_description[:50]}...\n"
        if len(c2pc_records) > 5:
            message += f"... and {len(c2pc_records) - 5} more\n"
        message += "\n"
    
    if other_records:
        message += "📋 <b>New Equipment:</b>\n"
        for r in other_records[:5]:  # Limit to 5
            message += f"• {r.fcc_id}: {r.product_description[:50]}...\n"
        if len(other_records) > 5:
            message += f"... and {len(other_records) - 5} more\n"
    
    message += f"\nTotal: {len(new_records)} new records"
    
    await send_telegram_notification(message)
    await send_discord_notification(message)


# ============================================================================
# Main Crawl Functions
# ============================================================================

async def crawl_grantee(grantee_code: str, use_fallback: bool = True) -> CrawlResult:
    """Crawl a single grantee code."""
    result = CrawlResult(grantee_code=grantee_code)
    start_time = time.time()
    
    try:
        async with FCCBrowserCrawler(browser_type=PLAYWRIGHT_BROWSER, headless=HEADLESS) as crawler:
            records = await crawler.search_grantee(grantee_code)
            result.records = records
            
            # Save records and detect new ones
            for record in records:
                is_new = save_record(record)
                if is_new:
                    result.new_records.append(record)
    
    except Exception as e:
        logger.error(f"Browser crawl failed for {grantee_code}: {e}")
        
        # Try fallback to SpiderCloud API if enabled
        if use_fallback and SPIDERCLOUD_AVAILABLE:
            logger.info(f"Attempting SpiderCloud API fallback for {grantee_code}")
            try:
                records = await crawl_spidercloud_fallback(grantee_code)
                result.records = records
                
                for record in records:
                    is_new = save_record(record)
                    if is_new:
                        result.new_records.append(record)
                
                result.errors.append(f"Browser failed, used SpiderCloud fallback: {e}")
            except Exception as fallback_error:
                logger.error(f"SpiderCloud fallback also failed: {fallback_error}")
                result.errors.append(str(e))
        else:
            result.errors.append(str(e))
    
    result.duration_seconds = time.time() - start_time
    
    # Save to history
    save_crawl_history(result)
    
    return result


async def crawl_spidercloud_fallback(grantee_code: str) -> list[FCCRecord]:
    """Fallback to SpiderCloud API for FCC data."""
    import httpx
    
    api_key = os.getenv("SPIDERCLOUD_API_KEY", "sk-ac0423ad-1897-4094-bc2c-5e5a92777375")
    url = f"https://api.spidercloud.com/v1/fcc/search?grantee_code={grantee_code}"
    
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    records = []
    
    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            for item in data.get("results", []):
                record = FCCRecord(
                    fcc_id=item.get("fcc_id", ""),
                    grantee_code=grantee_code,
                    product_code=item.get("product_code", ""),
                    applicant_name=item.get("applicant_name", ""),
                    product_description=item.get("product_description", item.get("product_name", "")),
                    grant_date=item.get("certification_date", item.get("grant_date", "")),
                    filing_date=item.get("filing_date", ""),
                    application_type=item.get("application_type", "Original Equipment"),
                    status=item.get("status", "Granted"),
                    equipment_class=item.get("equipment_class")
                )
                records.append(record)
        else:
            logger.warning(f"SpiderCloud API returned {response.status_code}: {response.text}")
            raise Exception(f"SpiderCloud API error: {response.status_code}")
    
    logger.info(f"SpiderCloud fallback found {len(records)} records for {grantee_code}")
    return records


async def run_crawl(grantee_codes: list[str] = None, notify: bool = True):
    """Run the full crawl across all grantee codes."""
    if grantee_codes is None:
        grantee_codes = GRANTEE_CODES
    
    logger.info(f"Starting FCC crawl for {len(grantee_codes)} grantee codes")
    
    all_new_records = []
    all_results = []
    
    for code in grantee_codes:
        logger.info(f"=== Processing {code} ===")
        result = await crawl_grantee(code)
        all_results.append(result)
        all_new_records.extend(result.new_records)
        
        # Brief pause between requests to be respectful
        if code != grantee_codes[-1]:
            await asyncio.sleep(2)
    
    # Save state
    state = {
        "last_run": datetime.now().isoformat(),
        "grantee_codes": grantee_codes,
        "total_records": sum(len(r.records) for r in all_results),
        "total_new": len(all_new_records)
    }
    save_last_state(state)
    
    # Send notifications
    if notify and all_new_records:
        await notify_new_records(all_new_records)
    
    # Summary
    total_errors = sum(len(r.errors) for r in all_results)
    logger.info(f"=== Crawl Complete ===")
    logger.info(f"Total new records: {len(all_new_records)}")
    logger.info(f"Total errors: {total_errors}")
    
    return all_results


# ============================================================================
# CLI
# ============================================================================

def setup_logging(verbose: bool = False):
    """Configure logging."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    
    log_level = "DEBUG" if verbose else "INFO"
    
    logger.remove()
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"
    )
    logger.add(
        LOG_DIR / "fcc_browser_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="1 day",
        retention="30 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}"
    )


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="FCC Browser-Based Crawler")
    parser.add_argument(
        "--grantee", "-g",
        help="Single grantee code to test"
    )
    parser.add_argument(
        "--daemon", "-d",
        action="store_true",
        help="Run as daemon with periodic checks"
    )
    parser.add_argument(
        "--interval", "-i",
        type=int,
        default=3600,
        help="Interval in seconds for daemon mode (default: 3600)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Disable notifications"
    )
    
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    
    # Check Playwright availability
    if not PLAYWRIGHT_AVAILABLE:
        logger.error("Playwright is not installed!")
        logger.error("Install with: pip install playwright && playwright install chromium")
        sys.exit(1)
    
    # Initialize database
    init_database()
    
    # Run once or daemon
    if args.grantee:
        # Single grantee test
        asyncio.run(run_crawl([args.grantee], notify=not args.no_notify))
    elif args.daemon:
        # Daemon mode
        logger.info(f"Starting daemon mode with {args.interval}s interval")
        while True:
            try:
                asyncio.run(run_crawl(notify=not args.no_notify))
            except Exception as e:
                logger.error(f"Daemon error: {e}")
            
            logger.info(f"Sleeping for {args.interval} seconds...")
            time.sleep(args.interval)
    else:
        # Single run
        asyncio.run(run_crawl(notify=not args.no_notify))


if __name__ == "__main__":
    main()
