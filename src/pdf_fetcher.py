"""
FCC Label PDF Fetcher

Problem (FIXED):
  Spider.cloud is stateless — each /scrape call is a fresh browser session.
  Fetching the exhibits page or PDF as a *new* Spider.cloud request has no FCC
  session cookies, so Akamai flags it as a bot and returns an error page.

Solution — cookie chaining:
  1. The search request (SpiderCloudScriptFetcher) now uses return_cookies=True
     and stores the resulting cookies in ``fetcher.last_session_cookies``.
  2. PDFFetcher.fetch_label_pdfs accepts those cookies and passes them to every
     subsequent Spider.cloud request (exhibits page, etc.) via the ``cookies``
     parameter — making each request appear to be part of the same browser
     session that Akamai already approved.
  3. PDF binary download is attempted first via plain httpx with Cookie header
     (often works without Spider.cloud; no binary-relay issues).

Flow:
  fetch_label_pdfs(fcc_id, application_id, session_cookies)
    ├─ [already have app_id?] skip step a
    │  [else] (a) _resolve_app_id_with_session() — Spider.cloud GET + cookies
    ├─ (b) _fetch_exhibits_html() — Spider.cloud GET with session cookies
    ├─ (c) _parse_label_attachments() — find GetApplicationAttachment ids
    └─ for each id:
         ├─ _direct_get(id, cookies)  — plain httpx (fast, no API cost)
         └─ [fallback] _spidercloud_bytes(id, proxy, cookies)

Real URL examples (verified against live FCC):
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

# Direct attachment download — /eas/ NOT /oetcf/eas/reports/
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
    app_id = urllib_parse.unquote(application_id)  # avoid double-encoding
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
    Return ``[(attachment_id, description), ...]`` for Label rows in the
    ViewExhibitReport table.

    FCC exhibit table structure (typical):
      <tr>
        <td>Label</td>
        <td><a href="...GetApplicationAttachment...?id=9082570">file.pdf</a></td>
        <td>45 KB</td>
      </tr>
    """
    results: list[tuple[str, str]] = []
    seen: set[str] = set()

    row_re = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    att_re = re.compile(
        r"GetApplicationAttachment\.html[^\"']*[?&]id=(\d+)",
        re.IGNORECASE,
    )
    td_re = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)

    for row_m in row_re.finditer(html):
        row_html = row_m.group(1)
        row_text = re.sub(r"<[^>]+>", "", row_html).lower()
        if "label" not in row_text:
            continue
        for att_m in att_re.finditer(row_html):
            att_id = att_m.group(1)
            if att_id in seen:
                continue
            seen.add(att_id)
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


def _direct_get(attachment_id: str, session_cookies: str = "") -> Optional[bytes]:
    """
    Plain HTTPS download of a PDF attachment.

    Passes *session_cookies* as a Cookie header so Akamai sees this as a
    continuation of an established browser session rather than a new bot request.
    """
    url = f"{_ATTACHMENT_BASE}?id={attachment_id}"
    headers: dict = {
        "User-Agent": _USER_AGENT,
        "Referer": "https://apps.fcc.gov/",
    }
    if session_cookies:
        headers["Cookie"] = session_cookies

    try:
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
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


def _spidercloud_bytes(
    attachment_id: str,
    proxy: SpiderCloudProxy,
    session_cookies: str = "",
) -> Optional[bytes]:
    """
    Download PDF via spider.cloud with return_format=bytes.

    Passes *session_cookies* so the request appears part of the same session.
    """
    import base64

    url = f"{_ATTACHMENT_BASE}?id={attachment_id}"
    payload = proxy._build_payload(
        url,
        return_format=SpiderCloudProxy.FORMAT_BYTES,
        extra={"render_js": False},
    )
    if session_cookies:
        payload["cookies"] = session_cookies

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
# Exhibits page fetch  (with cookie chaining)
# ---------------------------------------------------------------------------

