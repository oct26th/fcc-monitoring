#!/usr/bin/env python3
"""
test_pdf_chain.py — Standalone smoke-test for Spider.cloud stateful PDF chain.

Problem:
  Spider.cloud is stateless — each /scrape call is a fresh browser with no FCC
  session cookies.  Akamai blocks the exhibits page and PDF download because
  they look like new bot requests.

Solution:
  1. Search request with return_cookies=True  → capture FCC/Akamai session cookies
  2. Exhibits page GET with those cookies    → real HTML (not Akamai block)
  3. Parse Label attachment IDs from HTML
  4. Direct httpx download with cookies      → PDF bytes (no Spider.cloud needed)
  5. Spider.cloud bytes fallback if step 4 blocked

Usage:
  SPIDERCLOUD_API_KEY=sk-... python3 scripts/test_pdf_chain.py <fcc_id>
  e.g.:  python3 scripts/test_pdf_chain.py UZ7ZZ0NVJ4

Expected:
  [Step 1] N records found — cookies: <...>
  [Step 2] exhibits HTML XXXX bytes — 1 label attachment(s) found
  [Step 3] ✓ Saved 45,102 B → /tmp/test_label_XXXXXXX.pdf
"""

import base64
import os
import re
import sys
from pathlib import Path
from urllib import parse as urllib_parse

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("SPIDERCLOUD_API_KEY", "")
if not API_KEY:
    sys.exit("ERROR: SPIDERCLOUD_API_KEY not set")

FCC_ID = sys.argv[1] if len(sys.argv) > 1 else None
GRANTEE_CODE = FCC_ID[:3] if FCC_ID else "UZ7"

SCRAPE_URL = "https://api.spider.cloud/scrape"
FCC_SEARCH_URL = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm"
EXHIBITS_BASE = "https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm"
ATTACHMENT_BASE = "https://apps.fcc.gov/eas/GetApplicationAttachment.html"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Spider.cloud helper
# ---------------------------------------------------------------------------

def spidercloud_post(payload: dict) -> dict:
    """POST to Spider.cloud /scrape; return first item from response."""
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(
            SCRAPE_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    if isinstance(data, list) and data:
        return data[0]
    if isinstance(data, dict):
        return data
    return {}


def extract_html(item: dict) -> str:
    return item.get("content") or item.get("html") or ""


def extract_cookies(item: dict) -> str:
    """
    Return a Cookie-header string from Spider.cloud response.

    Spider.cloud may return cookies as:
      - str:  "name=value; name2=value2"
      - list: [{"name": "...", "value": "..."}, ...]
      - dict: {"name": "value", ...}
    """
    raw = item.get("cookies") or ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        return "; ".join(
            f"{c['name']}={c['value']}"
            for c in raw
            if isinstance(c, dict) and "name" in c
        )
    if isinstance(raw, dict):
        return "; ".join(f"{k}={v}" for k, v in raw.items())
    return ""


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def find_application_id(html: str, fcc_id: str) -> str | None:
    """Extract base64 application_id for a specific fcc_id from search results HTML."""
    # Search in a window around the fcc_id occurrence
    idx = html.lower().find(fcc_id.lower())
    if idx < 0:
        return None
    window = html[max(0, idx - 400) : idx + 200]
    m = re.search(r"application_id=([A-Za-z0-9+/=%]+)", window, re.IGNORECASE)
    if m:
        return urllib_parse.unquote(m.group(1))
    return None


def find_any_application_id(html: str) -> str | None:
    """Fallback: return the first application_id found anywhere in HTML."""
    m = re.search(r"application_id=([A-Za-z0-9+/=%]+)", html, re.IGNORECASE)
    return urllib_parse.unquote(m.group(1)) if m else None


ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
ATT_RE = re.compile(r"GetApplicationAttachment\.html[^\"']*[?&]id=(\d+)", re.IGNORECASE)
TD_RE  = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)


