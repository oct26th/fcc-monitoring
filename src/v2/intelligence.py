"""
V2 競品情報網 — AI Intelligence Extraction Engine
==================================================
明日香 (Asuka / Flash) 的行銷視角 LLM Prompt 模板。
從爬蟲抓回的原始 HTML/Markdown 中，提煉出：
  - 主打賣點 (Key Selling Points)
  - 硬體規格摘要 (Hardware Specs)
  - 上市狀態 / 發表日期 (Launch Status)
  - 競品提及 (Competitor Mentions)

並將解析結果寫入 Data Lake 的 `intelligence` 表。

Usage:
    from src.v2.intelligence import IntelligenceExtractor
    from src.v2.data_lake import DataLake

    with DataLake() as lake:
        extractor = IntelligenceExtractor()
        # Extract from a single product
        result = extractor.extract_product_intel(content, brand_id="zebra", product_id=42)
        extractor.save_to_lake(result, lake)

        # Batch: process all unprocessed raw_pages in the lake
        count = extractor.run_batch(lake)

CLI:
    python -m src.v2.intelligence --all
    python -m src.v2.intelligence --brand zebra
    python -m src.v2.intelligence --source-id 17 --source-type product
"""

from __future__ import annotations

import argparse
import json
import os
import re
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from loguru import logger

from .data_lake import DataLake


# ---------------------------------------------------------------------------
# Model config
# ---------------------------------------------------------------------------

# Supported providers: "openai" | "anthropic" | "openrouter"
# Priority: check env vars, fall back to lightweight default
_DEFAULT_MODEL  = "google/gemini-flash-1.5"   # Asuka's model (fast + cheap)
_DEFAULT_PROVIDER = "openrouter"


# ---------------------------------------------------------------------------
# Asuka's Prompt Templates
# ---------------------------------------------------------------------------

ASUKA_SYSTEM_PROMPT = textwrap.dedent("""\
    你是明日香 (Asuka)，一位敏銳的競品行銷分析師，專精於工業行動電腦與條碼掃描器領域。
    你的任務是從競品官網的原始內容中，提煉出具有行銷價值的情報。

    分析重點：
    1. 主打賣點 (Key Selling Points) — 品牌想強調的差異化優勢
    2. 硬體規格 (Hardware Specs) — CPU、螢幕、防護等級、電池、連接方式
    3. 上市狀態 (Launch Status) — 新品發表、上市日期、預購資訊
    4. 競品提及 (Competitor Mentions) — 是否直接或間接提到競爭對手

    輸出格式必須是嚴格的 JSON，不要有任何說明文字。
    若某欄位無法從內容中找到，請填入 null。
""")

ASUKA_PRODUCT_PROMPT_TEMPLATE = textwrap.dedent("""\
    以下是來自 {brand_name} 官網的產品頁面內容：

    ---
    {content}
    ---

    請從上方內容中萃取競品情報，以 JSON 格式回傳（不含 markdown code block）：

    {{
      "product_name": "產品型號或名稱（若可識別）",
      "key_selling_points": [
        "賣點1（例如：IP67 防塵防水、5000mAh 超長電池）",
        "賣點2",
        "賣點3（最多5點，由重要到次要排序）"
      ],
      "hardware_specs": {{
        "processor": "CPU 型號",
        "display": "螢幕尺寸/解析度",
        "protection_rating": "IP等級 或 MIL-STD",
        "battery": "電池容量 mAh",
        "os": "作業系統",
        "connectivity": ["Wi-Fi 6", "BT 5.x", "5G/LTE"],
        "scanner_engine": "掃描引擎（若有）",
        "other": {{}}
      }},
      "launch_status": {{
        "announced_date": "YYYY-MM-DD 或 null",
        "availability": "available | announced | coming_soon | discontinued | unknown",
        "press_release_url": "新聞稿連結（若有）"
      }},
      "competitor_mentions": ["若提到競品名稱請列出"],
      "marketing_angle": "一句話總結：這個產品想搶攻哪個市場/場景？",
      "confidence": 0.0
    }}

    confidence 分數規則：
    - 0.9+ = 有完整規格表與清晰賣點
    - 0.7-0.9 = 有部分規格，賣點可識別
    - 0.5-0.7 = 僅有部分資訊，推斷成分高
    - 0.5 以下 = 內容模糊，僅能猜測
""")

