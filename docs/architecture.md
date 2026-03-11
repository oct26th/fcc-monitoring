# FCC 認證監控系統 — 技術架構（v2.0）

> 更新日期：2026-03
> 上一版：v1.0（2026-02-24）已廢棄

---

## 1. 專案目的

監控特定品牌在 FCC（美國聯邦通訊委員會）Equipment Authorization System 的新申請，
第一時間偵測競品新型號動向，並自動下載 Label PDF 取得詳細認證資料。

**監控對象**（工業條碼機 / 行動電腦）：

| 公司 | Grantee Code |
|------|--------------|
| Zebra Technologies | V2X, SS4 |
| Honeywell | HD5 |
| Motorola Solutions | U4F, U4G |
| Symbol Technologies | HLE |
| 其他 | UZ7, 2AOJL, 2AC6A, 2AR9L |

---

## 2. 系統架構

### 2.1 整體流程

```
main.py
  │
  ├─ config.py           載入 settings.yaml + 環境變數
  │
  ├─ fetcher.py          抓取 FCC EAS 搜尋頁（返回 List[FCCRecord]）
  │    ├─ SpiderCloudScriptFetcher  ← 主路徑（automation_scripts）
  │    ├─ BrowserlessFetcher        ← 備案（headless browser）
  │    └─ PlaywrightFetcher         ← 本地備案
  │
  ├─ database.py         SQLite CRUD（判斷新舊 ID）
  │
  ├─ pdf_fetcher.py      對每個新 FCC ID 下載 Label PDF
  │    └─ data/pdfs/{FCC_ID}/*.pdf
  │
  └─ notifier.py         Telegram / Discord 通知
```

### 2.2 FCC 資料流

```
FCC EAS 搜尋頁
  https://apps.fcc.gov/oetcf/eas/reports/GenericSearch.cfm
  → POST grantee_code=V2X → 結果列表 → parse → List[FCCRecord]

FCC Exhibit 列表頁（per FCC ID）
  https://apps.fcc.gov/oetcf/eas/reports/ViewExhibitReport.cfm
  ?application_id=XXXXX&ID_Code=V2XFCC123
  → 找出 Label 類型附件 → attachment_id

FCC Label PDF 下載
  https://apps.fcc.gov/oetcf/eas/reports/GetApplicationAttachment.html
  ?calledFromFrame=N&id={attachment_id}&idType=APPID
  → 儲存至 data/pdfs/{FCC_ID}/Label_{id}.pdf
```

---

## 3. 模組說明

### 3.1 `src/models.py` — 資料模型

核心資料結構 `FCCRecord`（dataclass）：

```python
@dataclass
class FCCRecord:
    fcc_id: str
    grantee_code: str
    product_code: str
    applicant_name: str
    product_description: str
    grant_date: str
    filing_date: str
    application_type: str
    status: str = "Granted"
    expires_on: Optional[str] = None
```

### 3.2 `src/fetcher.py` — FCC 資料抓取

| 類別 | 路徑 | 狀態 |
|------|------|------|
| `SpiderCloudScriptFetcher` | spider.cloud automation_scripts + Chromium | ✅ 主要 |
| `BrowserlessFetcher` | Browserless.io /function endpoint | ✅ 備案 |
| `SpiderCloudFetcher` | spider.cloud /scrape POST | ⚠️ 已知 Bug（POST body 被吃掉） |
| `PlaywrightFetcher` | 本地 Playwright | 🔧 未完整實作 |

**⚠️ POST Bug 說明**：spider.cloud 的 `/scrape` API 不正確支援 `http_method=POST`，
會把 POST body（`grantee_code=UZ7`）丟失，導致 FCC 搜尋頁回傳空白。
修復方案：改用 `automation_scripts`（雲端 Chromium 腳本填表單）。

### 3.3 `src/database.py` — SQLite 持久化

- 基於 `FCCRecord` 設計的輕量 CRUD
- 主要方法：
  - `filter_new(records)` — 過濾出尚未入庫的記錄
  - `save_records(records)` — 批次儲存
  - `get_known_ids(grantee_code)` — 取得已知 ID 集合