def parse_label_attachments(html: str) -> list[tuple[str, str]]:
    """Return [(attachment_id, description), ...] for Label rows."""
    results, seen = [], set()
    for row_m in ROW_RE.finditer(html):
        row_html = row_m.group(1)
        row_text = re.sub(r"<[^>]+>", "", row_html).lower()
        if "label" not in row_text:
            continue
        for att_m in ATT_RE.finditer(row_html):
            att_id = att_m.group(1)
            if att_id in seen:
                continue
            seen.add(att_id)
            first_td = TD_RE.search(row_html)
            desc = re.sub(r"<[^>]+>", "", first_td.group(1)).strip() if first_td else "Label"
            results.append((att_id, desc))
    return results


def is_akamai_block(html: str) -> bool:
    return bool(re.search(r"access denied|reference #\d+\.\d+|akamai", html, re.IGNORECASE))


def is_pdf(data: bytes) -> bool:
    return len(data) > 4 and data[:4] == b"%PDF"


# ---------------------------------------------------------------------------
# Step 1 — FCC search with automation_scripts + return_cookies
# ---------------------------------------------------------------------------

print(f"\n{'='*60}")
print(f"[Step 1] FCC search  grantee={GRANTEE_CODE}  fcc_id={FCC_ID or '(any)'}")
print('='*60)

js_fill = (
    f"document.querySelector('input[name=\"grantee_code\"]').value = '{GRANTEE_CODE}';"
    "document.querySelector('input[name=\"show_records\"]').value = '10';"
)
if FCC_ID:
    js_fill += (
        f"var fi = document.querySelector('input[name=\"fcc_id\"]');"
        f"if (fi) fi.value = '{FCC_ID}';"
    )

payload1 = {
    "url": FCC_SEARCH_URL,
    "request": "chrome",
    "automation_scripts": {
        FCC_SEARCH_URL: [
            {"Evaluate": js_fill},
            {"Click": "input[type='submit']"},
            {"WaitForNavigation": None},
        ]
    },
    "return_format": "html",
    "return_cookies": True,
    "stealth": True,
    "proxy_enabled": True,
}

item1 = spidercloud_post(payload1)
html1 = extract_html(item1)
cookies = extract_cookies(item1)

record_links = len(re.findall(r"ViewGrantApplication\.cfm", html1))
print(f"  HTML: {len(html1):,} bytes")
print(f"  Record links found: ~{record_links}")
print(f"  Cookies: {repr(cookies[:120]) if cookies else '(none)'}")

if not html1:
    sys.exit("ERROR: Empty HTML from search — check API key or FCC availability")

if is_akamai_block(html1):
    print("  WARNING: Akamai block detected in search results")

# ---------------------------------------------------------------------------
# Step 1b — Resolve application_id
# ---------------------------------------------------------------------------

app_id = None
if FCC_ID:
    app_id = find_application_id(html1, FCC_ID)
    print(f"  application_id (from search): {repr(app_id)}")

if not app_id:
    # Fall back to targeted GET with URL params + cookies to find application_id
    target_params = urllib_parse.urlencode({
        "grantee_code": GRANTEE_CODE,
        "fcc_id": FCC_ID or "",
        "show_records": "5",
        "action": "Submit",
    })
    target_url = f"{FCC_SEARCH_URL}?{target_params}"
    print(f"\n[Step 1b] Targeted search: {target_url}")

    payload1b = {
        "url": target_url,
        "request": "chrome",
        "return_format": "html",
        "return_cookies": True,
        "stealth": True,
        "proxy_enabled": True,
    }
    if cookies:
        payload1b["cookies"] = cookies

    item1b = spidercloud_post(payload1b)
    html1b = extract_html(item1b)
    new_cookies = extract_cookies(item1b)
    if new_cookies:
        cookies = new_cookies

    app_id = find_any_application_id(html1b)
    print(f"  application_id: {repr(app_id)}")
    print(f"  cookies: {repr(cookies[:120]) if cookies else '(none)'}")

if not app_id:
    print(f"  HTML snippet: {html1[:600]}")
    sys.exit("ERROR: Could not find application_id — check fcc_id argument")

# ---------------------------------------------------------------------------
# Step 2 — Exhibits page with session cookies
# ---------------------------------------------------------------------------

