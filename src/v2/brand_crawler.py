"""
V2 競品情報網 — Brand Intelligence Crawler Pipeline
=====================================================
Crawls 10 brand official Newsroom / Products pages via SpiderCloud,
writes raw content + parsed items to the Data Lake.

Pipeline per URL:
  1. Fetch HTML/markdown via SpiderCloudProxy (or direct httpx fallback)
  2. Detect if content is valid (not blocked, not empty)
  3. Persist raw content to DataLake.raw_pages + disk backup
  4. Basic parse: extract article titles/links or product names
  5. Persist parsed items to DataLake.news_articles / DataLake.products

Usage:
    from src.v2.brand_crawler import BrandCrawlerPipeline
    from src.v2.data_lake import DataLake

    with DataLake() as lake:
        pipeline = BrandCrawlerPipeline(lake)
        results = pipeline.run_all()           # crawl all 10 brands
        # or:
        results = pipeline.run_brand("zebra")  # single brand

Run from CLI:
    python -m src.v2.brand_crawler --brand zebra
    python -m src.v2.brand_crawler --all
"""

import argparse
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from loguru import logger

from ..config import get_settings
from ..fetcher import SpiderCloudProxy
from .brand_sources import BRAND_SOURCES, BrandSource, get_brand_by_id
from .data_lake import DataLake


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CrawlResult:
    """Outcome of a single URL crawl."""
    brand_id: str
    url: str
    url_type: str
    status: str                    # 'success' | 'error' | 'blocked' | 'empty'
    content: Optional[str] = None
    error_msg: Optional[str] = None
    duration_ms: int = 0
    articles_found: int = 0
    products_found: int = 0


