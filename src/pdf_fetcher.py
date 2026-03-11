"""FCC Label PDF downloader.

FCC stores Label/ID exhibits as PDF attachments inside each application.
The attachment listing page URL pattern is:

  https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm
    ?mode=Exhibits&RequestTimeout=500&calledFromFrame=N
    &application_id={APPLICATION_ID}&ID_Code={FCC_ID}

From that listing we scrape the attachment rows and download all PDFs
whose exhibit description contains "label" (case-insensitive).

If we cannot resolve the application_id, we fall back to searching for
the FCC ID directly via the EAS search result link.

Saved files: data/pdfs/{FCC_ID}/{filename}.pdf
"""
import re
import time
from pathlib import Path
from typing import Optional
from urllib import request as urllib_request, parse

import httpx
from loguru import logger

from .config import get_settings


# ---------------------------------------------------------------------------
# FCC URL templates
# ---------------------------------------------------------------------------

_ATTACHMENT_LIST_URL = (
    "https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm"
    "?mode=Exhibits&RequestTimeout=500&calledFromFrame=N"
    "&application_id={application_id}&ID_Code={fcc_id}"
)

_ATTACHMENT_DOWNLOAD_URL = (
    "https://apps.fcc.gov/oetcf/eas/reports/GetApplicationAttachment.html"
    "?calledFromFrame=N&id={attachment_id}&idType=APPID"
)

_EAS_SEARCH_URL = (
    "https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm"
)