exhibits_url = (
    EXHIBITS_BASE + "?"
    + urllib_parse.urlencode({
        "mode": "Exhibits",
        "RequestTimeout": "500",
        "calledFromFrame": "N",
        "application_id": app_id,
        "fcc_id": FCC_ID or GRANTEE_CODE,
    })
)
print(f"\n{'='*60}")
print(f"[Step 2] Exhibits page")
print(f"  URL: {exhibits_url}")
print(f"  Passing cookies: {'yes' if cookies else 'no'}")
print('='*60)

payload2 = {
    "url": exhibits_url,
    "request": "chrome",
    "return_format": "html",
    "return_cookies": True,
    "stealth": True,
    "proxy_enabled": True,
}
if cookies:
    payload2["cookies"] = cookies

item2 = spidercloud_post(payload2)
html2 = extract_html(item2)
new_cookies = extract_cookies(item2)
if new_cookies:
    cookies = new_cookies

print(f"  HTML: {len(html2):,} bytes")
print(f"  Akamai block: {is_akamai_block(html2)}")
print(f"  cookies: {repr(cookies[:120]) if cookies else '(none)'}")

if not html2 or is_akamai_block(html2):
    print(f"  HTML snippet: {html2[:600]}")
    sys.exit("ERROR: Exhibits page blocked or empty")

attachments = parse_label_attachments(html2)
print(f"  Label attachments: {len(attachments)}")
for att_id, desc in attachments:
    print(f"    id={att_id}  desc={repr(desc)}")

if not attachments:
    print(f"  HTML snippet: {html2[:800]}")
    sys.exit("ERROR: No Label attachments found — check HTML parsing")

# ---------------------------------------------------------------------------
# Step 3 — Download PDFs
# ---------------------------------------------------------------------------

print(f"\n{'='*60}")
print(f"[Step 3] Downloading {len(attachments)} PDF(s)")
print('='*60)

success = 0
for att_id, desc in attachments:
    pdf_url = f"{ATTACHMENT_BASE}?id={att_id}"
    print(f"\n  Attachment id={att_id} ({desc})")

    # 3a. Direct httpx with session cookies
    try:
        dl_headers = {
            "User-Agent": USER_AGENT,
            "Referer": exhibits_url,
        }
        if cookies:
            dl_headers["Cookie"] = cookies

        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(pdf_url, headers=dl_headers)

        print(f"    Direct GET: HTTP {resp.status_code}, {len(resp.content):,} B, "
              f"ct={resp.headers.get('content-type', '?')}")

        if resp.status_code == 200 and is_pdf(resp.content):
            dest = Path(f"/tmp/test_label_{att_id}.pdf")
            dest.write_bytes(resp.content)
            print(f"    ✓ Direct download: {len(resp.content):,} B → {dest}")
            success += 1
            continue
    except Exception as exc:
        print(f"    Direct GET error: {exc}")

    # 3b. Spider.cloud bytes fallback
    print(f"    Trying Spider.cloud bytes fallback...")
    payload3 = {
        "url": pdf_url,
        "request": "chrome",
        "return_format": "bytes",
        "stealth": True,
        "proxy_enabled": True,
    }
    if cookies:
        payload3["cookies"] = cookies

    try:
        item3 = spidercloud_post(payload3)
        raw = item3.get("content") or item3.get("body") or b""
        if isinstance(raw, str):
            try:
                raw = base64.b64decode(raw)
            except Exception:
                raw = raw.encode()

        print(f"    Spider.cloud bytes: {len(raw):,} B, "
              f"magic={repr(raw[:4]) if raw else 'empty'}")

        if is_pdf(raw):
            dest = Path(f"/tmp/test_label_{att_id}.pdf")
            dest.write_bytes(raw)
            print(f"    ✓ Spider.cloud: {len(raw):,} B → {dest}")
            success += 1
        else:
            print(f"    ✗ Not a PDF: {repr(str(raw)[:200])}")
    except Exception as exc:
        print(f"    Spider.cloud error: {exc}")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print(f"\n{'='*60}")
print(f"SUMMARY: {success}/{len(attachments)} PDF(s) downloaded")
print('='*60)
if success == 0:
    sys.exit(1)
