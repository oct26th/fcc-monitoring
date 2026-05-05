# FCC Monitor — 交接手冊

> 一份文件帶走專案。先讀這份，再讀 `CLAUDE.md`、`AGENTS.md`、`memory/DISTILLED.md`。

---

## 一、專案在做什麼

監控 11 家工業條碼機 / 行動電腦廠商在 **FCC 設備認證資料庫** 的新申請，
偵測到新型號就：

1. 從 FCC 官網下載 Label PDF（產品標籤、認證細節）
2. 透過 **Telegram bot** 即時推播通知
3. 偵測 **C2PC**（Class II Permissive Change，硬體暗中升級）

監控對象（grantee codes）：Zebra (UZ7)、Honeywell (HD5)、Datalogic (U4F/U4G)、Point Mobile (V2X)、Bluebird (SS4)、Unitech (HLE)、Proglove (2AOJL)、Chainway (2AC6A)、Fujian Newland (2AR9L)、Generalscan (2ADNT)。

---

## 二、技術棧

- **語言**：Python 3.11+ （目前用 3.13/3.14 都可）
- **HTTP**：`httpx`
- **Logging**：`loguru`
- **設定**：`pydantic-settings` + YAML
- **DB**：SQLite，檔案 `data/fcc_monitor.db`
- **爬蟲後端**：
  - 主：[Spider.cloud](https://spider.cloud) — 繞 Akamai TLS 指紋
  - 備：[Browserless.io](https://www.browserless.io) — headless Chrome，做 PDF 下載 + 表單提交
  - 備備：本地 Playwright

---

## 三、Repo

```
https://github.com/oct26th/fcc-monitoring  (public)
```

---

## 四、五分鐘啟動

```bash
# 1. Clone
git clone https://github.com/oct26th/fcc-monitoring.git
cd fcc-monitoring

# 2. 建 venv + 裝相依
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. 填環境變數
cp .env.example .env
# 編輯 .env，填入 4 個 API key（見「五、需要準備的 Token」）

# 4. Dry-run（不寫 DB、不發通知，只驗證能跑）
python -m src.main --dry-run

# 5. 真的跑一次
python -m src.main

# 6. 排程模式（每天 14:00 UTC 跑一次，由 settings.yaml 控制）
python -m src.main --daemon
```

---

## 五、需要準備的 Token（4 個）

對方要自己申請或跟交接人拿。**不要把 token 貼在 git、Slack、Email**，用 1Password / 私訊 / 加密管道傳遞。

| Token | 申請地點 | 計費 | 用途 |
|-------|----------|------|------|
| `SPIDERCLOUD_API_KEY` | https://spider.cloud → Dashboard → API Keys | 免費額度有限，付費依 request 計算 | **主**爬蟲。繞 FCC 站 Akamai 防爬 |
| `BROWSERLESS_API_KEY` | https://www.browserless.io → Sign up → API Token | 免費 1000 units/月，$50/月起 | **備**爬蟲 + PDF 下載（headless Chrome） |
| `TELEGRAM_BOT_TOKEN` | Telegram 找 `@BotFather` → `/newbot` | 免費 | 通知 bot |
| `TELEGRAM_CHAT_ID` | 把 bot 加進目標 chat → 訪問 `https://api.telegram.org/bot<TOKEN>/getUpdates` 看 `chat.id` | 免費 | 通知目標 |

> ⚠️ 移交當下的 Spider.cloud 帳號 quota 已用完（HTTP 401: "Credits or a valid subscription required"）。對方接手後要**先充值或開新帳號**才能跑。Browserless / Telegram 兩個是好的。

---

## 六、現有設定總覽

| 檔案 | 內容 |
|------|------|
| `config/settings.yaml` | 監控品牌清單、爬蟲策略、排程時間、DB 路徑 |
| `.env` | 4 個 secret（自己填，不 commit） |
| `data/fcc_monitor.db` | SQLite 資料庫（首次跑會自動建立） |
| `data/pdfs/` | 下載的 Label PDF 存放區 |
| `logs/fcc_monitor.log` | 滾動 log，10 MB 一檔，留 30 天 |

`since_date`（`config/settings.yaml`）= `2026-03-01` —— 早於這個日期的 FCC 紀錄會被忽略，避免第一次跑時把幾年前的資料全發成通知。

---

## 七、執行入口

| 指令 | 行為 |
|------|------|
| `python -m src.main` | 跑一次完整掃描，會寫 DB、會發通知 |
| `python -m src.main --dry-run` | 跑流程但不寫 DB、不發通知（沙箱測試用） |
| `python -m src.main --daemon` | Loop 模式，按 `settings.yaml` 的 `scheduler.run_time` 排程 |

---

## 八、Code 地圖

```
src/
├── main.py              # 入口，串接整個流程
├── config.py            # 載入 settings.yaml，env var 展開
├── models.py            # FCCRecord dataclass
├── fetcher.py           # Spider.cloud / Browserless / Playwright 三套 fetcher
├── pdf_fetcher.py       # FCC Label PDF 下載（走 Browserless session）
├── detector.py          # 比對新舊 FCC ID
├── c2pc_detector.py     # 偵測硬體暗中升級
├── notifier.py          # Telegram 通知（含 PDF 群組推送）
├── database.py          # SQLite CRUD
├── circuit_breaker.py   # 爬蟲容錯
├── scheduler.py         # 排程
└── v2/
    ├── data_lake.py     # V2: 競品情報 SQLite
    ├── intelligence.py  # V2: LLM 萃取（已 decoupled，留 schema）
    ├── brand_crawler.py # V2: 品牌官網爬蟲
    └── brand_sources.py # V2: 10 家品牌的官網入口

tests/
├── test_core_logic.py   # Unit tests
└── test_live.py         # 整合測試（會打外部 API，要有 token）
```

---

## 九、已知陷阱（避免重蹈覆轍）

### Spider.cloud POST bug
`SpiderCloudFetcher` 走 `/scrape` API 時，POST body（如 `grantee_code=UZ7`）會被吃掉變成 GET，造成 FCC 搜尋頁回空白。
**現況解法**：改走 Browserless headless 路徑，或用 Spider.cloud `automation_scripts` 端點。
**詳見**：`CLAUDE.md` → 已知問題段落。

### FCC 站 Akamai 防爬
直連 FCC 會被 Akamai 擋 403。一定要透過 Spider.cloud（TLS 指紋繞過）或 Browserless（真實 Chrome 環境）。

### `.venv` 在 Google Drive 會壞掉
如果 repo clone 到 Google Drive 同步資料夾，`.venv/bin/python` 的 symlink 會被 Drive 同步成空字串。
**解法**：clone 到 `~/projects/` 之類的本地路徑，或者每次重建 `.venv`。

---

## 十、V2（競品情報網）

正在進行的下一階段，**不是 V1 監控的核心**，可以先不管。需要時讀 `docs/v2_architecture.md` 與 `CLAUDE.md` 的 V2 段落。
物理切割設計：Python 只負責爬資料寫進 SQLite；LLM 萃取由外部 agent 讀 SQLite 做，避免 LLM API key 寫進這個 repo。

---

## 十一、文件閱讀順序

1. **`HANDOVER.md`**（這份） — 全貌
2. **`CLAUDE.md`** — 對 AI agent 的常駐指示，含已知 bug、架構流程、設計原則
3. **`AGENTS.md`** — 多 agent 角色定義（如果你也用 NERV-style agent 流程；用不到可以忽略）
4. **`memory/DISTILLED.md`** — 蒸餾記憶，最快上下文同步
5. **`docs/architecture.md`** — V1 架構
6. **`docs/v2_architecture.md`** — V2 競品情報網架構

---

## 十二、聯絡人

- 原作者：oct26th (`https://github.com/oct26th`)
- Repo issues：`https://github.com/oct26th/fcc-monitoring/issues`
