"""
V2 競品情報網 — Brand Source Registry
======================================
Registry of the 10 target brands' official Newsroom and Products URLs.

Aligned with settings.yaml target_grantees (same 10 companies).
Each BrandSource entry is the single source of truth for:
  - newsroom_url  : Official press releases / news
  - products_url  : Products listing page
  - crawl_type    : "spider" (spider.cloud) | "direct" (plain httpx)
  - js_required   : Whether the page needs JS execution to render content
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class BrandSource:
    """Registry entry for a single brand's intelligence URLs."""
    brand_id: str                     # matches settings.yaml name (lowercase, no spaces)
    display_name: str                 # human-readable brand name
    grantee_codes: List[str]          # FCC grantee codes (link back to V1)
    newsroom_urls: List[str]          # 1+ newsroom/press-release URLs
    products_urls: List[str]          # 1+ product listing URLs
    crawl_type: str = "spider"        # "spider" | "direct"
    js_required: bool = True          # Most modern brand sites need JS
    notes: str = ""                   # Known quirks / parsing notes


# ---------------------------------------------------------------------------
# 10 Brand Registry
# ---------------------------------------------------------------------------

BRAND_SOURCES: List[BrandSource] = [

    BrandSource(
        brand_id="zebra",
        display_name="Zebra Technologies",
        grantee_codes=["UZ7"],
        newsroom_urls=[
            "https://www.zebra.com/us/en/about-zebra/newsroom.html",
            "https://www.zebra.com/us/en/about-zebra/newsroom/press-releases.html",
        ],
        products_urls=[
            "https://www.zebra.com/us/en/products.html",
            "https://www.zebra.com/us/en/products/mobile-computers.html",
        ],
        js_required=True,
        notes="Heavy JS SPA. Products may need pagination. Newsroom has category filter.",
    ),

    BrandSource(
        brand_id="honeywell",
        display_name="Honeywell Safety & Productivity Solutions",
        grantee_codes=["HD5"],
        newsroom_urls=[
            "https://sps.honeywell.com/us/en/news",
            "https://www.honeywell.com/us/en/press/releases",
        ],
        products_urls=[
            "https://sps.honeywell.com/us/en/products/productivity/mobile-computers",
            "https://sps.honeywell.com/us/en/products",
        ],
        js_required=True,
        notes="SPS subdomain is the relevant division. May require region cookie.",
    ),

    BrandSource(
        brand_id="datalogic",
        display_name="Datalogic",
        grantee_codes=["U4F", "U4G"],
        newsroom_urls=[
            "https://www.datalogic.com/news.html",
            "https://www.datalogic.com/press-releases.html",
        ],
        products_urls=[
            "https://www.datalogic.com/products.html",
            "https://www.datalogic.com/products/mobile-computers.html",
        ],
        js_required=True,
        notes="Standard JS site. Products organized by category.",
    ),

    BrandSource(
        brand_id="point_mobile",
        display_name="Point Mobile",
        grantee_codes=["V2X"],
        newsroom_urls=[
            "https://www.pointmobile.com/news-events",
            "https://www.pointmobile.com/blog",
        ],
        products_urls=[
            "https://www.pointmobile.com/products",
            "https://www.pointmobile.com/enterprise-mobile-computer",
        ],
        js_required=True,
        notes="Korean manufacturer. English site. Some pages may 301 redirect.",
    ),

    BrandSource(
        brand_id="bluebird",
        display_name="Bluebird",
        grantee_codes=["SS4"],
        newsroom_urls=[
            "https://www.bluebird.co.kr/en/news/news_list.asp",
            "https://www.bluebird.co.kr/en/news/press_list.asp",
        ],
        products_urls=[
            "https://www.bluebird.co.kr/en/products/products_list.asp",
        ],
        js_required=False,
        crawl_type="direct",
        notes="Korean site with English version. Older ASP stack — may not need full JS.",
    ),

    BrandSource(
        brand_id="unitech",
        display_name="Unitech",
        grantee_codes=["HLE"],
        newsroom_urls=[
            "https://www.unitechamerica.com/news-room",
            "https://www.unitech-group.com/en/news",
        ],
        products_urls=[
            "https://www.unitechamerica.com/products",
            "https://www.unitech-group.com/en/product-list",
        ],
        js_required=True,
        notes="Has both US (.com) and global (-group.com) domains.",
    ),

    BrandSource(
        brand_id="proglove",
        display_name="ProGlove",
        grantee_codes=["2AOJL"],
        newsroom_urls=[
            "https://www.proglove.com/news/",
            "https://www.proglove.com/press-releases/",
        ],
        products_urls=[
            "https://www.proglove.com/products/",
        ],
        js_required=True,
        notes="German startup. Focused on wearable barcode scanners.",
    ),

    BrandSource(
        brand_id="chainway",
        display_name="Chainway",
        grantee_codes=["2AC6A"],
        newsroom_urls=[
            "https://www.chainway.net/news/",
            "https://www.chainway.net/blog/",
        ],
        products_urls=[
            "https://www.chainway.net/products/",
            "https://www.chainway.net/handheld/",
        ],
        js_required=True,
        notes="Chinese manufacturer. English site available.",
    ),

    BrandSource(
        brand_id="newland",
        display_name="Fujian Newland Auto-ID Tech",
        grantee_codes=["2AR9L"],
        newsroom_urls=[
            "https://www.newlandaidc.com/news/",
            "https://www.newlandaidc.com/newsroom/",
        ],
        products_urls=[
            "https://www.newlandaidc.com/products/",
            "https://www.newlandaidc.com/mobile-computers/",
        ],
        js_required=True,
        notes="AIDC division of Newland. English international site.",
    ),

    BrandSource(
        brand_id="generalscan",
        display_name="Generalscan",
        grantee_codes=["2ADNT"],
        newsroom_urls=[
            "https://www.generalscan.com/news/",
        ],
        products_urls=[
            "https://www.generalscan.com/products/",
            "https://www.generalscan.com/ring-scanner/",
        ],
        js_required=False,
        crawl_type="direct",
        notes="Smaller brand. Simpler site. May use direct fetch.",
    ),
]


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def get_brand_by_id(brand_id: str) -> Optional[BrandSource]:
    """Return a BrandSource by brand_id, or None if not found."""
    for brand in BRAND_SOURCES:
        if brand.brand_id == brand_id:
            return brand
    return None


def get_brand_by_grantee_code(code: str) -> Optional[BrandSource]:
    """Return a BrandSource that contains the given FCC grantee code."""
    for brand in BRAND_SOURCES:
        if code in brand.grantee_codes:
            return brand
    return None


def get_all_urls() -> List[tuple[str, str, str]]:
    """
    Flatten all brand URLs into a list of (brand_id, url_type, url) tuples.
    url_type is 'newsroom' or 'products'.
    """
    result = []
    for brand in BRAND_SOURCES:
        for url in brand.newsroom_urls:
            result.append((brand.brand_id, "newsroom", url))
        for url in brand.products_urls:
            result.append((brand.brand_id, "products", url))
    return result
