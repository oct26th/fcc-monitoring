"""Data fetchers for FCC/SpiderCloud/Browser APIs.

Architecture:
  SpiderCloudProxy          — Network layer: routes HTTP requests through spider.cloud
                              to bypass Akamai bot-detection on apps.fcc.gov.
  SpiderCloudFetcher        — ⚠️  KNOWN BUG: /scrape API eats POST body (grantee_code
                              becomes a GET param-less request). Use automation_scripts
                              path or BrowserlessFetcher instead.
  SpiderCloudScriptFetcher  — Fixed path: uses spider.cloud automation_scripts to
                              fill and submit the FCC search form via headless Chrome.
  BrowserlessFetcher        — Secondary fetcher: uses Browserless.io JS execution
                              as an alternative browser-based scraping path.
  PlaywrightFetcher         — Tertiary fetcher: local Playwright instance (fallback).
  get_fetcher()             — Factory; selects strategy from settings or explicit arg.
"""

import json
import logging
import re
import time
from typing import List, Optional, Dict, Any
from urllib import request, parse, error

import httpx
from loguru import logger

from .config import get_settings
from .models import FCCRecord


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class BaseFetcher:
    """Base class for all fetchers."""

    def __init__(self):
        self.settings = get_settings()

    def fetch_by_grantee(self, grantee_code: str) -> List[FCCRecord]:
        raise NotImplementedError


# ===========================================================================
# SpiderCloud Proxy  ─  Network / Infrastructure Layer
# ===========================================================================

