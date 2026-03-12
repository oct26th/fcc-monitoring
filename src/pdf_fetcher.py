"""
FCC Label PDF Fetcher

Flow:
  1. Fetch ViewExhibitReport page via SpiderCloudProxy (GET — no POST bug)
  2. Parse HTML: find table rows where the description cell contains "label"
     → extract attachment IDs from GetApplicationAttachment links in that row
  3. Download each PDF:
       a. Direct httpx GET first (fast, no API cost)
       b. spider.cloud fallback if Akamai blocks
  4. Save to data/pdfs/{fcc_id}/{desc}_{id}.pdf

Real URL examples (from FCC):
  Exhibits page:
    https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm
      ?mode=Exhibits&RequestTimeout=500&calledFromFrame=N
      &application_id=OoRMBTSnDFNsAu7OSl2Xkw==&fcc_id=U4GJTSHICN

  Attachment download (note: /eas/ not /oetcf/eas/reports/):
    https://apps.fcc.gov/eas/GetApplicationAttachment.html?id=9082570
"""

import json
import re
import time
from pathlib import Path
from typing import Optional
from urllib import parse as urllib_parse, request as urllib_request

import httpx
from loguru import logger

from .config import get_settings
from .fetcher import SpiderCloudProxy


# ---------------------------------------------------------------------------
# URL constants  (verified against live FCC pages)
# ---------------------------------------------------------------------------

_EXHIBITS_BASE = (
    "https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm"
)

# Direct attachment download — domain path is /eas/, NOT /oetcf/eas/reports/
_ATTACHMENT_BASE = "https://apps.fcc.gov/eas/GetApplicationAttachment.html"

# FCC generic search — used to resolve application_id from fcc_id
_EAS_SEARCH_URL = (
    "https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm"
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# URL builder
# ---------------------------------------------------------------------------

def _exhibits_url(fcc_id: str, application_id: str) -> str:
    """Build the ViewExhibitReport URL, safely encoding application_id."""
    # Decode first to avoid double-encoding (%3D%3D → == → %3D%3D)
    app_id = urllib_parse.unquote(application_id)
    params = urllib_parse.urlencode(
        {
            "mode": "Exhibits",
            "RequestTimeout": "500",
            "calledFromFrame": "N",
            "application_id": app_id,
            "fcc_id": fcc_id,
        }
    )
    return f"{_EXHIBITS_BASE}?{params}"


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _parse_label_attachments(html: str) -> list[tuple[str, str]]:
    """
    Return ``[(attachment_id, description), ...]`` for every table row
    whose description cell contains 'label' (case-insensitive).

    FCC exhibit table structure (typical):
      <tr>
        <td>Label</td>                                     ← description
        <td><a href="...GetApplicationAttachment...?id=9082570">file.pdf</a></td>
        <td>45 KB</td>
      </tr>

    Strategy: scan every <tr>, strip tags for keyword check, extract id from link.
    """
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    row_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    att_re = re.compile(
        r"GetApplicationAttachment\.html[^\"']*[?&]id=(\d+)",
        re.IGNORECASE,
    )
    # Description text is in the first <td> of the row
    td_re = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)

    for row_m in row_re.finditer(html):
        row_html = row_m.group(1)

        # Check plain text of the whole row for "label" or "antenna"
        row_text = re.sub(r"<[^>]+>", "", row_html).lower()
        if "label" not in row_text and "antenna" not in row_text:
            continue

        # Pull attachment IDs from this row
        for att_m in att_re.finditer(row_html):
            att_id = att_m.group(1)
            if att_id in seen:
                continue
            seen.add(att_id)

            # Use the first <td> text as the description
            first_td = td_re.search(row_html)
            desc = (
                re.sub(r"<[^>]+>", "", first_td.group(1)).strip()
                if first_td
                else "Document"
            )
            results.append((att_id, desc))
            logger.debug(f"[PDFFetcher] label attachment: id={att_id} desc='{desc}'")

    return results


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def _is_pdf(data: bytes) -> bool:
    return len(data) > 4 and data[:4] == b"%PDF"


def _direct_get(attachment_id: str) -> Optional[bytes]:
    """Plain HTTPS download — works when FCC attachment endpoint is open."""
    url = f"{_ATTACHMENT_BASE}?id={attachment_id}"
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(
                url,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Referer": "https://apps.fcc.gov/",
                },
            )
            if resp.status_code == 200 and _is_pdf(resp.content):
                return resp.content
            logger.debug(
                f"[PDFFetcher] direct GET id={attachment_id} → "
                f"HTTP {resp.status_code}, {len(resp.content)} B, "
                f"ct={resp.headers.get('content-type', '?')}"
            )
    except Exception as exc:
        logger.debug(f"[PDFFetcher] direct error id={attachment_id}: {exc}")
    return None


