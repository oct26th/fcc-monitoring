#!/usr/bin/env python3
"""
獨立驗證腳本：用 spider.cloud execution_scripts 抓 FCC UZ7 搜尋結果

用法：
    SPIDERCLOUD_API_KEY=sk-... python scripts/test_spider_fcc.py

成功條件：印出 HTML 長度 > 1000，且包含 "UZ7"
"""
import os
import httpx

GRANTEE = "UZ7"
FCC_URL = "https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm"

JS = f"""
document.addEventListener("DOMContentLoaded", function() {{
    var gc = document.querySelector('input[name="grantee_code"]');
    if (gc) gc.value = "{GRANTEE}";
    var sr = document.querySelector('input[name="show_records"]');
    if (sr) sr.value = "500";
    var btn = document.querySelector('input[type="submit"]');
    if (btn) btn.click();
}});
"""

payload = {
    "url": FCC_URL,
    "request": "chrome",
    "execution_scripts": {FCC_URL: JS},
    "return_format": "html",
    "stealth": 1,
    "proxy_enabled": True,
    "anti_bot": True,
}

api_key = os.environ.get("SPIDERCLOUD_API_KEY", "")
if not api_key:
    raise SystemExit("ERROR: SPIDERCLOUD_API_KEY environment variable not set")

print(f"Sending request for grantee_code={GRANTEE} ...")
resp = httpx.post(
    "https://api.spider.cloud/scrape",
    json=payload,
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    timeout=120,
)
resp.raise_for_status()

data = resp.json()
if isinstance(data, list) and data:
    html = data[0].get("content") or data[0].get("html") or ""
elif isinstance(data, dict):
    html = data.get("content") or data.get("html") or ""
else:
    html = ""

print(f"HTML length: {len(html)}")
print("--- first 800 chars ---")
print(html[:800])
print("-----------------------")

assert len(html) > 1000, f"FAIL: HTML too short ({len(html)} bytes), likely no results returned"
assert GRANTEE in html, f"FAIL: grantee code '{GRANTEE}' not found in HTML"
print(f"\n✅ PASS: got {len(html)} bytes, '{GRANTEE}' found in response")
