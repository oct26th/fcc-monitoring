#!/usr/bin/env python3
"""
FCC Monitor - Browserless API Crawler
Uses Browserless cloud service (browserless.io) for browser automation to scrape
FCC.gov equipment authorizations, bypassing Akamai WAF protection.

Usage:
    python crawler_browserless.py              # Run once
    python crawler_browserless.py --daemon     # Run as daemon
    python crawler_browserless.py --grantee UZ7 # Test with single grantee

Environment Variables:
    BROWSERLESS_API_KEY: Browserless API key (required)
    BROWSERLESS_REGION: Region (sfo, lon, ams) - default: sfo
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
from html.parser import HTMLParser
from typing import Optional

import sqlite3
from loguru import logger

# Try to import Playwright
try:
    from playwright.async_api import async_playwright, Playwright, Browser, Page, BrowserContext
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.error("Playwright is not installed. Run: pip install playwright")

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

# Browserless configuration
BROWSERLESS_API_KEY = os.getenv("BROWSERLESS_API_KEY", "")
BROWSERLESS_REGION = os.getenv("BROWSERLESS_REGION", "sfo")  # sfo, lon, ams
BROWSERLESS_STEALTH = os.getenv("BROWSERLESS_STEALTH", "true").lower() == "true"
BROWSERLESS_HEADLESS = os.getenv("BROWSERLESS_HEADLESS", "true").lower() == "true"

# Telegram/Discord configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

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
# Browserless Connection
# ============================================================================

class BrowserlessConnection:
    """Manages connection to Browserless cloud service."""
    
    def __init__(self, api_key: str, region: str = "sfo", stealth: bool = True, headless: bool = True):
        self.api_key = api_key
        self.region = region
        self.stealth = stealth
        self.headless = headless
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        
        # Build WebSocket endpoint URL
        self._build_endpoint()
    
    def _build_endpoint(self):
        """Build the Browserless WebSocket endpoint URL."""
        # Base WebSocket URL
        ws_base = f"wss://production-{self.region}.browserless.io"
        
        # Build query parameters
        params = [f"token={self.api_key}"]
        
        # Add stealth mode
        if self.stealth:
            # Use stealth endpoint for better bot detection bypass
            self.ws_endpoint = f"{ws_base}/stealth?{ '&'.join(params) }"
        else:
            self.ws_endpoint = f"{ws_base}?{ '&'.join(params) }"
        
        # Add headless parameter
        if not self.headless:
            self.ws_endpoint += "&headless=false"
        
        logger.info(f"Browserless endpoint: {self.ws_endpoint.replace(self.api_key, '***')}")
    
    async def __aenter__(self):
        """Connect to Browserless and setup browser."""
        if not PLAYWRIGHT_AVAILABLE:
            raise RuntimeError("Playwright is not installed")
        
        self.playwright = await async_playwright().start()
        
        try:
            # Connect to Browserless via CDP (Chrome DevTools Protocol)
            logger.info("Connecting to Browserless cloud...")
            self.browser = await self.playwright.chromium.connect_over_cdp(self.ws_endpoint)
            
            # Create context with realistic settings
            self.context = await self.browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York',
                permissions=['geolocation'],
                ignore_https_errors=True,
            )
            
            # Set extra headers
            await self.context.set_extra_http_headers({
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Upgrade-Insecure-Requests': '1',
            })
            
            # Create new page
            self.page = await self.context.new_page()
            
            # Set timeout for FCC pages (they can be slow)
            self.page.set_default_timeout(120000)
            
            # Listen for console errors
            self.page.on("console", lambda msg: logger.debug(f"Browser console: {msg.text}") if msg.type == "error" else None)
            
            logger.info("Successfully connected to Browserless")
            
        except Exception as e:
            logger.error(f"Failed to connect to Browserless: {e}")
            # Cleanup on failure
            if self.playwright:
                await self.playwright.stop()
            raise
        
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Disconnect from Browserless."""
        try:
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            logger.info("Disconnected from Browserless")
        except Exception as e:
            logger.warning(f"Error during Browserless cleanup: {e}")
    
    async def close(self):
        """Explicit close method."""
        await self.__aexit__(None, None, None)


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
        # Update existing record - always update dates with fresh crawl data
        cursor.execute("""
            UPDATE fcc_records 
            SET grant_date = ?,
                filing_date = ?,
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
# FCC Browser Crawler (Browserless)
# ============================================================================

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self.current_table = []
        self.current_row = []
        self.in_td = False
        self.in_th = False
        
    def handle_starttag(self, tag, attrs):
        if tag == 'table':
            self.current_table = []
        elif tag == 'tr':
            self.current_row = []
        elif tag == 'td':
            self.in_td = True
        elif tag == 'th':
            self.in_th = True
            
    def handle_endtag(self, tag):
        if tag == 'table':
            if self.current_table:
                self.tables.append(self.current_table)
        elif tag == 'tr':
            if self.current_row:
                self.current_table.append(self.current_row)
                self.current_row = []
        elif tag == 'td':
            self.in_td = False
        elif tag == 'th':
            self.in_th = False
            
    def handle_data(self, data):
        if self.in_td or self.in_th:
            self.current_row.append(data.strip())



class FCCBrowserlessCrawler:
    """Browser-based FCC crawler using Browserless cloud service."""
    
    def __init__(self, browserless_conn: BrowserlessConnection):
        self.conn = browserless_conn
        self.page = browserless_conn.page
    
    async def search_grantee(self, grantee_code: str, max_retries: int = 3) -> list[FCCRecord]:
        """Search for a grantee code and return parsed results."""
        records = []
        last_error = None
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Navigating to FCC search page for {grantee_code} (attempt {attempt + 1}/{max_retries})...")
                
                # Navigate to FCC search page
                response = await self.page.goto(FCC_SEARCH_URL, wait_until="networkidle", timeout=90000)
                
                # Check for HTTP errors
                if response and response.status >= 400:
                    logger.warning(f"HTTP error {response.status} on attempt {attempt + 1}")
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                
                # Brief pause for page to settle
                await asyncio.sleep(2)
                
                # Handle potential meta refresh redirect (FCC uses ColdFusion)
                try:
                    meta_refresh = await self.page.query_selector('meta[http-equiv="refresh"]')
                    if meta_refresh:
                        content = await meta_refresh.get_attribute('content')
                        if content and 'url=' in content.lower():
                            redirect_url = content.split('url=')[-1].strip()
                            logger.info(f"Following meta refresh to: {redirect_url}")
                            await self.page.goto(redirect_url, wait_until="networkidle", timeout=90000)
                except:
                    pass
                
                # Check for access denied/blocked
                page_title = await self.page.title()
                page_url = self.page.url
                
                if 'access denied' in page_title.lower() or 'forbidden' in page_title.lower():
                    logger.warning(f"Access denied on attempt {attempt + 1}")
                    await asyncio.sleep(5 * (attempt + 1))
                    continue
                
                logger.debug(f"Page title: {page_title}, URL: {page_url}")
                
                # Wait for form to be ready
                try:
                    await self.page.wait_for_selector('form, table', timeout=15000)
                except Exception as e:
                    logger.warning(f"Form not found: {e}")
                
                # Find grantee code input field
                grantee_input = None
                input_selectors = [
                    'input[name="grantee_code"]',
                    'input[id="grantee_code"]',
                    'input[name="granteeCode"]', 
                    'input[id="granteeCode"]',
                    'input[placeholder*="Grantee"]',
                    'input[type="text"]:nth-of-type(1)',
                    'input[type="text"]',
                ]
                
                for selector in input_selectors:
                    try:
                        grantee_input = await self.page.query_selector(selector)
                        if grantee_input:
                            logger.debug(f"Found input with selector: {selector}")
                            break
                    except:
                        continue
                
                if not grantee_input:
                    # Dump HTML for debugging
                    html = await self.page.content()
                    logger.error(f"Could not find grantee code input")
                    logger.debug(f"Page HTML (first 1000 chars): {html[:1000]}")
                    raise Exception("Could not find grantee code input field")
                
                # Clear and fill the input
                await grantee_input.fill("")
                await grantee_input.type(grantee_code, delay=100)
                
                # Find search button
                search_button = None
                button_selectors = [
                    'input[type="submit"][value*="Search"]',
                    'input[type="submit"]',
                    'button[type="submit"]',
                    'button:has-text("Search")',
                    'input[type="button"][value*="Search"]',
                ]
                
                for selector in button_selectors:
                    try:
                        search_button = await self.page.query_selector(selector)
                        if search_button:
                            break
                    except:
                        continue
                
                if not search_button:
                    raise Exception("Could not find search button")
                
                # Click search
                logger.info(f"Clicking search for grantee: {grantee_code}")
                await search_button.click()
                
                # Wait for results
                await asyncio.sleep(3)
                
                try:
                    await self.page.wait_for_selector('table', timeout=30000)
                except Exception as e:
                    logger.warning(f"Timeout waiting for results: {e}")
                
                # Parse results
                records = await self._parse_results(grantee_code)
                
                logger.info(f"Found {len(records)} records for {grantee_code}")
                
                # Success - break retry loop
                break
            
            except Exception as e:
                last_error = e
                logger.error(f"Error on attempt {attempt + 1} for {grantee_code}: {e}")
                
                if attempt == max_retries - 1:
                    # Last attempt - save screenshot
                    try:
                        screenshot_path = LOG_DIR / f"error_{grantee_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
                        LOG_DIR.mkdir(parents=True, exist_ok=True)
                        await self.page.screenshot(path=str(screenshot_path))
                        logger.info(f"Screenshot saved: {screenshot_path}")
                    except:
                        pass
                else:
                    await asyncio.sleep(3)
        
        # Handle final state
        if not records and last_error:
            raise last_error
        elif not records:
            raise Exception(f"Failed to access FCC website after {max_retries} attempts - likely blocked")
            
        return records
    
    async def _parse_results(self, grantee_code: str) -> list[FCCRecord]:
        """Parse the results table from the FCC search results page."""
        records = []
        
        try:
            # Wait for results to fully load
            await asyncio.sleep(2)
            
            # DEBUG: Find the actual data table by looking for specific headers
            # Skip navigation/menu tables - look for tables with FCC ID, Grant Date, etc.
            debug_info = await self.page.evaluate('''() => {
                const tables = document.querySelectorAll('table');
                const result = {
                    tableCount: tables.length,
                    tablesAnalyzed: [],
                    selectedTableIndex: -1,
                    totalDataRows: 0
                };
                
                // Analyze each table to find the one with actual search results
                let dataTableIndex = -1;
                let maxDataRows = 0;
                
                tables.forEach((table, idx) => {
                    const rows = table.querySelectorAll('tr');
                    const rowCount = rows.length;
                    
                    // Get header cells to check for FCC-related columns
                    const headerRow = rows[0];
                    const headers = headerRow ? Array.from(headerRow.querySelectorAll('th, td')).map(h => h.innerText.trim().toLowerCase()) : [];
                    const headerText = headers.join(' ');
                    
                    // Check if this looks like a data table (has relevant headers or many rows)
                    const hasFCCIdHeader = headerText.includes('fcc') && headerText.includes('id');
                    const hasGrantDateHeader = headerText.includes('grant') && headerText.includes('date');
                    const hasApplicantHeader = headerText.includes('applicant') || headerText.includes('name');
                    
                    // Count data rows (rows with td elements)
                    let dataRowCount = 0;
                    rows.forEach(row => {
                        const cells = row.querySelectorAll('td');
                        if (cells.length >= 3) dataRowCount++;
                    });
                    
                    result.tablesAnalyzed.push({
                        index: idx,
                        rowCount: rowCount,
                        dataRowCount: dataRowCount,
                        headers: headers.slice(0, 10),
                        hasFCCIdHeader: hasFCCIdHeader,
                        hasGrantDateHeader: hasGrantDateHeader,
                        hasApplicantHeader: hasApplicantHeader
                    });
                    
                    // Select this table if it has data rows and looks like a results table
                    // Prioritize tables with FCC ID or Grant Date headers
                    if (dataRowCount > 0) {
                        if (hasFCCIdHeader || hasGrantDateHeader) {
                            // This is likely the results table - high priority
                            if (dataRowCount > maxDataRows || dataTableIndex === -1) {
                                dataTableIndex = idx;
                                maxDataRows = dataRowCount;
                            }
                        } else if (dataRowCount > maxDataRows && dataTableIndex === -1) {
                            // Fallback: take the table with most data rows
                            dataTableIndex = idx;
                            maxDataRows = dataRowCount;
                        }
                    }
                });
                
                result.selectedTableIndex = dataTableIndex;
                result.totalDataRows = maxDataRows;
                
                return result;
            }''')
            
            logger.info(f"DEBUG - Found {debug_info['tableCount']} tables")
            for t in debug_info['tablesAnalyzed']:
                logger.info(f"DEBUG Table {t['index']}: rows={t['rowCount']}, dataRows={t['dataRowCount']}, headers={t['headers'][:5]}, fccId={t['hasFCCIdHeader']}, grantDate={t['hasGrantDateHeader']}")
            logger.info(f"DEBUG - Selected table index: {debug_info['selectedTableIndex']} with {debug_info['totalDataRows']} data rows")
            
            if debug_info['selectedTableIndex'] < 0:
                logger.warning("No data table found - search may have returned no results")
                return records
            
            # Use JavaScript to find the correct table and extract data from ONLY that table
            # Skip navigation tables - target the data table with FCC ID/Grant Date headers
            # Simpler parsing - just get the table HTML
            html = await self.page.content()
            # Parse manually using BeautifulSoup
            from html.parser import HTMLParser
            parser = TableParser()
            
            # Find tables with FCC ID in headers
            tables = parser.tables
            for idx, table in enumerate(tables):
                headers = [th.get_text(strip=True).lower() for th in table.find_all('th')]
                header_text = ' '.join(headers)
                if 'fcc' in header_text and 'id' in header_text:
                    print(f'Found data table at index {idx}')
                    # Parse rows here
                    break
            
            logger.info(f"Parsed {len(data.get('records', []))} records from table {data.get('tableIndex', -1)}")
            
            # Convert to FCCRecord objects
            seen_ids = set()
            for item in data.get('records', []):
                fcc_id = item.get('fcc_id', '')
                app_type = item.get('app_type', '')
                
                if not fcc_id:
                    continue
                
                # Skip if application_type looks like an FCC ID (parsing error)
                # Use case-insensitive comparison
                if app_type and app_type.upper().startswith(grantee_code.upper()):
                    logger.debug(f"Skipping record with invalid app_type: {fcc_id}")
                    continue
                
                if fcc_id in seen_ids:
                    continue
                seen_ids.add(fcc_id)
                
                # Extract product code from FCC ID
                product_code = ""
                if len(fcc_id) > len(grantee_code):
                    product_code = fcc_id[len(grantee_code):]
                
                # Build applicant name with location
                applicant = item.get('applicant', '')
                city = item.get('city', '')
                state = item.get('state', '')
                applicant_name = applicant
                if city and state:
                    applicant_name = f"{applicant} ({city}, {state})"
                
                record = FCCRecord(
                    fcc_id=fcc_id,
                    grantee_code=grantee_code,
                    product_code=product_code,
                    applicant_name=applicant_name,
                    product_description="",
                    grant_date=item.get('grant_date', ''),
                    filing_date=item.get('filing_date', ''),
                    application_type=item.get('app_type', ''),
                    status="Granted" if item.get('grant_date') else "Pending"
                )
                records.append(record)
                logger.debug(f"Parsed record: {fcc_id} - {record.application_type} - {record.grant_date}")
            
            logger.info(f"Parsed {len(records)} unique records from FCC search results")
                    
        except Exception as e:
            logger.error(f"Error parsing results: {e}")
            import traceback
            traceback.print_exc()
        
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
    
    message = "📡 <b>FCC Monitor - New Equipment Authorizations (Browserless)</b>\n\n"
    
    if c2pc_records:
        message += "🔄 <b>Class II Permissive Changes (C2PC):</b>\n"
        for r in c2pc_records[:5]:
            message += f"• {r.fcc_id}: {r.product_description[:50]}...\n"
        if len(c2pc_records) > 5:
            message += f"... and {len(c2pc_records) - 5} more\n"
        message += "\n"
    
    if other_records:
        message += "📋 <b>New Equipment:</b>\n"
        for r in other_records[:5]:
            message += f"• {r.fcc_id}: {r.product_description[:50]}...\n"
        if len(other_records) > 5:
            message += f"... and {len(other_records) - 5} more\n"
    
    message += f"\nTotal: {len(new_records)} new records"
    
    await send_telegram_notification(message)
    await send_discord_notification(message)


# ============================================================================
# Main Crawl Functions
# ============================================================================

async def crawl_grantee(grantee_code: str) -> CrawlResult:
    """Crawl a single grantee code using Browserless."""
    result = CrawlResult(grantee_code=grantee_code)
    start_time = time.time()
    
    # Create Browserless connection
    conn = BrowserlessConnection(
        api_key=BROWSERLESS_API_KEY,
        region=BROWSERLESS_REGION,
        stealth=BROWSERLESS_STEALTH,
        headless=BROWSERLESS_HEADLESS
    )
    
    try:
        async with conn:
            crawler = FCCBrowserlessCrawler(conn)
            records = await crawler.search_grantee(grantee_code)
            result.records = records
            
            # Save records and detect new ones
            for record in records:
                is_new = save_record(record)
                if is_new:
                    result.new_records.append(record)
    
    except Exception as e:
        logger.error(f"Browserless crawl failed for {grantee_code}: {e}")
        result.errors.append(str(e))
    
    result.duration_seconds = time.time() - start_time
    
    # Save to history
    save_crawl_history(result)
    
    return result


async def run_crawl(grantee_codes: list[str] = None, notify: bool = True):
    """Run the full crawl across all grantee codes."""
    if grantee_codes is None:
        grantee_codes = GRANTEE_CODES
    
    # Validate API key
    if not BROWSERLESS_API_KEY:
        logger.error("BROWSERLESS_API_KEY environment variable is required!")
        logger.error("Get your API key from https://browserless.io/")
        sys.exit(1)
    
    logger.info(f"Starting FCC Browserless crawl for {len(grantee_codes)} grantee codes")
    logger.info(f"Browserless region: {BROWSERLESS_REGION}, stealth: {BROWSERLESS_STEALTH}")
    
    all_new_records = []
    all_results = []
    
    for code in grantee_codes:
        logger.info(f"=== Processing {code} ===")
        result = await crawl_grantee(code)
        all_results.append(result)
        all_new_records.extend(result.new_records)
        
        # Brief pause between requests
        if code != grantee_codes[-1]:
            await asyncio.sleep(2)
    
    # Save state
    state = {
        "last_run": datetime.now().isoformat(),
        "grantee_codes": grantee_codes,
        "total_records": sum(len(r.records) for r in all_results),
        "total_new": len(all_new_records),
        "source": "browserless"
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
        LOG_DIR / "fcc_browserless_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="1 day",
        retention="30 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}"
    )


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="FCC Browserless Crawler")
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
    parser.add_argument(
        "--region", "-r",
        default="sfo",
        choices=["sfo", "lon", "ams"],
        help="Browserless region (default: sfo)"
    )
    parser.add_argument(
        "--no-stealth",
        action="store_true",
        help="Disable stealth mode"
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Run browser in headful mode (visible)"
    )
    
    args = parser.parse_args()
    
    # Override config from args
    global BROWSERLESS_REGION, BROWSERLESS_STEALTH, BROWSERLESS_HEADLESS
    BROWSERLESS_REGION = args.region
    BROWSERLESS_STEALTH = not args.no_stealth
    BROWSERLESS_HEADLESS = not args.headful
    
    setup_logging(args.verbose)
    
    # Check Playwright availability
    if not PLAYWRIGHT_AVAILABLE:
        logger.error("Playwright is not installed!")
        logger.error("Install with: pip install playwright && playwright install chromium")
        sys.exit(1)
    
    # Check API key
    if not BROWSERLESS_API_KEY:
        logger.warning("BROWSERLESS_API_KEY not set!")
        logger.warning("Get your free API key from https://browserless.io/")
        logger.warning("Or set BROWSERLESS_API_KEY environment variable")
    
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
