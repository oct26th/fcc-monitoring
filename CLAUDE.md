# CLAUDE.md — FCC Monitor 作戰簡報

> 此文件是給 Claude Code 的持久化記憶。每次新 Session 請先讀完這份文件。

---

## 專案目的

監控特定品牌在 FCC（美國聯邦通訊委員會）的設備認證申請（Equipment Authorization System），
第一時間偵測競品新型號動向，並下載 Label PDF 取得詳細認證資料。

**監控對象**（工業條碼機 / 行動電腦領域）：

| 公司 | Grantee Code |
|------|--------------|
| Zebra Technologies | V2X, SS4 |
| Honeywell | HD5 |
| Motorola Solutions | U4F, U4G |
| Symbol Technologies | HLE |
| 其他 | UZ7, 2AOJL, 2AC6A, 2AR9L |

---

## 當前核心任務（2026-03）

1. **FCC 搜尋列表** — 對每個 grantee_code 抓取新 FCC ID 清單
2. **Label PDF 下載** — 對每個新 FCC ID，從 FCC 網站拔下 Label PDF：
   ```
   https://apps.fcc.gov/oetcf/eas/reports/GetApplicationAttachment.html
     ?calledFromFrame=N&id=[APPLICATION_ID]&idType=APPID
   ```
3. **Telegram / Discord 通知** — 新 FCC ID 出現時立即通知
4. **C2PC 偵測** — 偵測 Class II Permissive Change（硬體暗中升級），diff 新舊規格

---

## 已知問題（重要！勿重蹈覆轍）

### ⚠️ Spider.cloud POST Bug
- **現象**：`SpiderCloudProxy.post_form()` 透過 `/scrape` API 發送 POST 請求時，
  Spider.cloud 會把 POST body（`grantee_code=UZ7` 等表單參數）吃掉，實際變成 GET，
  導致 FCC 搜尋頁回傳空白結果。
- **根本原因**：`/scrape` API 不支援正確透傳 `http_method=POST` + `body`。
- **修復方向**：改用 Spider.cloud 的 **automation_scripts** 端點，
  透過無頭瀏覽器腳本直接填表單並提交，或改用 **BrowserlessFetcher**（已實作，可用）。
- **現況**：`BrowserlessFetcher` 是目前可工作的備案（headless browser 填表單）。

---

## 現有檔案狀態

### ✅ 完成且可用
| 檔案 | 功能 |
|------|------|
| `src/models.py` | `FCCRecord` dataclass（資料模型基準） |
| `src/config.py` | 設定管理（YAML + env var 展開） |
| `src/detector.py` | 新舊 FCC ID 比對，產出 `Change` 清單 |
| `src/notifier.py` | Telegram / Discord 通知 |
| `src/circuit_breaker.py` | 爬蟲容錯（Circuit Breaker 模式） |
| `src/c2pc_detector.py` | C2PC diff 分析與告警訊息生成 |
| `src/fetcher.py` | FCC 抓取（SpiderCloud/Browserless/Playwright 策略） |

### ⚠️ 有缺陷
| 檔案 | 問題 |
|------|------|
| `src/fetcher.py` → `SpiderCloudFetcher` | POST bug（見上） |

### ❌ 缺失（需建立）
| 檔案 | 說明 |
|------|------|
| `src/database.py` | SQLite CRUD（基於 `FCCRecord`，存已知 ID） |
| `src/pdf_fetcher.py` | FCC Label PDF 下載模組 |
| `src/main.py` | 主程式入口，串接完整流程 |

### 🚫 不再需要
- `brand_db.py`、舊 `database.py` — 不要重建，已廢棄
- `brand_crawlers/` — 品牌官網爬蟲層，目前不是優先目標

---

## 架構流程（最新版）

```
main.py
  ├─ 載入設定（config.py → settings.yaml）
  ├─ 對每個 grantee_code：
  │    ├─ fetcher（BrowserlessFetcher 優先，SpiderCloud automation_scripts 次之）
  │    │    └─ 回傳 List[FCCRecord]
  │    ├─ database.py → 比對已知 ID → 找出新 FCCRecord
  │    └─ 若有新 ID：
  │         ├─ pdf_fetcher.py → 下載 Label PDF 存 data/pdfs/
  │         ├─ notifier.py → Telegram / Discord 通知
  │         └─ database.py → 儲存新 ID
  └─ （可選）scheduler.py → C2PC 掃描排程
```

---

## 資料庫設計原則

- 基於 `src/models.py` 的 `FCCRecord` dataclass
- SQLite，檔案路徑：`data/fcc_monitor.db`
- 只需輕量 CRUD：
  - `save_records(records)` — 批次儲存
  - `get_known_ids(grantee_code) -> set[str]` — 取已知 ID
  - `get_new_records(current_records) -> list[FCCRecord]` — 過濾出新的

---

## 設定檔

- **主設定**：`config/settings.yaml`
- **敏感環境變數**：
  - `SPIDERCLOUD_API_KEY` — Spider.cloud API 金鑰
  - `BROWSERLESS_API_KEY` — Browserless.io 金鑰
  - Telegram token 直接寫在 settings.yaml（開發環境）

---

## Git 分支

- 開發分支：`claude/fcc-monitor-setup-qYtN5`
- 永遠推到此分支，不推 main

---

## 開發慣例

- Python 3.11+
- `loguru` 做 logging
- `httpx` 做 HTTP client
- `pydantic` / `pydantic-settings` 做設定驗證
- 不過度工程化：能用標準庫解決的不引入新依賴