def _fetch_exhibits_html(
    url: str,
    proxy: Optional[SpiderCloudProxy],
    session_cookies: str = "",
) -> Optional[str]:
    """
    Fetch the ViewExhibitReport HTML.

    Passes *session_cookies* so Spider.cloud (and therefore Akamai on apps.fcc.gov)
    sees this as part of the same session established during the search step.
    """
    if proxy:
        html, new_cookies = proxy.fetch_html_with_cookies(url, incoming_cookies=session_cookies)
        return html

    # No proxy: try direct (may hit Akamai without prior session)
    try:
        headers: dict = {"User-Agent": _USER_AGENT}
        if session_cookies:
            headers["Cookie"] = session_cookies
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.text
    except Exception as exc:
        logger.error(f"[PDFFetcher] direct exhibits fetch failed: {exc}")
    return None


# ---------------------------------------------------------------------------
# application_id resolver  (with cookie chaining)
# ---------------------------------------------------------------------------

def _resolve_app_id_with_session(
    fcc_id: str,
    proxy: Optional[SpiderCloudProxy],
    session_cookies: str = "",
) -> tuple[Optional[str], str]:
    """
    Resolve the base64 application_id for *fcc_id* from FCC search results.

    Returns ``(application_id, cookies)`` — cookies may be updated by this
    request and should replace the caller's session_cookies for subsequent use.

    The results page links to applications via URLs like:
      ViewGrantApplication.cfm?...&application_id=OoRMBTSnDFNsAu7OSl2Xkw%3D%3D
    """
    grantee_code = fcc_id[:3]
    params = urllib_parse.urlencode(
        {"grantee_code": grantee_code, "fcc_id": fcc_id,
         "show_records": "10", "action": "Submit"}
    )
    url = f"{_EAS_SEARCH_URL}?{params}"

    new_cookies = session_cookies
    if proxy:
        html, fetched_cookies = proxy.fetch_html_with_cookies(
            url, incoming_cookies=session_cookies
        )
        if fetched_cookies:
            new_cookies = fetched_cookies
    else:
        headers: dict = {"User-Agent": _USER_AGENT}
        if session_cookies:
            headers["Cookie"] = session_cookies
        html = None
        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                resp = client.get(url, headers=headers)
                if resp.status_code == 200:
                    html = resp.text
        except Exception as exc:
            logger.error(f"[PDFFetcher] direct app_id resolve failed: {exc}")

    if not html:
        return None, new_cookies

    match = re.search(
        r"application_id=([A-Za-z0-9+/=%]+)", html, re.IGNORECASE
    )
    if match:
        app_id = urllib_parse.unquote(match.group(1))
        logger.debug(f"[PDFFetcher] resolved application_id={app_id!r} for {fcc_id}")
        return app_id, new_cookies

    logger.warning(f"[PDFFetcher] could not resolve application_id for {fcc_id}")
    return None, new_cookies


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
        session_cookies: str = "",
    ) -> list[Path]:
        """
        Download all Label PDF exhibits for *fcc_id*.

        Args:
            fcc_id:          e.g. ``"U4GJTSHICN"``
            application_id:  Base64 application ID from FCC URL.
                             If omitted, will attempt to resolve from search results.
            session_cookies: Cookie-header string captured from a prior Spider.cloud
                             search request (``fetcher.last_session_cookies``).
                             Passing these keeps all requests in the same Akamai
                             session and prevents bot-detection blocks.

        Returns:
            List of Paths for downloaded PDFs; empty list on failure.
        """
        cookies = session_cookies

        if not application_id:
            application_id, cookies = _resolve_app_id_with_session(
                fcc_id, self._proxy, cookies
            )
        if not application_id:
            logger.warning(f"[PDFFetcher] No application_id for {fcc_id} — aborting")
            return []

        url = _exhibits_url(fcc_id, application_id)
        logger.info(f"[PDFFetcher] Fetching exhibits: {url}")

        html = _fetch_exhibits_html(url, self._proxy, cookies)
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
            # Try direct httpx first (cheapest, avoids Spider.cloud binary-relay issues)
            pdf_bytes = _direct_get(att_id, session_cookies=cookies)

            if not pdf_bytes and self._proxy:
                logger.info(
                    f"[PDFFetcher] Direct failed; trying spider.cloud bytes id={att_id}"
                )
                pdf_bytes = _spidercloud_bytes(att_id, self._proxy, session_cookies=cookies)

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