ASUKA_NEWS_PROMPT_TEMPLATE = textwrap.dedent("""\
    以下是來自 {brand_name} 官網的新聞稿 / 部落格文章內容：

    ---
    {content}
    ---

    請從上方內容中萃取競品情報，以 JSON 格式回傳（不含 markdown code block）：

    {{
      "headline_summary": "一句話總結這則新聞的核心訊息",
      "key_selling_points": [
        "文中強調的主要賣點或亮點（最多5點）"
      ],
      "products_mentioned": [
        {{
          "model": "產品型號",
          "specs_snippet": "提到的關鍵規格片段（若有）"
        }}
      ],
      "launch_status": {{
        "announced_date": "YYYY-MM-DD 或 null",
        "availability": "available | announced | coming_soon | discontinued | unknown",
        "event_name": "發表活動名稱（如 MWC 2026）"
      }},
      "competitor_mentions": ["若提到競品名稱請列出"],
      "market_insight": "行銷視角：這則新聞對我們的競爭策略有何意義？",
      "confidence": 0.0
    }}
""")


# ---------------------------------------------------------------------------
# Extraction result dataclass
# ---------------------------------------------------------------------------

@dataclass
class IntelResult:
    """Parsed intelligence output from LLM extraction."""
    source_type: str               # 'product' | 'news_article'
    source_id: int                 # FK to products.id or news_articles.id
    brand_id: str
    intel_type: str                # 'key_points' | 'spec_summary' | 'launch_event' | 'competitor_mention'
    content: str                   # human-readable extracted text
    metadata: dict = field(default_factory=dict)
    model_used: str = _DEFAULT_MODEL
    confidence: float = 0.5
    raw_llm_json: Optional[dict] = None  # full parsed JSON from LLM


# ---------------------------------------------------------------------------
# LLM client wrapper (provider-agnostic)
# ---------------------------------------------------------------------------

class _LLMClient:
    """
    Thin wrapper to call LLMs for intelligence extraction.

    Supports:
      - OpenRouter (recommended: access to Gemini Flash, Claude, etc.)
      - OpenAI-compatible APIs

    Config via env vars:
      OPENROUTER_API_KEY   → OpenRouter (preferred)
      OPENAI_API_KEY       → direct OpenAI
      INTEL_MODEL          → override model name
    """

    def __init__(self):
        self.openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
        self.openai_key     = os.environ.get("OPENAI_API_KEY", "")
        self.minimax_key    = os.environ.get("MINIMAX_API_KEY", "")
        self.model          = os.environ.get("INTEL_MODEL", _DEFAULT_MODEL)

        if self.minimax_key:
            self._base_url = "https://api.minimaxi.chat/v1"
            self._api_key  = self.minimax_key
            self.model     = os.environ.get("INTEL_MODEL", "abab6.5s-chat") # MiniMax 2.5 equivalent
            logger.info(f"[IntelLLM] Using MiniMax model: {self.model}")
        elif self.openrouter_key:
            self._base_url = "https://openrouter.ai/api/v1"
            self._api_key  = self.openrouter_key
            logger.info(f"[IntelLLM] Using OpenRouter model: {self.model}")
        elif self.openai_key:
            self._base_url = "https://api.openai.com/v1"
            self._api_key  = self.openai_key
            self.model     = os.environ.get("INTEL_MODEL", "gpt-4o-mini")
            logger.info(f"[IntelLLM] Using OpenAI model: {self.model}")
        else:
            self._base_url = None
            self._api_key  = None
            logger.warning(
                "[IntelLLM] No LLM API key found. "
                "Set OPENROUTER_API_KEY or OPENAI_API_KEY. "
                "Extraction will return mock results."
            )

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def complete(self, system_prompt: str, user_prompt: str) -> Optional[str]:
        """
        Send a chat completion request. Returns the assistant message text or None.
        """
        if not self.available:
            return None

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type":  "application/json",
        }
        if "openrouter" in self._base_url:
            headers["HTTP-Referer"] = "https://github.com/fcc-monitor"
            headers["X-Title"]      = "FCC Monitor V2 Intelligence"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            "temperature":  0.2,   # low temp for factual extraction
            "max_tokens":   2048,
        }

        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except httpx.HTTPStatusError as exc:
            logger.error(f"[IntelLLM] HTTP {exc.response.status_code}: {exc}")
        except Exception as exc:
            logger.error(f"[IntelLLM] Unexpected error: {exc}")
        return None