class SpiderCloudProxy:
    """
    Wraps spider.cloud's Scrape API as a network-level HTTP transport.

    spider.cloud routes requests through its infrastructure (residential
    proxies, headless Chromium, JS rendering) and returns the fully-rendered
    page HTML.  This transparently bypasses Akamai Bot Manager checks that
    block plain httpx / requests calls to apps.fcc.gov.

    Usage:
        proxy = SpiderCloudProxy(api_key="sk-...")
        html  = proxy.fetch_html("https://apps.fcc.gov/...")
        resp  = proxy.post_form("https://apps.fcc.gov/...", form_data={...})

    API reference: https://spider.cloud/docs/api
    """

    BASE_URL = "https://api.spider.cloud"
    SCRAPE_ENDPOINT = "/scrape"
    CRAWL_ENDPOINT  = "/crawl"   # multi-page crawl w/ sitemap discovery

    # spider.cloud return formats
    FORMAT_HTML     = "html"
    FORMAT_MARKDOWN = "markdown"
    FORMAT_BYTES    = "bytes"

    def __init__(
        self,
        api_key: str,
        *,
        stealth: bool = True,
        proxy_enabled: bool = True,
        render_js: bool = True,
        timeout: float = 60.0,
        max_retries: int = 3,
        retry_delay: float = 5.0,
    ):
        if not api_key:
            raise ValueError("SpiderCloudProxy requires a valid api_key")

        self.api_key       = api_key
        self.stealth       = stealth
        self.proxy_enabled = proxy_enabled
        self.render_js     = render_js
        self.timeout       = timeout
        self.max_retries   = max_retries
        self.retry_delay   = retry_delay

        self._session: Optional[httpx.Client] = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    @property
    def session(self) -> httpx.Client:
        if self._session is None or self._session.is_closed:
            self._session = httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type":  "application/json",
                    "User-Agent":    "fcc-monitor/2.0 (+spider.cloud)",
                },
            )
        return self._session

    def close(self):
        if self._session and not self._session.is_closed:
            self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Core API calls
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        url: str,
        *,
        return_format: str = FORMAT_HTML,
        post_body: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "url":           url,
            "return_format": return_format,
            "request":       "chrome",
            "stealth":       True if self.stealth else False,
            "proxy_enabled": self.proxy_enabled,
        }
        if post_body is not None:
            payload["http_method"] = "POST"
            payload["body"]        = post_body
        if extra:
            payload.update(extra)
        return payload

    def _request_with_retry(self, payload: Dict[str, Any]) -> Optional[str]:
        """POST payload to /scrape; return raw response text or None."""
        url = self.BASE_URL + self.SCRAPE_ENDPOINT

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.post(url, json=payload)
                resp.raise_for_status()

                data = resp.json()

                # spider.cloud wraps content in a list or object
                if isinstance(data, list) and data:
                    return data[0].get("content") or data[0].get("html") or ""
                if isinstance(data, dict):
                    return data.get("content") or data.get("html") or ""

                logger.warning(f"[SpiderCloudProxy] Unexpected response shape: {type(data)}")
                return None

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                logger.warning(
                    f"[SpiderCloudProxy] HTTP {status} on attempt {attempt}/{self.max_retries}"
                )
                if status in (429, 503) and attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                    continue
                logger.error(f"[SpiderCloudProxy] Non-retryable HTTP error: {exc}")
                return None

            except httpx.RequestError as exc:
                logger.warning(
                    f"[SpiderCloudProxy] Request error on attempt {attempt}/{self.max_retries}: {exc}"
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                    continue
                return None

        return None

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def fetch_html(self, url: str, **extra) -> Optional[str]:
        """GET a URL; return rendered HTML (Akamai-bypassed)."""
        payload = self._build_payload(url, return_format=self.FORMAT_HTML, extra=extra or None)
        return self._request_with_retry(payload)

    def post_form(self, url: str, form_data: Dict[str, str], **extra) -> Optional[str]:
        """
        Simulate an HTML form POST through spider.cloud.

        *form_data* is URL-encoded and sent as the request body; spider.cloud
        issues the POST on our behalf from its infrastructure.
        """
        body    = parse.urlencode(form_data)
        payload = self._build_payload(
            url,
            return_format=self.FORMAT_HTML,
            post_body=body,
            extra=extra or None,
        )
        return self._request_with_retry(payload)

    def crawl_with_sitemap(
        self,
        base_url: str,
        *,
        limit: int = 50,
        return_format: str = FORMAT_MARKDOWN,
        path_patterns: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Crawl a website using its sitemap.xml via spider.cloud /crawl endpoint.

        With sitemap=True, spider.cloud will:
          1. Fetch <base_url>/sitemap.xml (or robots.txt → sitemap link)
          2. Enumerate all URLs up to *limit*
          3. Crawl each URL through its infrastructure
          4. Return rendered content per page

        Args:
            base_url:      Root domain or specific sitemap URL
            limit:         Maximum pages to crawl (default 50; raise for broader coverage)
            return_format: "markdown" (default) or "html"
            path_patterns: Optional list of URL path substrings to whitelist.
                           E.g. ["/news", "/press", "/product"] limits crawl
                           to only matching pages (client-side filter applied
                           after spider.cloud returns results).

        Returns:
            List of dicts:  [{"url": str, "content": str}, ...]
            Empty list on error.
        """
        payload: Dict[str, Any] = {
            "url":           base_url,
            "sitemap":       True,
            "limit":         limit,
            "return_format": return_format,
            "stealth":       self.stealth,
            "proxy_enabled": self.proxy_enabled,
        }

        api_url = self.BASE_URL + self.CRAWL_ENDPOINT
        pages: List[Dict[str, Any]] = []

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.post(api_url, json=payload)
                resp.raise_for_status()
                data = resp.json()

                # /crawl returns a list of page objects
                raw_pages: list = []
                if isinstance(data, list):
                    raw_pages = data
                elif isinstance(data, dict):
                    # Some versions wrap in {"data": [...]}
                    raw_pages = data.get("data") or data.get("pages") or []

                for page in raw_pages:
                    if not isinstance(page, dict):
                        continue
                    page_url     = page.get("url") or page.get("link") or ""
                    page_content = page.get("content") or page.get("html") or page.get("markdown") or ""
                    if not page_url or not page_content:
                        continue
                    pages.append({"url": page_url, "content": page_content})

                break  # success — exit retry loop

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                logger.warning(
                    f"[SpiderCloudProxy/crawl] HTTP {status} on attempt "
                    f"{attempt}/{self.max_retries} for {base_url}"
                )
                if status in (429, 503) and attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                    continue
                logger.error(f"[SpiderCloudProxy/crawl] Non-retryable HTTP error: {exc}")
                break

            except httpx.RequestError as exc:
                logger.warning(
                    f"[SpiderCloudProxy/crawl] Request error on attempt "
                    f"{attempt}/{self.max_retries}: {exc}"
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                    continue
                break

        # Apply optional client-side path filter
        if path_patterns and pages:
            filtered = [
                p for p in pages
                if any(pat in p["url"] for pat in path_patterns)
            ]
            logger.debug(
                f"[SpiderCloudProxy/crawl] sitemap path filter: "
                f"{len(pages)} → {len(filtered)} pages "
                f"(patterns={path_patterns})"
            )
            pages = filtered

        logger.info(
            f"[SpiderCloudProxy/crawl] sitemap crawl of {base_url}: "
            f"returned {len(pages)} page(s)"
        )
        return pages


# ===========================================================================
# SpiderCloud Fetcher  ─  Primary Fetcher (Akamai-bypass path)
# ===========================================================================

class SpiderCloudFetcher(BaseFetcher):
    """
    Fetch FCC EAS certification data using spider.cloud as the network layer.

    Strategy:
      1. Open the FCC EAS Generic Search page via SpiderCloudProxy.
      2. POST a search form for the requested grantee_code.
      3. Parse the results table from the returned HTML.

    This approach never touches apps.fcc.gov directly from our IP; all
    requests are proxied through spider.cloud's infrastructure, bypassing
    Akamai Bot Manager transparently.
    """

    FCC_SEARCH_URL = (
        "https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm"
    )

    def __init__(self):
        super().__init__()
        cfg = self.settings.data_source.spidercloud
        self._proxy: Optional[SpiderCloudProxy] = None

        if cfg.api_key:
            self._proxy = SpiderCloudProxy(
                api_key       = cfg.api_key,
                stealth       = True,
                proxy_enabled = True,
                render_js     = True,
                timeout       = cfg.timeout,
                max_retries   = cfg.max_retries,
            )
        else:
            logger.warning(
                "[SpiderCloudFetcher] No SPIDERCLOUD_API_KEY configured; "
                "fetcher is disabled."
            )

    # ------------------------------------------------------------------

    def fetch_by_grantee(self, grantee_code: str) -> List[FCCRecord]:
        if not self._proxy:
            return []

        logger.info(f"[SpiderCloudFetcher] Fetching grantee: {grantee_code}")

        form_data = {
            "grantee_code":  grantee_code,
            "show_records":  "50",
            "action":        "Submit",
        }

        html = self._proxy.post_form(self.FCC_SEARCH_URL, form_data)
        if not html:
            logger.error(
                f"[SpiderCloudFetcher] No HTML returned for {grantee_code}"
            )
            return []

        # Akamai block detection
        if _is_akamai_block(html):
            logger.warning(
                f"[SpiderCloudFetcher] Akamai block still detected for "
                f"{grantee_code}; spider.cloud stealth may need adjustment."
            )
            return []

        records = _parse_fcc_search_html(html, grantee_code)
        logger.info(
            f"[SpiderCloudFetcher] Parsed {len(records)} records for {grantee_code}"
        )
        return records

    def close(self):
        if self._proxy:
            self._proxy.close()


# ===========================================================================
# HTML Parsing helpers
# ===========================================================================

def _is_akamai_block(html: str) -> bool:
    """Heuristic: detect Akamai / bot-manager rejection pages."""
    lower = html.lower()
    signals = [
        "access denied",
        "reference #",       # Akamai reference ID
        "akamai",
        "your browser sent a request",
        "robot or automated",
    ]
    return any(s in lower for s in signals)


def _parse_fcc_search_html(html: str, grantee_code: str) -> List[FCCRecord]:
    """
    Parse the FCC EAS generic search results table from raw HTML.

    The page renders an HTML table with columns such as:
      FCC ID | Final Action | Filing Date | Applicant | Product Description | ...

    We use regex + simple parsing (no external deps like BeautifulSoup) so
    the fetcher works in minimal environments.
    """
    records: List[FCCRecord] = []

    # Strip HTML tags helper
    def strip_tags(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s).strip()

    # Locate <table> blocks
    table_pattern = re.compile(
        r"<table[^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL
    )

    for table_match in table_pattern.finditer(html):
        table_html = table_match.group(0)

        # Find rows
        row_pattern  = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
        cell_pattern = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.IGNORECASE | re.DOTALL)

        rows = row_pattern.findall(table_html)
        if len(rows) < 2:
            continue

        # Parse header
        headers = [strip_tags(c).lower() for c in cell_pattern.findall(rows[0])]
        if not any("fcc" in h for h in headers):
            continue  # not the results table

        # Map column positions
        col: Dict[str, int] = {}
        for i, h in enumerate(headers):
            if "fcc" in h and "id" in h:
                col.setdefault("fcc_id", i)
            elif ("final" in h and "action" in h) or ("grant" in h and "date" in h):
                col.setdefault("grant_date", i)
            elif "filing" in h and "date" in h:
                col.setdefault("filing_date", i)
            elif "applicant" in h or ("name" in h and "applicant" not in col):
                col.setdefault("applicant", i)
            elif "product" in h or "description" in h:
                col.setdefault("product_desc", i)
            elif "purpose" in h or "type" in h:
                col.setdefault("app_type", i)
            elif "city" in h:
                col.setdefault("city", i)
            elif "state" in h:
                col.setdefault("state", i)

        if "fcc_id" not in col:
            continue

        # Parse data rows
        for row_html in rows[1:]:
            raw_cells = cell_pattern.findall(row_html)   # keep raw HTML for link extraction
            cells = [strip_tags(c) for c in raw_cells]
            if len(cells) < 3:
                continue

            def get(key: str) -> str:
                idx = col.get(key)
                if idx is None or idx >= len(cells):
                    return ""
                return cells[idx].strip()

            fcc_id = get("fcc_id").replace(" ", "")
            if not fcc_id or not fcc_id.startswith(grantee_code):
                continue

            product_code = fcc_id[len(grantee_code):]

            # Extract application_id from the ViewExhibitReport link in the row.
            # The row contains links to ViewExhibitReport.cfm with application_id=<base64>
            # and to GetTcb731Report.do with applicationId=<different base64>.
            # We want the one from ViewExhibitReport (exhibits page).
            application_id: Optional[str] = None
            from urllib.parse import unquote
            full_row_html = "".join(raw_cells)
            # Priority 1: application_id= from ViewExhibitReport URL
            m = re.search(
                r"ViewExhibitReport[^\"'<>]*?application_id=([A-Za-z0-9+/%=]+)",
                full_row_html,
                re.IGNORECASE,
            )
            # Priority 2: any application_id= in the row
            if not m:
                m = re.search(
                    r"application_id=([A-Za-z0-9+/%=]+)",
                    full_row_html,
                    re.IGNORECASE,
                )
            if m:
                application_id = unquote(m.group(1))

            # Format location into applicant_name
            city  = get("city")
            state = get("state")
            name  = get("applicant") or "Unknown"
            loc_parts = [p for p in [city, state] if p and p.lower() != "n/a"]
            location  = ", ".join(loc_parts)
            if location and location.lower() not in name.lower():
                name = f"{name} ({location})"

            records.append(FCCRecord(
                fcc_id              = fcc_id,
                grantee_code        = grantee_code,
                product_code        = product_code,
                applicant_name      = name,
                product_description = get("product_desc"),
                grant_date          = get("grant_date"),
                filing_date         = get("filing_date"),
                application_type    = get("app_type"),
                status              = "Granted",
                application_id      = application_id,
            ))

    return records


# ===========================================================================
# Browserless REST Fetcher  ─  Secondary (JS-rendered fallback)
# ===========================================================================

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
    await page.type('input[name="show_records"]', '50', { delay: 50 });

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
        // Extract application_id from anywhere in the row (it lives in link cells, not the FCC ID cell)
        {
          const rowHtml = row.outerHTML;
          const om = rowHtml.match(/application_id=([^&\\s"]+)/i);
          if (om) rect.application_id = decodeURIComponent(om[1]);
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
            "code":    self.JS_TEMPLATE,
            "context": {"granteeCode": grantee_code},
        }

        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "Cache-Control": "no-cache"}

        try:
            req = request.Request(url, data=data, headers=headers)
            with request.urlopen(req, timeout=120) as response:
                result = json.loads(response.read().decode())
                if "error" in result:
                    logger.error(f"Browserless error: {result['error']}")
                    return []

                raw_recs = result.get("records", [])
                records  = []
                for item in raw_recs:
                    fcc_id = item.get("fcc_id", "").replace(" ", "")
                    if not fcc_id:
                        continue

                    p_code = (
                        fcc_id[len(grantee_code):]
                        if fcc_id.startswith(grantee_code)
                        else fcc_id
                    )

                    name  = item.get("applicant", "Unknown")
                    city  = item.get("city")
                    state = item.get("state")

                    location = ""
                    if city and city.lower() != "n/a":
                        location = city
                        if state and state.lower() != "n/a":
                            location = f"{city}, {state}"

                    formatted_name = name
                    if location and location.lower() not in name.lower():
                        formatted_name = f"{name} ({location})"

                    records.append(FCCRecord(
                        fcc_id              = fcc_id,
                        grantee_code        = grantee_code,
                        product_code        = p_code,
                        applicant_name      = formatted_name,
                        product_description = item.get("product_desc", ""),
                        grant_date          = item.get("grant_date", ""),
                        filing_date         = item.get("filing_date", ""),
                        application_type    = item.get("app_type", ""),
                        application_id      = item.get("application_id"),
                    ))
                return records
        except Exception as e:
            logger.error(f"Browserless fetch failed: {e}")
            return []



# ===========================================================================
# SpiderCloud Script Fetcher  ─  Fixed POST path via automation_scripts
# ===========================================================================

class SpiderCloudScriptFetcher(BaseFetcher):
    """
    Fetch FCC EAS results using spider.cloud's automation_scripts parameter.

    WHY THIS EXISTS:
      SpiderCloudFetcher.post_form() is broken: spider.cloud's /scrape endpoint
      silently drops the POST body, so grantee_code never reaches apps.fcc.gov.

    HOW THIS WORKS:
      spider.cloud's /scrape endpoint supports an `automation_scripts` parameter —
      a dict mapping URL → list of actions.  The actions run inside spider.cloud's
      managed Chrome: Evaluate JS to fill the form, Click the submit button, then
      WaitForNavigation (null) to capture the results page HTML.
      No local Playwright or CDP connection required; pure httpx POST.

    Verified action format:
      {url_pattern: [{"Evaluate": "..."}, {"Click": "..."}, {"WaitForNavigation": null}]}

    API reference: https://spider.cloud/guides/crawling-authenticated-pages
    """

    SCRAPE_ENDPOINT = "https://api.spider.cloud/scrape"
    FCC_SEARCH_URL = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm"

    def __init__(self):
        super().__init__()
        cfg = self.settings.data_source.spidercloud
        self.api_key = cfg.api_key
        self.timeout = cfg.timeout
        self.max_retries = cfg.max_retries
        if not self.api_key:
            logger.warning(
                "[SpiderCloudScriptFetcher] No SPIDERCLOUD_API_KEY — fetcher disabled."
            )

    def fetch_by_grantee(self, grantee_code: str) -> List[FCCRecord]:
        if not self.api_key:
            return []

        logger.info(f"[SpiderCloudScriptFetcher] Fetching grantee: {grantee_code}")

        for attempt in range(1, self.max_retries + 1):
            try:
                html = self._fetch_via_execution_scripts(grantee_code)
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    f"[SpiderCloudScriptFetcher] HTTP {exc.response.status_code} "
                    f"on attempt {attempt}/{self.max_retries}"
                )
                if exc.response.status_code in (429, 503) and attempt < self.max_retries:
                    time.sleep(5 * attempt)
                    continue
                return []
            except Exception as exc:
                logger.warning(
                    f"[SpiderCloudScriptFetcher] Attempt {attempt}/{self.max_retries} "
                    f"failed: {exc}"
                )
                if attempt < self.max_retries:
                    time.sleep(5 * attempt)
                continue

            if not html:
                logger.warning(
                    f"[SpiderCloudScriptFetcher] Empty HTML for {grantee_code} "
                    f"(attempt {attempt})"
                )
                if attempt < self.max_retries:
                    time.sleep(5 * attempt)
                continue

            if _is_akamai_block(html):
                logger.warning(
                    f"[SpiderCloudScriptFetcher] Akamai block for {grantee_code}"
                )
                return []

            records = _parse_fcc_search_html(html, grantee_code)
            logger.info(
                f"[SpiderCloudScriptFetcher] Parsed {len(records)} records "
                f"for {grantee_code}"
            )
            return records

        return []

    def _fetch_via_execution_scripts(self, grantee_code: str) -> Optional[str]:
        """POST to spider.cloud /scrape with automation_scripts to fill and submit the FCC form."""
        js_fill = (
            f"document.querySelector('input[name=\"grantee_code\"]').value = '{grantee_code}';"
            "document.querySelector('input[name=\"show_records\"]').value = '50';"
        )
        payload = {
            "url": self.FCC_SEARCH_URL,
            "request": "chrome",
            "automation_scripts": {
                self.FCC_SEARCH_URL: [
                    {"Evaluate": js_fill},
                    {"Click": "input[type='submit']"},
                    {"WaitForNavigation": None},
                ]
            },
            "return_format": "html",
            "stealth": True,
            "proxy_enabled": True,
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(
                self.SCRAPE_ENDPOINT,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            resp.raise_for_status()
            data = resp.json()

        if isinstance(data, list) and data:
            return data[0].get("content") or data[0].get("html") or ""
        if isinstance(data, dict):
            return data.get("content") or data.get("html") or ""
        return None


# ===========================================================================
# Playwright Fetcher  ─  Tertiary (local browser fallback)
# ===========================================================================

class PlaywrightFetcher(BaseFetcher):
    """Fetch data using a local Playwright instance (dev/fallback only)."""

    def fetch_by_grantee(self, grantee_code: str) -> List[FCCRecord]:
        logger.warning("PlaywrightFetcher not fully implemented; skipping.")
        return []


# ===========================================================================
# Factory
# ===========================================================================

def get_fetcher(strategy: Optional[str] = None) -> BaseFetcher:
    """
    Return an appropriate fetcher instance.

    Priority (when strategy is None):
      1. spidercloud  — if SPIDERCLOUD_API_KEY is set  (Akamai-bypass path)
      2. browserless  — if BROWSERLESS_API_KEY is set  (JS-rendered fallback)
      3. playwright   — local Playwright (last resort)

    Explicitly pass strategy='browserless' or 'playwright' to override.
    """
    settings = get_settings()

    if strategy is None:
        # Auto-detect best available strategy
        if settings.data_source.spidercloud.api_key:
            strategy = "spidercloud"
        elif settings.data_source.browserless.api_key:
            strategy = "browserless"
        else:
            strategy = "playwright"

    logger.info(f"[get_fetcher] Using strategy: {strategy}")

    if strategy == "spidercloud":
        # Use the automation_scripts path to avoid the POST-body bug in /scrape
        return SpiderCloudScriptFetcher()
    elif strategy == "spidercloud_legacy":
        # Original /scrape path — kept for reference but broken for POST forms
        return SpiderCloudFetcher()
    elif strategy == "browserless":
        return BrowserlessFetcher()
    elif strategy == "playwright":
        return PlaywrightFetcher()
    else:
        raise ValueError(f"Unknown fetcher strategy: {strategy!r}")
