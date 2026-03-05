"""Data fetchers for FCC/SpiderCloud/Browser APIs."""
import json
import logging
import time
from typing import List, Optional, Dict, Any
from urllib import request, parse, error

import httpx
from loguru import logger

from .config import get_settings
from .models import FCCRecord

# ============================================================================
# Fetcher Interfaces & Base
# ============================================================================

class BaseFetcher:
    """Base class for all fetchers."""
    def __init__(self):
        self.settings = get_settings()

    def fetch_by_grantee(self, grantee_code: str) -> List[FCCRecord]:
        raise NotImplementedError

# ============================================================================
# SpiderCloud Fetcher
# ============================================================================

class SpiderCloudFetcher(BaseFetcher):
    """Fetch FCC certification data from SpiderCloud API."""
    
    def __init__(self):
        super().__init__()
        self._session: Optional[httpx.Client] = None
    
    @property
    def session(self) -> httpx.Client:
        if self._session is None:
            self._session = httpx.Client(
                timeout=30.0,
                follow_redirects=True,
                headers={"User-Agent": "FCC-Monitor/1.0"}
            )
        return self._session
    
    def fetch_by_grantee(self, grantee_code: str) -> List[FCCRecord]:
        cfg = self.settings.data_source.spidercloud
        if not cfg.api_key:
            logger.warning("SpiderCloud API key not configured")
            return []
        
        url = f"{cfg.base_url}/fcc/search"
        headers = {
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json"
        }
        params = {
            "grantee_code": grantee_code,
            "limit": 1000
        }
        
        try:
            response = self.session.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            
            records = []
            items = data.get("results", data.get("data", []))
            for item in items:
                records.append(FCCRecord(
                    fcc_id=item.get("fcc_id", ""),
                    grantee_code=item.get("grantee_code", ""),
                    product_code=item.get("product_code", ""),
                    applicant_name=item.get("applicant_name", ""),
                    product_name=item.get("product_name", ""),
                    certification_date=item.get("certification_date", ""),
                    status=item.get("status", "Granted"),
                    expires_on=item.get("expires_on")
                ))
            return records
        except httpx.HTTPError as e:
            logger.error(f"SpiderCloud API error: {e}")
            return []
        finally:
            if self._session:
                self._session.close()
                self._session = None

# ============================================================================
# Browserless REST Fetcher
# ============================================================================

class BrowserlessFetcher(BaseFetcher):
    """Fetch data via Browserless cloud service (/function endpoint)."""
    
    JS_TEMPLATE = """
export default async ({ page, context }) => {
  const granteeCode = context.granteeCode;
  const searchUrl = 'https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm';
  
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
  await page.setViewport({ width: 1280, height: 800 });

  try {
    await page.goto(searchUrl, { waitUntil: 'networkidle2', timeout: 60000 });
    const title = await page.title();
    if (title.toLowerCase().includes('access denied')) throw new Error('Blocked by Akamai');

    await page.waitForSelector('input[name="grantee_code"]', { timeout: 15000 });
    await page.type('input[name="grantee_code"]', granteeCode, { delay: 100 });
    
    await page.evaluate(() => {
      const showRecs = document.querySelector('input[name="show_records"]');
      if (showRecs) showRecs.value = '';
    });
    await page.type('input[name="show_records"]', '500', { delay: 50 });
    
    await Promise.all([
      page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 60000 }),
      page.click('input[type="submit"]')
    ]);

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
      return { records };
    });
    return data;
  } catch (e) {
    return { error: e.message };
  }
}
"""

    def fetch_by_grantee(self, grantee_code: str) -> List[FCCRecord]:
        cfg = self.settings.data_source.browserless
        if not cfg.api_key:
            logger.error("BROWSERLESS_API_KEY missing!")
            return []
            
        url = f"https://production-{cfg.region}.browserless.io/function?token={cfg.api_key}"
        payload = {
            "code": self.JS_TEMPLATE,
            "context": {"granteeCode": grantee_code}
        }
        
        data = json.dumps(payload).encode('utf-8')
        headers = {'Content-Type': 'application/json', 'Cache-Control': 'no-cache'}
        
        try:
            req = request.Request(url, data=data, headers=headers)
            with request.urlopen(req, timeout=120) as response:
                result = json.loads(response.read().decode())
                if "error" in result:
                    logger.error(f"Browserless error: {result['error']}")
                    return []
                
                raw_recs = result.get("records", [])
                records = []
                for item in raw_recs:
                    fcc_id = item.get("fcc_id", "").replace(" ", "")
                    if not fcc_id: continue
                    
                    p_code = fcc_id[len(grantee_code):] if fcc_id.startswith(grantee_code) else fcc_id
                    
                    name = item.get("applicant", "Unknown")
                    city = item.get("city")
                    state = item.get("state")
                    
                    # More robust location formatting
                    location = ""
                    if city and city.lower() != "n/a":
                        location = city
                        if state and state.lower() != "n/a":
                            location = f"{city}, {state}"
                    
                    formatted_name = name
                    if location and location.lower() not in name.lower():
                        formatted_name = f"{name} ({location})"
                    
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
        except Exception as e:
            logger.error(f"Browserless fetch failed: {e}")
            return []

# ============================================================================
# Playwright Fetcher
# ============================================================================

class PlaywrightFetcher(BaseFetcher):
    """Fetch data using local Playwright instance."""
    # (Implementation omitted for brevity, but can be added if needed)
    # Since crawler_browser.py is very long, let's just use the logic if required.
    # For now, we focus on the most reliable ones.
    def fetch_by_grantee(self, grantee_code: str) -> List[FCCRecord]:
        logger.warning("PlaywrightFetcher not fully implemented in refactor yet.")
        return []

# ============================================================================
# Fetcher Factory
# ============================================================================

def get_fetcher(strategy: str = "spidercloud") -> BaseFetcher:
    """Get fetcher instance based on strategy."""
    if strategy == "spidercloud":
        return SpiderCloudFetcher()
    elif strategy == "browserless":
        return BrowserlessFetcher()
    elif strategy == "playwright":
        return PlaywrightFetcher()
    else:
        raise ValueError(f"Unknown strategy: {strategy}")
