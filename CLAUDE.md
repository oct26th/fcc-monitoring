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
# V2 Operation: New Product Intelligence (競品情報網)

## 目標 (Goal)
建立 FCC 監控系統的第二階段 (V2)，當 FCC 發現新品或官網有更新時，自動抓取並建立競品分析資料庫 (Data Lake)。

## 三點確認 (3-Point Confirmation)
1. **形式 (Format)**: 初期建立 Data Lake (暫定 SQLite + JSON 混合結構) 作為原始資料池。後續再決定前端呈現。
2. **範圍 (Scope)**: 10 大品牌的官方 Press Release (新聞稿) 與 Products (產品列表) 頁面。
3. **時限 (Deadline)**: 週一 (Monday) 完成 POC (概念驗證) 上線。

## 專家調度計畫 (Expert Delegation)
- **綾波零 (Rei / Opus) - 架構設計**: 設計 Data Lake Schema，規劃從 FCC 觸發到官網爬蟲的 Pipeline。
- **真希波 (Mari / M2.5) - 情報搜集**: 盤點 10 大品牌的 Newsroom/Products 目錄 URL 結構。
- **碇真嗣 (Shinji / Sonnet) - 程式實作**: 擴充目前的 SpiderCloudFetcher，實作定期爬蟲與 Data Lake 寫入邏輯。
- **明日香 (Asuka / Flash) - 行銷萃取**: 撰寫 LLM Prompt，負責從凌亂的網頁 HTML 中萃取出「主打賣點、硬體規格、上市日期」。

## 第一步行動 (Next Action)
請 Rei (Opus) 展開 `V2_ARCHITECTURE.md`，制定 Data Lake 結構與資料流。

## V2 Stage 2: Intelligence Extraction & Hook (2026-03-15)
**長官決策確認：**
1. **Sitemap 巡航 (Live Test)**：爬蟲引擎需整合 SpiderCloud 的 `sitemap: true` 參數，確保能遍歷產品與新聞網址，不漏接隱藏網頁。
2. **AI 萃取引擎 (LLM Intelligence)**：實作從 raw HTML/text 中提煉「主打賣點、硬體規格、上市狀態」的 Prompt 模板 (Asuka 行銷視角)，並將解析結果寫入 Data Lake。
3. **V1 連動 V2 (Trigger Hook)**：修改 `src/main.py`，當 FCC 掃描到新品入庫時，自動觸發該品牌對應的 V2 官網巡邏腳本。

## V2.1 Architecture Update: Physical Decoupling (2026-03-16)
**長官戰術指示（物理切割）：**
- **Data Crawler (Python Script)**: 降級為純粹的「無腦搬磚工」。只負責透過 SpiderCloud 抓取網頁、存入 SQLite Data Lake，**不再於腳本內處理** LLM 分析，徹底拔除對外部 LLM API Key 的依賴與資安風險。
- **Intelligence Analyzer (OpenClaw Agents)**: 真正的「大腦」留在 NERV 總部。由系統內建的 Agent (如 Asuka/Misato) 直接讀取 SQLite 內的資料，使用總部內建的算力 (MiniMax 2.5 / Gemini) 進行情報分析。
- **戰術優勢**: 節省 API 成本、避免金鑰外洩，且長官能隨時在頻道用自然語言「動態調度」Agent 進行多維度比較 (例如：隨時點名 Asuka 針對特定兩款機型做弱點打擊分析)。