def _spidercloud_get_bytes(
    attachment_id: str, proxy: SpiderCloudProxy
) -> Optional[bytes]:
    """Download via spider.cloud bytes format (PDF returned as base64 in JSON)."""
    import base64

    url = f"{_ATTACHMENT_BASE}?id={attachment_id}"
    payload = proxy._build_payload(
        url,
        return_format=SpiderCloudProxy.FORMAT_BYTES,
        extra={"render_js": False},
    )
    api_url = proxy.BASE_URL + proxy.SCRAPE_ENDPOINT
    try:
        resp = proxy.session.post(api_url, json=payload)
        resp.raise_for_status()
        data = resp.json()

        raw = None
        if isinstance(data, list) and data:
            item = data[0]
            raw = item.get("content") or item.get("bytes") or item.get("body")
        elif isinstance(data, dict):
            raw = data.get("content") or data.get("bytes") or data.get("body")

        if not raw:
            logger.warning(f"[PDFFetcher] spider.cloud empty body id={attachment_id}")
            return None

        if isinstance(raw, bytes) and _is_pdf(raw):
            return raw
        if isinstance(raw, str):
            try:
                decoded = base64.b64decode(raw)
                if _is_pdf(decoded):
                    return decoded
            except Exception:
                pass
            logger.warning(
                f"[PDFFetcher] spider.cloud non-PDF response id={attachment_id}"
            )
    except Exception as exc:
        logger.error(f"[PDFFetcher] spider.cloud error id={attachment_id}: {exc}")
    return None


# ---------------------------------------------------------------------------
# Exhibits page fetch
# ---------------------------------------------------------------------------

def _fetch_exhibits(url: str, proxy: Optional[SpiderCloudProxy]) -> Optional[str]:
    # Try direct first — FCC exhibit pages are usually accessible without Akamai bypass
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": _USER_AGENT, "Referer": "https://apps.fcc.gov/"})
            if resp.status_code == 200 and len(resp.text) > 200:
                return resp.text
            logger.debug(f"[PDFFetcher] direct exhibits HTTP {resp.status_code} for {url[:80]}")
    except Exception as exc:
        logger.debug(f"[PDFFetcher] direct exhibits fetch failed: {exc}")

    # Fallback to SpiderCloud proxy if available
    if proxy:
        return proxy.fetch_html(url)
    return None


# ---------------------------------------------------------------------------
# Browserless exhibits fetch  (Akamai bypass via managed browser)
# ---------------------------------------------------------------------------

_BROWSERLESS_EXHIBITS_AND_DOWNLOAD_JS = """
export default async ({ page, context }) => {
  const url = context.url;
  await page.setUserAgent('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36');
  await page.setViewport({ width: 1280, height: 800 });
  try {
    // Step 1: load exhibits page — establishes Akamai session cookies
    await page.goto(url, { waitUntil: 'networkidle2', timeout: 60000 });
    const title = await page.title();
    if (title.toLowerCase().includes('access denied')) throw new Error('Blocked by Akamai');

    // Step 2: find Label attachments by inspecting each link's direct parent row.
    // Using link.closest('tr') avoids the outer wrapper <tr> that contains the
    // entire table as innerText and would match every attachment.
    // We only keep rows that have direct <td> children (real data rows) and whose
    // cell text includes "label" (exhibit type column).
    const attachments = await page.evaluate(() => {
      const results = [];
      const seen = new Set();
      const attRe = /GetApplicationAttachment[^"']*[?&]id=(\\d+)/i;
      for (const link of document.querySelectorAll('a[href*="GetApplicationAttachment"]')) {
        const href = link.getAttribute('href') || '';
        const m = href.match(attRe);
        if (!m || seen.has(m[1])) continue;
        const row = link.closest('tr');
        if (!row) continue;
        // Only real data rows have direct <td> children (not wrapper rows)
        const directCells = Array.from(row.children).filter(n => n.tagName === 'TD' || n.tagName === 'TH');
        if (directCells.length === 0) continue;
        const rowText = directCells.map(c => c.innerText).join('\\t').toLowerCase();
        if (!rowText.includes('label') && !rowText.includes('antenna')) continue;
        seen.add(m[1]);
        const desc = link.innerText.trim() || directCells[0].innerText.trim() || 'Document';
        results.push({ id: m[1], desc });
      }
      return results;
    });

    if (attachments.length === 0) return { attachments: [] };

    // Step 3: download each Label PDF using fetch() in browser context.
    // fetch() reuses the current page's Akamai session cookies, bypassing the
    // 403 that direct HTTP requests get.
    const pdfs = [];
    for (const att of attachments) {
      const attUrl = 'https://apps.fcc.gov/eas/GetApplicationAttachment.html?id=' + att.id;
      const pdfResult = await page.evaluate(async (u) => {
        try {
          const res = await fetch(u, { credentials: 'include' });
          const ct = res.headers.get('content-type') || '';
          if (res.status !== 200) return { error: 'HTTP ' + res.status };
          const buf = await res.arrayBuffer();
          const bytes = new Uint8Array(buf);
          let binary = '';
          const chunk = 8192;
          for (let i = 0; i < bytes.length; i += chunk) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
          }
          return { content: btoa(binary), contentType: ct, size: bytes.length };
        } catch(e) { return { error: e.message }; }
      }, attUrl);
      pdfs.push({ id: att.id, desc: att.desc, ...pdfResult });
    }

    return { attachments, pdfs };
  } catch (e) {
    return { error: e.message };
  }
}
"""