@dataclass
class PipelineRun:
    """Summary of a full pipeline execution."""
    run_id: str
    brands_attempted: int = 0
    urls_attempted: int = 0
    urls_success: int = 0
    urls_error: int = 0
    articles_total: int = 0
    products_total: int = 0
    results: list[CrawlResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Content validators
# ---------------------------------------------------------------------------

def _is_blocked(content: str) -> bool:
    """Detect anti-bot / access-denied pages."""
    lower = content.lower()
    signals = [
        "access denied",
        "akamai",
        "reference #",
        "robot or automated",
        "your browser does not support",
        "cloudflare",
        "please enable javascript",
        "enable cookies",
        "403 forbidden",
        "pardon our interruption",
    ]
    return any(s in lower for s in signals)


def _has_meaningful_content(content: str, min_chars: int = 200) -> bool:
    """Check if content has enough text to be useful."""
    if not content:
        return False
    # Strip HTML tags for length check
    text = re.sub(r"<[^>]+>", " ", content)
    text = re.sub(r"\s+", " ", text).strip()
    return len(text) >= min_chars


# ---------------------------------------------------------------------------
# Basic parser — extract news articles from markdown/HTML
# ---------------------------------------------------------------------------

def parse_news_articles(content: str, source_url: str, brand_id: str) -> list[dict]:
    """
    Heuristic extraction of news article links and titles from page content.

    Handles:
    - Markdown link syntax: [Title](URL)
    - HTML anchor tags: <a href="...">Title</a>
    - Press release patterns

    Returns list of dicts with keys: title, article_url, published_date (maybe)
    """
    articles = []
    seen_urls = set()

    # Pattern 1: Markdown links  [Title](URL)
    md_pattern = re.compile(r"\[([^\]]{10,200})\]\((https?://[^\s)]+)\)")
    for m in md_pattern.finditer(content):
        title, url = m.group(1).strip(), m.group(2).strip()
        if url in seen_urls:
            continue
        if _looks_like_article_url(url, source_url) or _looks_like_news_title(title):
            seen_urls.add(url)
            date = _extract_date_from_nearby_text(content, m.start())
            articles.append({
                "title": title,
                "article_url": url,
                "published_date": date,
                "summary": None,
            })

    # Pattern 2: HTML <a href> anchors
    html_pattern = re.compile(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*([^<]{10,200})\s*</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for m in html_pattern.finditer(content):
        raw_url, title = m.group(1).strip(), re.sub(r"\s+", " ", m.group(2)).strip()
        url = _resolve_url(raw_url, source_url)
        if not url or url in seen_urls:
            continue
        if _looks_like_article_url(url, source_url) or _looks_like_news_title(title):
            seen_urls.add(url)
            date = _extract_date_from_nearby_text(content, m.start())
            articles.append({
                "title": title,
                "article_url": url,
                "published_date": date,
                "summary": None,
            })

    logger.debug(f"[Parser] {brand_id} newsroom: found {len(articles)} article candidates")
    return articles[:100]  # cap at 100 per page


def parse_products(content: str, source_url: str, brand_id: str) -> list[dict]:
    """
    Heuristic extraction of product names and URLs from page content.

    Returns list of dicts with keys: product_name, product_url, category, description
    """
    products = []
    seen_urls = set()

    # Pattern 1: Markdown links
    md_pattern = re.compile(r"\[([^\]]{3,150})\]\((https?://[^\s)]+)\)")
    for m in md_pattern.finditer(content):
        title, url = m.group(1).strip(), m.group(2).strip()
        if url in seen_urls:
            continue
        if _looks_like_product_url(url, source_url) or _looks_like_product_title(title):
            seen_urls.add(url)
            products.append({
                "product_name": title,
                "product_url": url,
                "category": _guess_category(title, url),
                "description": None,
                "model_number": _extract_model_number(title),
            })

    # Pattern 2: HTML anchors
    html_pattern = re.compile(
        r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>\s*([^<]{3,150})\s*</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for m in html_pattern.finditer(content):
        raw_url, title = m.group(1).strip(), re.sub(r"\s+", " ", m.group(2)).strip()
        url = _resolve_url(raw_url, source_url)
        if not url or url in seen_urls:
            continue
        if _looks_like_product_url(url, source_url) and len(title) > 3:
            seen_urls.add(url)
            products.append({
                "product_name": title,
                "product_url": url,
                "category": _guess_category(title, url),
                "description": None,
                "model_number": _extract_model_number(title),
            })

    logger.debug(f"[Parser] {brand_id} products: found {len(products)} product candidates")
    return products[:200]


# ---------------------------------------------------------------------------
# Parser helpers
# ---------------------------------------------------------------------------

def _resolve_url(raw_url: str, base_url: str) -> Optional[str]:
    """Resolve relative URL against base, return absolute or None."""
    if not raw_url or raw_url.startswith(("#", "javascript:", "mailto:")):
        return None
    if raw_url.startswith("http"):
        return raw_url
    return urljoin(base_url, raw_url)


def _looks_like_article_url(url: str, source_url: str) -> bool:
    """Heuristic: does this URL look like a news article?"""
    path = urlparse(url).path.lower()
    news_segments = ["/news", "/press", "/release", "/blog", "/article",
                     "/announcement", "/newsroom", "/media"]
    return any(seg in path for seg in news_segments)


def _looks_like_news_title(title: str) -> bool:
    """Heuristic: does this title sound like a news headline?"""
    title_lower = title.lower()
    news_keywords = ["launches", "announces", "releases", "unveils", "introduces",
                     "partnership", "award", "wins", "expands", "new", "product"]
    return any(kw in title_lower for kw in news_keywords) and len(title) > 20


def _looks_like_product_url(url: str, source_url: str) -> bool:
    """Heuristic: does this URL look like a product page?"""
    # Same domain check
    base_host = urlparse(source_url).netloc
    url_host = urlparse(url).netloc
    if base_host and url_host and base_host != url_host:
        return False
    path = urlparse(url).path.lower()
    product_segments = ["/product", "/mobile-computer", "/handheld", "/scanner",
                        "/tablet", "/wearable", "/device", "/hardware"]
    return any(seg in path for seg in product_segments)


def _looks_like_product_title(title: str) -> bool:
    """Heuristic: does this title look like a product name?"""
    # Model numbers often contain letters+digits
    has_model = bool(re.search(r"[A-Z]{1,5}\d{2,}", title))
    product_keywords = ["computer", "scanner", "tablet", "handheld", "rugged",
                        "wearable", "reader", "terminal", "mobile"]
    title_lower = title.lower()
    return has_model or any(kw in title_lower for kw in product_keywords)


def _guess_category(title: str, url: str) -> str:
    """Rough product category from title/URL."""
    combined = (title + " " + url).lower()
    if any(k in combined for k in ["scanner", "barcode", "ring"]):
        return "barcode_scanner"
    if any(k in combined for k in ["tablet", "rugged-tablet"]):
        return "rugged_tablet"
    if any(k in combined for k in ["mobile", "handheld", "computer", "pda"]):
        return "mobile_computer"
    if any(k in combined for k in ["wearable", "glove", "arm"]):
        return "wearable"
    if any(k in combined for k in ["vehicle", "forklift", "mount"]):
        return "vehicle_mount"
    return "other"


def _extract_model_number(title: str) -> Optional[str]:
    """Extract model number pattern (e.g. TC52, ET40, MC9400) from a title."""
    m = re.search(r"\b([A-Z]{1,4}\d{2,4}[A-Z0-9]{0,4})\b", title)
    return m.group(1) if m else None


def _extract_date_from_nearby_text(content: str, pos: int, window: int = 300) -> Optional[str]:
    """Look for ISO or common date formats in the text near position pos."""
    snippet = content[max(0, pos - window): pos + window]
    # ISO date
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", snippet)
    if m:
        return m.group(1)
    # US date: March 14, 2026
    m = re.search(
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
        r"\s+\d{1,2},?\s+(\d{4})\b",
        snippet,
        re.IGNORECASE,
    )
    if m:
        return m.group(0)
    return None


# ---------------------------------------------------------------------------
# Fetcher strategy selector (SpiderCloud or direct httpx)
# ---------------------------------------------------------------------------

def _get_spider_proxy() -> Optional[SpiderCloudProxy]:
    """Return a SpiderCloudProxy if API key is configured, else None."""
    try:
        settings = get_settings()
        api_key = settings.data_source.spidercloud.api_key
        if api_key:
            return SpiderCloudProxy(
                api_key=api_key,
                stealth=True,
                proxy_enabled=True,
                render_js=True,
                timeout=60.0,
                max_retries=2,
            )
    except Exception as e:
        logger.warning(f"[BrandCrawler] Could not init SpiderCloudProxy: {e}")
    return None


def _direct_fetch(url: str, timeout: float = 30.0) -> Optional[str]:
    """Plain httpx GET fallback for non-JS sites."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        logger.warning(f"[BrandCrawler] Direct fetch failed for {url}: {e}")
        return None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class BrandCrawlerPipeline:
    """
    Orchestrates crawling of all brand newsroom + products pages,
    writing results to the Data Lake.
    """

    # Delay between URL fetches (be polite to spider.cloud rate limits)
    INTER_URL_DELAY_SEC = 3.0
    INTER_BRAND_DELAY_SEC = 5.0

    def __init__(self, lake: DataLake, proxy: Optional[SpiderCloudProxy] = None):
        self.lake = lake
        self._proxy = proxy or _get_spider_proxy()
        if not self._proxy:
            logger.warning(
                "[BrandCrawler] No SpiderCloud API key — falling back to direct HTTP. "
                "JS-heavy sites will likely fail or return incomplete content."
            )

    def run_all(self) -> PipelineRun:
        """Crawl all 10 brands. Returns a PipelineRun summary."""
        run = PipelineRun(run_id=str(uuid.uuid4()))
        run.brands_attempted = len(BRAND_SOURCES)

        for i, brand in enumerate(BRAND_SOURCES):
            logger.info(
                f"[BrandCrawler] [{i+1}/{len(BRAND_SOURCES)}] "
                f"Starting brand: {brand.display_name}"
            )
            results = self._crawl_brand(brand, run.run_id)
            run.results.extend(results)
            run.urls_attempted += len(results)
            run.urls_success += sum(1 for r in results if r.status == "success")
            run.urls_error += sum(1 for r in results if r.status != "success")
            run.articles_total += sum(r.articles_found for r in results)
            run.products_total += sum(r.products_found for r in results)

            if i < len(BRAND_SOURCES) - 1:
                time.sleep(self.INTER_BRAND_DELAY_SEC)

        logger.info(
            f"[BrandCrawler] Pipeline complete. "
            f"brands={run.brands_attempted} "
            f"urls={run.urls_attempted} "
            f"success={run.urls_success} "
            f"articles={run.articles_total} "
            f"products={run.products_total}"
        )
        return run

    def run_brand(self, brand_id: str) -> PipelineRun:
        """Crawl a single brand by brand_id."""
        brand = get_brand_by_id(brand_id)
        if not brand:
            raise ValueError(f"Unknown brand_id: {brand_id!r}")

        run = PipelineRun(run_id=str(uuid.uuid4()))
        run.brands_attempted = 1
        results = self._crawl_brand(brand, run.run_id)
        run.results.extend(results)
        run.urls_attempted = len(results)
        run.urls_success = sum(1 for r in results if r.status == "success")
        run.urls_error = sum(1 for r in results if r.status != "success")
        run.articles_total = sum(r.articles_found for r in results)
        run.products_total = sum(r.products_found for r in results)
        return run

    def _crawl_brand(self, brand: BrandSource, run_id: str) -> list[CrawlResult]:
        """Crawl all URLs for a single brand."""
        results = []
        urls_with_type = (
            [(u, "newsroom") for u in brand.newsroom_urls] +
            [(u, "products") for u in brand.products_urls]
        )

        for j, (url, url_type) in enumerate(urls_with_type):
            result = self._crawl_url(brand, url, url_type, run_id)
            results.append(result)
            if j < len(urls_with_type) - 1:
                time.sleep(self.INTER_URL_DELAY_SEC)

        return results

    def _crawl_url(
        self,
        brand: BrandSource,
        url: str,
        url_type: str,
        run_id: str,
    ) -> CrawlResult:
        """Fetch a single URL and persist results."""
        logger.info(f"[BrandCrawler] Fetching {brand.brand_id}/{url_type}: {url}")

        # Record start in DB
        self.lake.start_run(run_id, brand.brand_id, url, url_type)
        t_start = time.monotonic()

        # --- Fetch ---
        content: Optional[str] = None
        fetch_error: Optional[str] = None

        try:
            if brand.crawl_type == "direct" or not self._proxy:
                content = _direct_fetch(url)
            else:
                # Use SpiderCloud with markdown output for cleaner parsing
                content = self._proxy.fetch_html(url, return_format="markdown")

        except Exception as exc:
            fetch_error = str(exc)
            logger.error(f"[BrandCrawler] Fetch exception for {url}: {exc}")

        duration_ms = int((time.monotonic() - t_start) * 1000)

        # --- Validate ---
        if fetch_error or not content:
            status = "error"
            self.lake.finish_run(
                run_id, url, status=status, duration_ms=duration_ms,
                error_msg=fetch_error or "empty response",
            )
            return CrawlResult(
                brand_id=brand.brand_id, url=url, url_type=url_type,
                status=status, error_msg=fetch_error, duration_ms=duration_ms,
            )

        if _is_blocked(content):
            logger.warning(f"[BrandCrawler] Blocked response for {url}")
            self.lake.finish_run(
                run_id, url, status="blocked", duration_ms=duration_ms,
                content_length=len(content), error_msg="anti-bot block detected",
            )
            return CrawlResult(
                brand_id=brand.brand_id, url=url, url_type=url_type,
                status="blocked", duration_ms=duration_ms,
                error_msg="anti-bot block detected",
            )

        if not _has_meaningful_content(content):
            logger.warning(f"[BrandCrawler] Insufficient content for {url}")
            self.lake.finish_run(
                run_id, url, status="empty", duration_ms=duration_ms,
                content_length=len(content),
            )
            return CrawlResult(
                brand_id=brand.brand_id, url=url, url_type=url_type,
                status="empty", duration_ms=duration_ms,
            )

        # --- Persist raw ---
        raw_page_id = self.lake.save_raw_page(
            run_id=run_id,
            brand_id=brand.brand_id,
            url=url,
            url_type=url_type,
            content=content,
            content_format="markdown" if brand.crawl_type != "direct" else "html",
            write_file=True,
        )

        self.lake.finish_run(
            run_id, url, status="success",
            duration_ms=duration_ms, content_length=len(content),
        )

        # --- Parse & persist structured data ---
        articles_count = 0
        products_count = 0

        if url_type == "newsroom":
            articles = parse_news_articles(content, url, brand.brand_id)
            for art in articles:
                try:
                    self.lake.upsert_article(
                        raw_page_id=raw_page_id,
                        brand_id=brand.brand_id,
                        title=art["title"],
                        source_url=url,
                        article_url=art.get("article_url"),
                        published_date=art.get("published_date"),
                        summary=art.get("summary"),
                    )
                    articles_count += 1
                except Exception as e:
                    logger.debug(f"[BrandCrawler] Article upsert error: {e}")

        elif url_type == "products":
            prods = parse_products(content, url, brand.brand_id)
            for prod in prods:
                try:
                    self.lake.upsert_product(
                        raw_page_id=raw_page_id,
                        brand_id=brand.brand_id,
                        product_name=prod["product_name"],
                        source_url=url,
                        product_url=prod.get("product_url"),
                        model_number=prod.get("model_number"),
                        category=prod.get("category"),
                        description=prod.get("description"),
                    )
                    products_count += 1
                except Exception as e:
                    logger.debug(f"[BrandCrawler] Product upsert error: {e}")

        logger.info(
            f"[BrandCrawler] Done {brand.brand_id}/{url_type}: "
            f"articles={articles_count} products={products_count} "
            f"({duration_ms}ms)"
        )

        return CrawlResult(
            brand_id=brand.brand_id,
            url=url,
            url_type=url_type,
            status="success",
            content=None,  # don't carry large content in memory
            duration_ms=duration_ms,
            articles_found=articles_count,
            products_found=products_count,
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="V2 Brand Intelligence Crawler",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.v2.brand_crawler --all
  python -m src.v2.brand_crawler --brand zebra
  python -m src.v2.brand_crawler --brand honeywell --brand datalogic
        """,
    )
    parser.add_argument("--all", action="store_true", help="Crawl all 10 brands")
    parser.add_argument("--brand", action="append", dest="brands",
                        metavar="BRAND_ID", help="Brand ID(s) to crawl (repeatable)")
    parser.add_argument("--db", default="data/v2/intelligence.db",
                        help="SQLite DB path (default: data/v2/intelligence.db)")
    args = parser.parse_args()

    if not args.all and not args.brands:
        parser.error("Specify --all or at least one --brand BRAND_ID")

    with DataLake(db_path=args.db) as lake:
        lake.upsert_brand_sources()
        pipeline = BrandCrawlerPipeline(lake)

        if args.all:
            run = pipeline.run_all()
        else:
            # Merge results from multiple single-brand runs
            run = PipelineRun(run_id=str(uuid.uuid4()))
            for brand_id in args.brands:
                sub = pipeline.run_brand(brand_id)
                run.results.extend(sub.results)
                run.urls_attempted += sub.urls_attempted
                run.urls_success += sub.urls_success
                run.urls_error += sub.urls_error
                run.articles_total += sub.articles_total
                run.products_total += sub.products_total

        # Print summary
        print("\n=== Crawl Summary ===")
        print(f"Run ID      : {run.run_id}")
        print(f"URLs tried  : {run.urls_attempted}")
        print(f"Success     : {run.urls_success}")
        print(f"Errors      : {run.urls_error}")
        print(f"Articles    : {run.articles_total}")
        print(f"Products    : {run.products_total}")
        print()

        print("DB Stats:")
        for table, count in lake.summary().items():
            print(f"  {table:<20}: {count}")


if __name__ == "__main__":
    main()