# ---------------------------------------------------------------------------
# Intelligence Extractor
# ---------------------------------------------------------------------------

class IntelligenceExtractor:
    """
    Asuka's AI-powered competitive intelligence extraction engine.

    Wraps LLM API calls with prompt templates designed for marketing analysts:
    - Extracts key selling points, hardware specs, and launch dates
    - Detects competitor mentions
    - Saves structured results to the Data Lake `intelligence` table

    Design principle: graceful degradation
      If no LLM key is configured, extraction returns a placeholder result
      so the pipeline never hard-fails.
    """

    # Max content chars to send to LLM (avoid token limits + cost)
    MAX_CONTENT_CHARS = 4000

    def __init__(self, llm: Optional[_LLMClient] = None):
        self._llm = llm or _LLMClient()

    # ------------------------------------------------------------------
    # Public: single-item extraction
    # ------------------------------------------------------------------

    def extract_product_intel(
        self,
        content: str,
        brand_id: str,
        brand_name: str,
        product_id: int,
    ) -> IntelResult:
        """
        Extract competitive intelligence from a product page.

        Args:
            content:     Raw page content (markdown or HTML)
            brand_id:    Brand identifier (e.g. "zebra")
            brand_name:  Human-readable brand name for prompt
            product_id:  FK to products.id in Data Lake

        Returns:
            IntelResult with structured extraction.
        """
        truncated = self._truncate_content(content)

        user_prompt = ASUKA_PRODUCT_PROMPT_TEMPLATE.format(
            brand_name=brand_name,
            content=truncated,
        )

        raw_json = self._call_llm_for_json(user_prompt)

        if raw_json is None:
            # Fallback: minimal result when LLM unavailable
            return self._make_fallback_result(
                source_type="product",
                source_id=product_id,
                brand_id=brand_id,
                content=f"[no LLM key] Product page content ({len(content)} chars)",
            )

        return self._parse_product_result(raw_json, brand_id, product_id)

    def extract_news_intel(
        self,
        content: str,
        brand_id: str,
        brand_name: str,
        article_id: int,
    ) -> IntelResult:
        """
        Extract competitive intelligence from a news article / press release.

        Args:
            content:    Raw article content (markdown or HTML)
            brand_id:   Brand identifier
            brand_name: Human-readable brand name for prompt
            article_id: FK to news_articles.id in Data Lake

        Returns:
            IntelResult with structured extraction.
        """
        truncated = self._truncate_content(content)

        user_prompt = ASUKA_NEWS_PROMPT_TEMPLATE.format(
            brand_name=brand_name,
            content=truncated,
        )

        raw_json = self._call_llm_for_json(user_prompt)

        if raw_json is None:
            return self._make_fallback_result(
                source_type="news_article",
                source_id=article_id,
                brand_id=brand_id,
                content=f"[no LLM key] News article content ({len(content)} chars)",
            )

        return self._parse_news_result(raw_json, brand_id, article_id)

    # ------------------------------------------------------------------
    # Public: save to Data Lake
    # ------------------------------------------------------------------

    def save_to_lake(self, result: IntelResult, lake: DataLake) -> int:
        """
        Persist an IntelResult to the `intelligence` table.

        Returns the new row id.
        """
        row_id = lake.save_intelligence(
            source_type=result.source_type,
            source_id=result.source_id,
            brand_id=result.brand_id,
            intel_type=result.intel_type,
            content=result.content,
            metadata=result.metadata,
            model_used=result.model_used,
            confidence=result.confidence,
        )
        logger.info(
            f"[Intelligence] Saved intel row {row_id}: "
            f"{result.brand_id}/{result.intel_type} "
            f"(confidence={result.confidence:.2f})"
        )
        return row_id

    # ------------------------------------------------------------------
    # Public: batch processing
    # ------------------------------------------------------------------

    def run_batch(
        self,
        lake: DataLake,
        brand_id: Optional[str] = None,
        max_items: int = 50,
        delay_sec: float = 1.5,
    ) -> int:
        """
        Batch-process unextracted products and news articles.

        Finds items in the Data Lake that have no intelligence rows yet,
        runs LLM extraction, and saves results.

        Args:
            lake:      Open DataLake connection
            brand_id:  If specified, only process this brand
            max_items: Cap on total items to process per run
            delay_sec: Polite delay between LLM calls

        Returns:
            Number of intelligence rows created.
        """
        processed = 0

        # --- Products ---
        products = lake.get_products(brand_id=brand_id, limit=max_items)
        for product in products:
            if processed >= max_items:
                break
            prod_id  = product["id"]
            b_id     = product["brand_id"]
            b_name   = self._get_brand_name(lake, b_id)

            # Skip if already extracted
            if self._already_extracted(lake, "product", prod_id):
                logger.debug(f"[Intelligence] Skip (already extracted): product/{prod_id}")
                continue

            # Get raw content for this product's source page
            content = self._get_content_for_product(lake, product)
            if not content:
                logger.debug(f"[Intelligence] No content for product {prod_id}, skipping")
                continue

            try:
                result = self.extract_product_intel(
                    content=content,
                    brand_id=b_id,
                    brand_name=b_name,
                    product_id=prod_id,
                )
                self.save_to_lake(result, lake)
                processed += 1
            except Exception as exc:
                logger.error(f"[Intelligence] Error processing product {prod_id}: {exc}")

            if delay_sec > 0:
                time.sleep(delay_sec)

        # --- News articles ---
        articles = lake.get_articles(brand_id=brand_id, limit=max_items)
        for article in articles:
            if processed >= max_items:
                break
            art_id = article["id"]
            b_id   = article["brand_id"]
            b_name = self._get_brand_name(lake, b_id)

            if self._already_extracted(lake, "news_article", art_id):
                logger.debug(f"[Intelligence] Skip (already extracted): news_article/{art_id}")
                continue

            content = self._get_content_for_article(lake, article)
            if not content:
                continue

            try:
                result = self.extract_news_intel(
                    content=content,
                    brand_id=b_id,
                    brand_name=b_name,
                    article_id=art_id,
                )
                self.save_to_lake(result, lake)
                processed += 1
            except Exception as exc:
                logger.error(f"[Intelligence] Error processing article {art_id}: {exc}")

            if delay_sec > 0:
                time.sleep(delay_sec)

        logger.info(f"[Intelligence] Batch complete: {processed} intelligence rows created")
        return processed

    # ------------------------------------------------------------------
    # Private: LLM helpers
    # ------------------------------------------------------------------

    def _call_llm_for_json(self, user_prompt: str) -> Optional[dict]:
        """
        Call LLM and parse the JSON response.
        Returns parsed dict or None if LLM unavailable / parse fails.
        """
        if not self._llm.available:
            return None

        raw_text = self._llm.complete(ASUKA_SYSTEM_PROMPT, user_prompt)
        if not raw_text:
            return None

        # Strip markdown code blocks if LLM wrapped output
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            # Try to extract JSON from within the text
            m = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
            logger.warning(f"[Intelligence] JSON parse failed: {exc}. Raw: {raw_text[:200]}")
            return None

    def _truncate_content(self, content: str) -> str:
        """Trim content to MAX_CONTENT_CHARS, preserving the beginning."""
        if len(content) <= self.MAX_CONTENT_CHARS:
            return content
        # Try to cut at a paragraph boundary
        cut = content[:self.MAX_CONTENT_CHARS]
        last_nl = cut.rfind("\n\n")
        if last_nl > self.MAX_CONTENT_CHARS * 0.7:
            cut = cut[:last_nl]
        return cut + "\n\n[... content truncated for analysis ...]"

    # ------------------------------------------------------------------
    # Private: result parsers
    # ------------------------------------------------------------------

    def _parse_product_result(
        self, raw: dict, brand_id: str, product_id: int
    ) -> IntelResult:
        """
        Convert raw LLM JSON (product) → IntelResult.
        Handles missing/null fields gracefully.
        """
        ksp    = raw.get("key_selling_points") or []
        specs  = raw.get("hardware_specs") or {}
        launch = raw.get("launch_status") or {}
        conf   = float(raw.get("confidence") or 0.5)

        # Primary content: bullet-point selling points (human-readable)
        if ksp:
            content = "主打賣點：\n" + "\n".join(f"• {p}" for p in ksp if p)
        else:
            content = f"[{brand_id}] 產品頁面 — 無法萃取賣點"

        # Full metadata stored as JSON
        metadata: dict[str, Any] = {
            "product_name":         raw.get("product_name"),
            "hardware_specs":       specs,
            "launch_status":        launch,
            "competitor_mentions":  raw.get("competitor_mentions") or [],
            "marketing_angle":      raw.get("marketing_angle"),
        }

        return IntelResult(
            source_type="product",
            source_id=product_id,
            brand_id=brand_id,
            intel_type="key_points",
            content=content,
            metadata=metadata,
            model_used=self._llm.model,
            confidence=conf,
            raw_llm_json=raw,
        )

    def _parse_news_result(
        self, raw: dict, brand_id: str, article_id: int
    ) -> IntelResult:
        """
        Convert raw LLM JSON (news) → IntelResult.
        """
        ksp     = raw.get("key_selling_points") or []
        launch  = raw.get("launch_status") or {}
        conf    = float(raw.get("confidence") or 0.5)

        headline = raw.get("headline_summary") or f"[{brand_id}] 新聞稿"
        if ksp:
            content = f"{headline}\n\n重點：\n" + "\n".join(f"• {p}" for p in ksp if p)
        else:
            content = headline

        metadata: dict[str, Any] = {
            "headline_summary":    headline,
            "products_mentioned":  raw.get("products_mentioned") or [],
            "launch_status":       launch,
            "competitor_mentions": raw.get("competitor_mentions") or [],
            "market_insight":      raw.get("market_insight"),
        }

        # Use 'launch_event' intel_type if there's an event name
        event = launch.get("event_name")
        intel_type = "launch_event" if event else "key_points"

        return IntelResult(
            source_type="news_article",
            source_id=article_id,
            brand_id=brand_id,
            intel_type=intel_type,
            content=content,
            metadata=metadata,
            model_used=self._llm.model,
            confidence=conf,
            raw_llm_json=raw,
        )

    # ------------------------------------------------------------------
    # Private: fallback / helpers
    # ------------------------------------------------------------------

    def _make_fallback_result(
        self, source_type: str, source_id: int, brand_id: str, content: str
    ) -> IntelResult:
        return IntelResult(
            source_type=source_type,
            source_id=source_id,
            brand_id=brand_id,
            intel_type="key_points",
            content=content,
            metadata={"fallback": True, "reason": "no_llm_key"},
            model_used="none",
            confidence=0.0,
        )

    def _already_extracted(
        self, lake: DataLake, source_type: str, source_id: int
    ) -> bool:
        """Check if intelligence already exists for this source_type/source_id."""
        try:
            row = lake.conn.execute(
                "SELECT id FROM intelligence WHERE source_type=? AND source_id=? LIMIT 1",
                (source_type, source_id),
            ).fetchone()
            return row is not None
        except Exception:
            return False

    def _get_brand_name(self, lake: DataLake, brand_id: str) -> str:
        """Look up brand display_name from Data Lake."""
        try:
            row = lake.conn.execute(
                "SELECT display_name FROM brand_sources WHERE brand_id=?", (brand_id,)
            ).fetchone()
            return row["display_name"] if row else brand_id
        except Exception:
            return brand_id

    def _get_content_for_product(
        self, lake: DataLake, product: dict
    ) -> Optional[str]:
        """Fetch raw page content that corresponds to this product's source_url."""
        source_url   = product.get("source_url") or product.get("product_url")
        raw_page_id  = product.get("raw_page_id")
        return self._get_raw_content(lake, raw_page_id, source_url)

    def _get_content_for_article(
        self, lake: DataLake, article: dict
    ) -> Optional[str]:
        """Fetch raw page content for this article."""
        source_url  = article.get("article_url") or article.get("source_url")
        raw_page_id = article.get("raw_page_id")
        return self._get_raw_content(lake, raw_page_id, source_url)

    def _get_raw_content(
        self,
        lake: DataLake,
        raw_page_id: Optional[int],
        url: Optional[str],
    ) -> Optional[str]:
        """
        Retrieve raw page content from the Data Lake.
        Tries raw_page_id first, then falls back to URL match.
        """
        try:
            if raw_page_id:
                row = lake.conn.execute(
                    "SELECT content, file_path FROM raw_pages WHERE id=?",
                    (raw_page_id,),
                ).fetchone()
                if row:
                    if row["content"]:
                        return row["content"]
                    if row["file_path"]:
                        fp = Path(row["file_path"])
                        if fp.exists():
                            return fp.read_text(encoding="utf-8", errors="replace")

            if url:
                row = lake.conn.execute(
                    "SELECT content, file_path FROM raw_pages WHERE url=? "
                    "ORDER BY crawled_at DESC LIMIT 1",
                    (url,),
                ).fetchone()
                if row:
                    if row["content"]:
                        return row["content"]
                    if row["file_path"]:
                        fp = Path(row["file_path"])
                        if fp.exists():
                            return fp.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.debug(f"[Intelligence] content lookup failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="V2 Intelligence Extraction — Asuka's Marketing AI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m src.v2.intelligence --all
  python -m src.v2.intelligence --brand zebra --max 20
  python -m src.v2.intelligence --brand honeywell --delay 2.0
        """,
    )
    parser.add_argument("--all",   action="store_true", help="Process all brands")
    parser.add_argument("--brand", action="append", dest="brands",
                        metavar="BRAND_ID", help="Brand ID(s) to process (repeatable)")
    parser.add_argument("--max",   type=int, default=50,
                        help="Max items to process per run (default: 50)")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="Delay between LLM calls in seconds (default: 1.5)")
    parser.add_argument("--db",    default="data/v2/intelligence.db",
                        help="SQLite DB path")
    args = parser.parse_args()

    if not args.all and not args.brands:
        parser.error("Specify --all or at least one --brand BRAND_ID")

    extractor = IntelligenceExtractor()

    if not extractor._llm.available:
        logger.warning(
            "⚠️  No LLM API key configured — extraction will produce placeholder results.\n"
            "   Set OPENROUTER_API_KEY or OPENAI_API_KEY environment variable."
        )

    with DataLake(db_path=args.db) as lake:
        if args.all:
            total = extractor.run_batch(
                lake, brand_id=None, max_items=args.max, delay_sec=args.delay
            )
        else:
            total = 0
            for brand_id in args.brands:
                total += extractor.run_batch(
                    lake, brand_id=brand_id,
                    max_items=args.max, delay_sec=args.delay,
                )

    print(f"\n✅ Intelligence extraction complete: {total} rows created")

    # Print DB summary
    with DataLake(db_path=args.db) as lake:
        stats = lake.summary()
        print("\nData Lake summary:")
        for table, count in stats.items():
            print(f"  {table:<22}: {count}")


if __name__ == "__main__":
    main()