- 檔案路徑：`data/fcc_monitor.db`

### 3.4 `src/pdf_fetcher.py` — Label PDF 下載

兩步驟流程：
1. 從搜尋結果頁解析出 `application_id`
2. 爬 Exhibit 列表頁，下載所有描述含 "label" 的 PDF 附件

存儲位置：`data/pdfs/{FCC_ID}/{description}_{att_id}.pdf`

### 3.5 `src/notifier.py` — 通知

- Telegram Bot（HTML 格式）
- Discord Webhook
- 訊息以繁體中文為主，有品牌分組和表情符號標示

### 3.6 `src/detector.py` — 變更偵測（進階）

比較兩次抓取結果，產出 `Change` 清單（NEW / REMOVED / STATUS_CHANGED）。
目前主流程直接用 `database.py` 做 ID 比對，`detector.py` 為進階用途保留。

### 3.7 `src/c2pc_detector.py` — C2PC 偵測

偵測 FCC 申請類型為「Class II Permissive Change」（硬體悄悄升級）的案件，
比對新舊規格並產生 diff 報告。

---

## 4. 設定管理

### 4.1 主設定檔 `config/settings.yaml`

```yaml
target_grantees:
  - name: "Zebra Technologies"
    codes: ["V2X", "SS4"]

data_source:
  spidercloud:
    enabled: true
    api_key: "${SPIDERCLOUD_API_KEY}"

telegram:
  bot_token: "..."
  chat_id: "..."
```

### 4.2 環境變數

| 變數 | 用途 |
|------|------|
| `SPIDERCLOUD_API_KEY` | spider.cloud API 金鑰 |
| `BROWSERLESS_API_KEY` | Browserless.io 金鑰 |

---

## 5. 執行方式

```bash
# 單次掃描
python -m src.main

# 測試模式（不寫 DB，不發通知）
python -m src.main --dry-run

# 指定抓取策略
python -m src.main --strategy browserless

# Daemon 模式（每 24 小時一次）
python -m src.main --daemon --interval-hours 24
```

---

## 6. 目錄結構

```
fcc-monitoring/
├── src/
│   ├── __init__.py
│   ├── models.py          # 資料模型（FCCRecord）
│   ├── config.py          # 設定管理
│   ├── fetcher.py         # FCC 資料抓取（多策略）
│   ├── database.py        # SQLite 持久化
│   ├── pdf_fetcher.py     # Label PDF 下載
│   ├── notifier.py        # Telegram / Discord 通知
│   ├── detector.py        # 變更偵測（進階）
│   ├── scheduler.py       # 雙軌掃描排程（進階）
│   ├── c2pc_detector.py   # C2PC 偵測
│   ├── circuit_breaker.py # 爬蟲容錯
│   └── main.py            # 主入口
├── config/
│   └── settings.yaml
├── data/
│   ├── fcc_monitor.db     # SQLite 資料庫
│   └── pdfs/              # 下載的 Label PDF
│       └── {FCC_ID}/
├── logs/
│   └── fcc_monitor.log
├── docs/
│   └── architecture.md    # 本文件
├── CLAUDE.md              # AI 作戰簡報（Claude Code 自動讀取）
└── requirements.txt
```

---

## 7. 已知限制與待解問題

| 項目 | 說明 |
|------|------|
| Spider.cloud POST bug | `/scrape` API 不支援表單 POST，已改用 `automation_scripts` |
| PDF `application_id` 解析 | 依賴 FCC 搜尋結果頁的連結格式，若 FCC 改版需更新 regex |
| `scheduler.py` 依賴缺失 | 仍 import `brand_db` / `brand_crawlers`（已廢棄層），執行前需移除或重寫 |
| Browserless 區域 | 預設 `sfo`，可透過 `BROWSERLESS_REGION` 調整 |

---

*文檔版本: 2.0 — 2026-03*