class PDFFetcher:
    """
    Download FCC Label PDF exhibits for a given FCC ID.

    Two-step process:
      1. Resolve the numeric application_id for the FCC ID.
      2. Scrape the exhibit listing page; download Label PDFs.

    Uses spider.cloud proxy (if configured) to bypass Akamai, or falls
    back to a direct httpx request.
    """

    def __init__(self, output_dir: str = "data/pdfs"):
        self._settings = get_settings()
        self._output_root = Path(output_dir)
        self._output_root.mkdir(parents=True, exist_ok=True)
        self._client = self._build_client()

    # ------------------------------------------------------------------
    # HTTP client
    # ------------------------------------------------------------------

    def _build_client(self) -> httpx.Client:
        """Build an httpx client, optionally routed through spider.cloud proxy."""
        cfg = self._settings.data_source.spidercloud
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        # If spider.cloud is enabled, add auth header so we can route through it
        if cfg.enabled and cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"

        return httpx.Client(
            timeout=60.0,
            follow_redirects=True,
            headers=headers,
        )

    def close(self):
        self._client.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_label_pdfs(
        self, fcc_id: str, application_id: Optional[str] = None
    ) -> list[Path]:
        """
        Download all Label PDF exhibits for *fcc_id*.

        Args:
            fcc_id:         FCC ID (e.g. "V2XMOBILE01")
            application_id: Optional numeric application ID.  If omitted we
                            attempt to resolve it from the FCC search page.

        Returns:
            List of local Path objects for successfully downloaded PDFs.
            Empty list on failure.
        """
        if application_id is None:
            application_id = self._resolve_application_id(fcc_id)

        if not application_id:
            logger.warning(f"[PDFFetcher] Cannot resolve application_id for {fcc_id}")
            return []

        exhibit_url = _ATTACHMENT_LIST_URL.format(
            application_id=application_id, fcc_id=fcc_id
        )
        logger.info(f"[PDFFetcher] Fetching exhibit list: {exhibit_url}")

        html = self._get_html(exhibit_url)
        if not html:
            logger.error(f"[PDFFetcher] Failed to fetch exhibit page for {fcc_id}")
            return []

        attachment_ids = _parse_label_attachment_ids(html)
        if not attachment_ids:
            logger.info(f"[PDFFetcher] No Label attachments found for {fcc_id}")
            return []

        logger.info(
            f"[PDFFetcher] Found {len(attachment_ids)} Label attachment(s) for {fcc_id}"
        )

        out_dir = self._output_root / fcc_id
        out_dir.mkdir(parents=True, exist_ok=True)

        saved: list[Path] = []
        for att_id, att_desc in attachment_ids:
            path = self._download_pdf(att_id, att_desc, fcc_id, out_dir)
            if path:
                saved.append(path)
            time.sleep(0.5)  # gentle rate limiting

        return saved

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_application_id(self, fcc_id: str) -> Optional[str]:
        """
        Try to extract the numeric application_id from the FCC search result
        page for this FCC ID.

        The search results page links to individual applications via URLs like:
          ViewGrantApplication.cfm?...application_id=12345...
        """
        grantee_code = fcc_id[:3]  # first 3 chars are always grantee code
        form_data = parse.urlencode({
            "grantee_code": grantee_code,
            "fcc_id": fcc_id,
            "show_records": "20",
            "action": "Submit",
        })
        url = f"{_EAS_SEARCH_URL}?{form_data}"

        html = self._get_html(url)
        if not html:
            return None

        # Look for application_id in links on the results page
        match = re.search(r"application_id=(\d+)", html, re.IGNORECASE)
        if match:
            app_id = match.group(1)
            logger.debug(f"[PDFFetcher] Resolved application_id={app_id} for {fcc_id}")
            return app_id

        logger.warning(f"[PDFFetcher] Could not find application_id for {fcc_id}")
        return None

    def _get_html(self, url: str) -> Optional[str]:
        """Fetch URL and return response text, or None on failure."""
        cfg = self._settings.data_source.spidercloud

        # Route through spider.cloud /scrape for HTML pages (GET only — safe here)
        if cfg.enabled and cfg.api_key:
            return self._spidercloud_get(url)

        # Direct request fallback
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError as exc:
            logger.error(f"[PDFFetcher] GET {url} failed: {exc}")
            return None

    def _spidercloud_get(self, url: str) -> Optional[str]:
        """GET a URL through spider.cloud /scrape (GET-only, no POST bug here)."""
        cfg = self._settings.data_source.spidercloud
        payload = {
            "url": url,
            "return_format": "html",
            "stealth": 1,
            "proxy_enabled": True,
            "render_js": True,
            "anti_bot": True,
        }
        try:
            resp = httpx.post(
                f"{cfg.base_url}/scrape",
                json=payload,
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=cfg.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                return data[0].get("content") or data[0].get("html") or ""
            if isinstance(data, dict):
                return data.get("content") or data.get("html") or ""
        except Exception as exc:
            logger.error(f"[PDFFetcher] spider.cloud GET failed for {url}: {exc}")
        return None

    def _download_pdf(
        self,
        attachment_id: str,
        description: str,
        fcc_id: str,
        out_dir: Path,
    ) -> Optional[Path]:
        """Download a single PDF attachment and save it to *out_dir*."""
        download_url = _ATTACHMENT_DOWNLOAD_URL.format(attachment_id=attachment_id)

        # Sanitise description for use as filename
        safe_desc = re.sub(r"[^\w\-]", "_", description)[:60]
        filename = f"{safe_desc}_{attachment_id}.pdf"
        out_path = out_dir / filename

        if out_path.exists():
            logger.debug(f"[PDFFetcher] Already downloaded: {out_path}")
            return out_path

        logger.info(f"[PDFFetcher] Downloading: {download_url}")
        try:
            # PDFs must be downloaded as raw bytes; use direct httpx (no proxy needed
            # since the attachment endpoint is a direct download link)
            resp = self._client.get(download_url)
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "pdf" not in content_type.lower() and len(resp.content) < 100:
                logger.warning(
                    f"[PDFFetcher] Unexpected content-type '{content_type}' for {filename}"
                )

            out_path.write_bytes(resp.content)
            logger.info(
                f"[PDFFetcher] Saved {len(resp.content):,} bytes → {out_path}"
            )
            return out_path

        except httpx.HTTPError as exc:
            logger.error(f"[PDFFetcher] Download failed for {attachment_id}: {exc}")
            return None


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _parse_label_attachment_ids(html: str) -> list[tuple[str, str]]:
    """
    Parse the FCC exhibit listing page and return (attachment_id, description)
    tuples for all exhibits whose description contains "label".

    The exhibit table rows look like:
      <a href="GetApplicationAttachment.html?...id=98765...">Label</a>
    """
    results: list[tuple[str, str]] = []

    # Match all GetApplicationAttachment links with their surrounding text
    pattern = re.compile(
        r'<a[^>]+GetApplicationAttachment\.html[^>]*id=(\d+)[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )

    for match in pattern.finditer(html):
        att_id = match.group(1)
        link_text = re.sub(r"<[^>]+>", "", match.group(2)).strip()

        if "label" in link_text.lower():
            results.append((att_id, link_text))
            logger.debug(f"[PDFFetcher] Label attachment found: id={att_id} '{link_text}'")

    return results
