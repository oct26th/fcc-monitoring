# FCC 認證監控系統 - 技術架構設計

## 1. SpiderCloud API 評估

### 1.1 FCC 公開數據獲取方式

FCC 設備授權（Equipment Authorization）數據可透過以下途徑取得：

| 來源 | 類型 | 適用場景 |
|------|------|----------|
| FCC EAS 數據下載 | CSV/JSON bulk download | 每日全量同步 |
| FCC OET API | REST API | 實時查詢特定 Grantee |
| SpiderCloud 第三方服務 | API Wrapper | 已整合的商業服務 |

### 1.2 SpiderCloud 評估

**SpiderCloud** 通常提供：
- 已包裝的 FCC/監管數據 API
- 預先處理的搜尋與過濾功能
- 變更追蹤與通知 Webhook

**建議做法**：
1. 若已購買 SpiderCloud 服務，使用其 API 查詢 Grantee Code（如 Zebra = A3L, SKY）
2. 若無，使用 FCC 官方的公開數據端點
3. 本設計假設使用 **FCC EAS Bulk Download** + 自建監控邏輯

### 1.3 監控對象 Grantee Code

| 公司 | Grantee Code | 備註 |
|------|--------------|------|
| Zebra Technologies | A3L, SKY | |
| Honeywell | HD5, NORAND | 多個代碼 |
| Datalogic | RC4, U1A | |
| Unitech | MXN, NXP | |

---

## 2. 資料流程設計

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  SpiderCloud    │    │  Data Pipeline   │    │  Notification   │
│  API / FCC EAS  │───▶│  (Python)        │───▶│  (Telegram Bot) │
└─────────────────┘    └──────────────────┘    └─────────────────┘
        │                      │                        │
        ▼                      ▼                        ▼
  取得認證數據           過濾 → 差異比對 → 儲存      發送變更通知
```

### 2.1 資料流程步驟

1. **數據獲取** (每日)
   - 從 SpiderCloud API 或 FCC EAS 下載最新認證數據
   - 按 Grantee Code 過濾目標公司

2. **數據處理**
   - 解析 FCC ID、產品名稱、認證日期、證書狀態
   - 去除無效/過期認證

3. **變更偵測**
   - 與上次同步的本地數據比對
   - 識別新增、移除、狀態變更

4. **通知觸發**
   - 偵測到變更 → 發送 Telegram 訊息
   - 包含 FCC ID、產品資訊、變更類型

---

## 3. 實作規劃

### 3.1 技術選型

| 層面 | 選擇 | 理由 |
|------|------|------|
| **語言** | Python 3.10+ | 豐富的 HTTP/JSON 庫，生態成熟 |
| **排程** | APScheduler / systemd timer | 輕量、定時精確 |
| **HTTP Client** | requests + httpx | 簡單易用 |
| **資料庫** | SQLite (本地) | 無需額外服務，適合單機 |
| **通知** | python-telegram-bot | 官方 API 封裝 |
| **日誌** | structlog + loguru | 可讀性高 |

### 3.2 專案結構

```
fcc-monitor/
├── src/
│   ├── __init__.py
│   ├── fetcher.py        # FCC/SpiderCloud API 獲取
│   ├── parser.py         # 數據解析
│   ├── detector.py       # 變更偵測
│   ├── notifier.py       # Telegram 通知
│   ├── scheduler.py      # 排程主程式
│   └── config.py         # 配置管理
├── config/
│   └── settings.yaml     # 配置文件
├── data/
│   └── fcc_monitor.db    # SQLite 數據庫
├── docs/
│   └── architecture.md   # 本文件
├── tests/
├── requirements.txt
└── main.py               # 入口點
```

### 3.3 核心腳本說明

| 腳本 | 功能 |
|------|------|
| `fetcher.py` | 調用 SpiderCloud/FCC API 獲取認證數據 |
| `parser.py` | 解析 FCC 響應，提取關鍵欄位 |
| `detector.py` | 比對新舊數據，輸出變更清單 |
| `notifier.py` | 構建並發送 Telegram 訊息 |
| `scheduler.py` | 封裝 APScheduler，定義每日執行邏輯 |

---

## 4. 預估工時

| 階段 | 工作項目 | 工時 (小時) |
|------|----------|-------------|
| **1. 基礎建設** | 專案初始化、依賴安裝、配置管理 | 2 |
| **2. 數據獲取** | FCC API 串接、SpiderCloud 整合（如有） | 4 |
| **3. 數據處理** | 解析、過濾、數據結構設計 | 3 |
| **4. 變更偵測** | 差異比對邏輯、SQLite 狀態管理 | 3 |
| **5. 通知系統** | Telegram Bot 設定、訊息模板 | 2 |
| **6. 排程與部署** | APScheduler/systemd 整合、測試 | 2 |
| **7. 測試與優化** | 單元測試、邊界情況處理 | 2 |
| **合計** | | **18 小時** |

### 工時說明
- 若 SpiderCloud API 文件完整，可減少 2 小時
- 若只需監控單一來源，可減少 1 小時
- 建議預留緩衝：20 小時

---

## 5. 部署建議

### 5.1 定時機制選擇

| 方案 | 優點 | 缺點 |
|------|------|------|
| **APScheduler (程式內)** | 單一程序，易管理 | 程序掛則停止 |
| **systemd timer** | 系統級可靠，獨立於程式 | 需 systemd 知識 |
| **cron** | 簡單萬用 | 精度只到分鐘 |

**推薦**：APScheduler（開發簡單）或 systemd timer（生產環境）

### 5.2 執行時段

- 建議 UTC 14:00-16:00（美國東部時間上午）
- 可避開 FCC 伺服器維護時段

---

## 6. 風險與緩解

| 風險 | 緩解措施 |
|------|----------|
| FCC API 變更 | 版本化 API 呼叫，失敗告警 |
| 頻率限制 | 指數退避重試，指派人為監控 |
| Telegram 限流 | 批量發送，錯誤重試 |
| 數據丟失 | 每次同步保留上一份快照 |

---

*文檔版本: 1.0*
*建立日期: 2026-02-24*
