# V2_ARCHITECTURE.md — 競品情報網 Data Lake Design

> Authored by Shinji (Sonnet) as implementation record.  
> See `CLAUDE.md` V2_MISSION section for mission context.

---

## Overview

V2 extends FCC Monitor into a **competitive intelligence platform**. When FCC detects new product filings (V1), V2 automatically crawls the brand's official Newsroom and Products pages to gather marketing context, specifications, and launch timelines.

---

## Data Lake Structure

### Storage Layers

```
SQLite (data/v2/intelligence.db)
  └─ Structured metadata: brands, crawl history, parsed articles/products
  
JSON files (data/v2/raw/{brand_id}/{date}/{type}_{hash}.json)
  └─ Raw page backups: full content for LLM re-processing
```

### SQLite Tables

| Table          | Purpose                                        |
|----------------|------------------------------------------------|
| `brand_sources`| Registry of 10 brands + their URLs (seed data) |
| `crawl_runs`   | Audit log per URL crawl (status, timing, errors)|
| `raw_pages`    | Raw scraped content (markdown/HTML) + disk path |
| `news_articles`| Parsed news items (title, date, summary, URL)   |
| `products`     | Parsed products (name, SKU, category, specs)    |
| `intelligence` | LLM-extracted insights (future Asuka/Flash task)|

---

## Pipeline Flow

```
BrandCrawlerPipeline.run_all()
  │
  ├─ For each brand in BRAND_SOURCES (10 brands):
  │    ├─ For each newsroom_url:
  │    │    ├─ Fetch via SpiderCloudProxy (js_required=True) or direct httpx
  │    │    ├─ Validate: not blocked, has content
  │    │    ├─ DataLake.save_raw_page()  → raw_pages + disk backup
  │    │    └─ parse_news_articles()    → DataLake.upsert_article()
  │    │
  │    └─ For each products_url:
  │         ├─ Fetch via SpiderCloudProxy or direct httpx
  │         ├─ Validate: not blocked, has content
  │         ├─ DataLake.save_raw_page()  → raw_pages + disk backup
  │         └─ parse_products()         → DataLake.upsert_product()
  │
  └─ Return PipelineRun summary (counts, errors)
```

---

## 10 Brand Sources

| Brand            | brand_id      | Grantee | Crawl Type | Notes                    |
|------------------|---------------|---------|------------|--------------------------|
| Zebra            | zebra         | UZ7     | spider     | JS SPA                   |
| Honeywell SPS    | honeywell     | HD5     | spider     | SPS subdomain            |
| Datalogic        | datalogic     | U4F,U4G | spider    | Standard JS              |
| Point Mobile     | point_mobile  | V2X     | spider     | Korean manufacturer      |
| Bluebird         | bluebird      | SS4     | direct     | Older ASP stack          |
| Unitech          | unitech       | HLE     | spider     | US + global domains      |
| ProGlove         | proglove      | 2AOJL   | spider     | German startup           |
| Chainway         | chainway      | 2AC6A   | spider     | Chinese manufacturer     |
| Fujian Newland   | newland       | 2AR9L   | spider     | AIDC division            |
| Generalscan      | generalscan   | 2ADNT   | direct     | Simpler site             |

---

## File Layout

```
src/v2/
  __init__.py
  brand_sources.py   — Brand registry (10 brands, URLs, grantee codes)
  schema.py          — SQLite DDL (6 tables + indexes)
  data_lake.py       — CRUD layer (upsert, query, summary)
  brand_crawler.py   — Pipeline: fetch → validate → parse → persist

data/v2/
  intelligence.db    — SQLite Data Lake
  raw/
    {brand_id}/
      {YYYY-MM-DD}/
        newsroom_{hash}.json
        products_{hash}.json
```

---

## Usage

```bash
# Crawl all brands
python -m src.v2.brand_crawler --all

# Crawl a single brand
python -m src.v2.brand_crawler --brand zebra

# Multiple brands
python -m src.v2.brand_crawler --brand honeywell --brand datalogic

# Custom DB path
python -m src.v2.brand_crawler --all --db data/v2/custom.db
```

---

## Future: LLM Intelligence Extraction (Asuka/Flash Task)

The `intelligence` table is ready to receive LLM-extracted insights. Planned prompt:

> "From this product page, extract:
> 1. Key marketing claims / differentiators
> 2. Hardware specs (CPU, RAM, battery, OS, certifications)
> 3. Launch date or 'coming soon' signals
> 4. Competitive mentions"

This will be implemented as a separate pipeline step using `DataLake.save_intelligence()`.

---

## Integration with V1 FCC Monitor

When V1 detects a **new FCC ID** for a known grantee code, V2 can be triggered:

```python
# In src/main.py, after new FCC IDs detected:
from src.v2.brand_sources import get_brand_by_grantee_code
from src.v2.brand_crawler import BrandCrawlerPipeline
from src.v2.data_lake import DataLake

brand = get_brand_by_grantee_code(fcc_record.grantee_code)
if brand:
    with DataLake() as lake:
        pipeline = BrandCrawlerPipeline(lake)
        pipeline.run_brand(brand.brand_id)
```

---

*Last updated: 2026-03-14*