def _browserless_fetch_attachments(url: str) -> list[tuple[str, str]]:
    """Use Browserless /function to extract label attachment IDs from exhibits page."""
    settings = get_settings()
    cfg = settings.data_source.browserless
    if not cfg.api_key:
        logger.debug("[PDFFetcher] Browserless not configured, skipping")
        return []

    endpoint = f"https://production-{cfg.region}.browserless.io/function?token={cfg.api_key}"
    payload = {
        "code": _BROWSERLESS_EXHIBITS_AND_DOWNLOAD_JS,
        "context": {"url": url},
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Cache-Control": "no-cache"}

    try:
        req = urllib_request.Request(endpoint, data=data, headers=headers)
        with urllib_request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode())
        if "error" in result:
            logger.error(f"[PDFFetcher] Browserless exhibits error: {result['error']}")
            return []
        attachments = result.get("attachments", [])
        return [(a["id"], a.get("desc", "Document")) for a in attachments]
    except Exception as exc:
        logger.error(f"[PDFFetcher] Browserless exhibits fetch failed: {exc}")
        return []


def _browserless_fetch_label_pdfs(url: str, out_dir: Path) -> list[Path]:
    """
    Combined Browserless call: load exhibits page, find Label attachments, and
    download each PDF via fetch() in the browser context (uses Akamai session
    cookies).  Saves results to out_dir and returns list of saved paths.
    """
    import base64

    settings = get_settings()
    cfg = settings.data_source.browserless
    if not cfg.api_key:
        return []

    endpoint = f"https://production-{cfg.region}.browserless.io/function?token={cfg.api_key}"
    payload = {
        "code": _BROWSERLESS_EXHIBITS_AND_DOWNLOAD_JS,
        "context": {"url": url},
    }
    data = json.dumps(payload).encode("utf-8")
    headers_http = {"Content-Type": "application/json", "Cache-Control": "no-cache"}

    try:
        req = urllib_request.Request(endpoint, data=data, headers=headers_http)
        with urllib_request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode())
    except Exception as exc:
        logger.error(f"[PDFFetcher] Browserless combined call failed: {exc}")
        return []

    if "error" in result:
        logger.error(f"[PDFFetcher] Browserless error: {result['error']}")
        return []

    pdfs_data = result.get("pdfs", [])
    saved: list[Path] = []
    for item in pdfs_data:
        att_id = item.get("id", "unknown")
        desc = item.get("desc", "Document")
        if "error" in item:
            logger.warning(f"[PDFFetcher] PDF fetch error for id={att_id}: {item['error']}")
            continue
        b64 = item.get("content")
        if not b64:
            logger.warning(f"[PDFFetcher] Empty PDF content for id={att_id}")
            continue
        try:
            pdf_bytes = base64.b64decode(b64)
        except Exception as exc:
            logger.warning(f"[PDFFetcher] base64 decode failed for id={att_id}: {exc}")
            continue
        if not _is_pdf(pdf_bytes):
            logger.warning(f"[PDFFetcher] Non-PDF response for id={att_id} ({len(pdf_bytes)} B)")
            continue
        safe_desc = re.sub(r"[^\w\-]", "_", desc)[:50]
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"{safe_desc}_{att_id}.pdf"
        dest.write_bytes(pdf_bytes)
        logger.info(f"[PDFFetcher] Saved {len(pdf_bytes):,} B → {dest}")
        saved.append(dest)

    return saved


# ---------------------------------------------------------------------------
# application_id resolver  (optional — only if caller doesn't supply it)
# ---------------------------------------------------------------------------

