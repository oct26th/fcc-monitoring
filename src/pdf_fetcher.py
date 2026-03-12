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

import re
import time
from pathlib import Path
from typing import Optional
from urllib import parse as urllib_parse

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

        # Check plain text of the whole row for "label"
        row_text = re.sub(r"<[^>]+>", "", row_html).lower()
        if "label" not in row_text:
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
                else "Label"
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
    if proxy:
        html = proxy.fetch_html(url)
        return html

    # No proxy: try direct (may hit Akamai)
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": _USER_AGENT})
            if resp.status_code == 200:
                return resp.text
    except Exception as exc:
        logger.error(f"[PDFFetcher] direct exhibits fetch failed: {exc}")
    return None


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

        html = _fetch_exhibits(url, self._proxy)
        if not html:
            logger.error(f"[PDFFetcher] Could not fetch exhibits page for {fcc_id}")
            return []

        attachments = _parse_label_attachments(html)
        if not attachments:
            logger.warning(f"[PDFFetcher] No Label attachments found for {fcc_id}")
            logger.debug(f"[PDFFetcher] HTML snippet: {html[:600]}")
            return []

        logger.info(
            f"[PDFFetcher] {len(attachments)} Label attachment(s) for {fcc_id}: "
            + ", ".join(f"id={a[0]}" for a in attachments)
        )

        out_dir = self._output_root / fcc_id
        out_dir.mkdir(parents=True, exist_ok=True)

        saved: list[Path] = []
        for att_id, desc in attachments:
            pdf_bytes = _direct_get(att_id)

            if not pdf_bytes and self._proxy:
                logger.info(
                    f"[PDFFetcher] Direct failed, trying spider.cloud id={att_id}"
                )
                pdf_bytes = _spidercloud_get_bytes(att_id, self._proxy)

            if not pdf_bytes:
                logger.error(f"[PDFFetcher] Failed to download id={att_id}")
                continue

            safe_desc = re.sub(r"[^\w\-]", "_", desc)[:50]
            dest = out_dir / f"{safe_desc}_{att_id}.pdf"
            dest.write_bytes(pdf_bytes)
            logger.info(f"[PDFFetcher] Saved {len(pdf_bytes):,} B → {dest}")
            saved.append(dest)

            time.sleep(0.5)  # light rate limiting

        return saved