def _resolve_application_id(
    fcc_id: str, proxy: Optional[SpiderCloudProxy]
) -> Optional[str]:
    """
    Try to discover the base64 application_id for *fcc_id* from FCC search
    results.  The results page links to applications via URLs like:
      ViewGrantApplication.cfm?...&application_id=OoRMBTSnDFNsAu7OSl2Xkw%3D%3D
    """
    grantee_code = fcc_id[:3]
    params = urllib_parse.urlencode(
        {"grantee_code": grantee_code, "fcc_id": fcc_id, "show_records": "10",
         "action": "Submit"}
    )
    url = f"{_EAS_SEARCH_URL}?{params}"

    html = _fetch_exhibits(url, proxy)
    if not html:
        return None

    # application_id is NOT always numeric — can be base64 (e.g. OoRMBTSnDF...==)
    match = re.search(
        r"application_id=([A-Za-z0-9+/=%]+)", html, re.IGNORECASE
    )
    if match:
        app_id = urllib_parse.unquote(match.group(1))
        logger.debug(f"[PDFFetcher] resolved application_id={app_id!r} for {fcc_id}")
        return app_id

    logger.warning(f"[PDFFetcher] could not resolve application_id for {fcc_id}")
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class PDFFetcher:
    """Download FCC Label PDF exhibits for a given FCC ID."""

    def __init__(self, output_dir: str = "data/pdfs"):
        self._settings = get_settings()
        self._output_root = Path(output_dir)
        self._output_root.mkdir(parents=True, exist_ok=True)
        self._proxy: Optional[SpiderCloudProxy] = None

        cfg = self._settings.data_source.spidercloud
        if cfg.enabled and cfg.api_key:
            self._proxy = SpiderCloudProxy(
                api_key=cfg.api_key,
                stealth=True,
                proxy_enabled=True,
                render_js=True,
                timeout=cfg.timeout,
                max_retries=cfg.max_retries,
            )

    def close(self):
        if self._proxy:
            self._proxy.close()

    def fetch_label_pdfs(
        self,
        fcc_id: str,
        application_id: Optional[str] = None,
    ) -> list[Path]:
        """
        Download all Label PDF exhibits for *fcc_id*.

        Args:
            fcc_id:         e.g. ``"U4GJTSHICN"``
            application_id: Base64 application ID from FCC URL.
                            If omitted, will attempt to resolve from search results.

        Returns:
            List of Paths for downloaded PDFs; empty list on failure.
        """
        if not application_id:
            application_id = _resolve_application_id(fcc_id, self._proxy)
        if not application_id:
            logger.warning(f"[PDFFetcher] No application_id for {fcc_id} — aborting")
            return []

        url = _exhibits_url(fcc_id, application_id)
        logger.info(f"[PDFFetcher] Fetching exhibits: {url}")

        # Step 1: try direct HTTP + SpiderCloud to get exhibits HTML
        attachments: list[tuple[str, str]] = []
        html = _fetch_exhibits(url, self._proxy)
        if html:
            attachments = _parse_label_attachments(html)
            if not attachments:
                logger.debug(f"[PDFFetcher] HTML snippet: {html[:600]}")

        out_dir = self._output_root / fcc_id

        # Step 2: if we found attachments via HTML, try direct/SpiderCloud download
        if attachments:
            logger.info(
                f"[PDFFetcher] {len(attachments)} Label attachment(s) for {fcc_id}: "
                + ", ".join(f"id={a[0]}" for a in attachments)
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            saved: list[Path] = []
            for att_id, desc in attachments:
                pdf_bytes = _direct_get(att_id)
                if not pdf_bytes and self._proxy:
                    logger.info(f"[PDFFetcher] Direct failed, trying spider.cloud id={att_id}")
                    pdf_bytes = _spidercloud_get_bytes(att_id, self._proxy)
                if not pdf_bytes:
                    logger.warning(f"[PDFFetcher] Direct/SpiderCloud failed id={att_id}, will retry via Browserless")
                    break  # fall through to Browserless combined path
                safe_desc = re.sub(r"[^\w\-]", "_", desc)[:50]
                dest = out_dir / f"{safe_desc}_{att_id}.pdf"
                dest.write_bytes(pdf_bytes)
                logger.info(f"[PDFFetcher] Saved {len(pdf_bytes):,} B → {dest}")
                saved.append(dest)
                time.sleep(0.5)
            if saved:
                return saved

        # Step 3: Browserless combined path — finds Label attachments AND downloads
        # PDFs in one session (uses browser's Akamai session cookies for the download)
        logger.info(f"[PDFFetcher] Using Browserless combined fetch+download for {fcc_id}")
        saved = _browserless_fetch_label_pdfs(url, out_dir)
        if saved:
            return saved

        logger.warning(f"[PDFFetcher] No Label PDFs obtained for {fcc_id}")
        return []
