# FCC Monitor — 蒸餾記憶 (Distilled Context for All Agents)

> 最後更新: 2026-03-12
> 用途: 供所有參與本專案的 AI Agent（Claude、Gemini、MiniMax 2.5 等）快速同步上下文

---

## 一句話

監控 10 家工業條碼機/行動電腦廠商在 FCC（美國 FCC 設備認證）的新申請，自動下載 Label PDF 並透過 Telegram 逐筆推播通知。

## 架構（5 行版）

```
settings.yaml → config.py (Pydantic)
main.py → for grantee_code:
  fetcher.py (Browserless 優先) → List[FCCRecord]
  database.py (SQLite) → filter_new → new records
  pdf_fetcher.py → Label PDF → notifier.py → Telegram (document + caption)
```

## 關鍵模組

| 檔案 | 一句話 |
|------|--------|
| `src/models.py` | `FCCRecord` dataclass，fingerprint 用 SHA256 |
| `src/config.py` | Pydantic Settings，從 `config/settings.yaml` + `.env` 載入 |
| `src/fetcher.py` | FCC 搜尋（SpiderCloud / Browserless / Playwright 三策略） |
| `src/database.py` | SQLite CRUD，`filter_new()` 做去重 |
| `src/pdf_fetcher.py` | 下載 FCC Label PDF，三層 fallback: direct → SpiderCloud → Browserless |
| `src/notifier.py` | Telegram `sendMessage` + `sendDocument`，逐筆推播含 PDF |
| `src/main.py` | CLI 入口，支援 `--dry-run` / `--seed` / `--daemon` |
| `src/c2pc_detector.py` | Class II Permissive Change 偵測 |
| `src/detector.py` | 新舊 FCC ID 比對 |
| `src/circuit_breaker.py` | 爬蟲容錯斷路器 |

## 已知陷阱（⚠️ 必讀）

1. **Spider.cloud POST Bug** — `/scrape` API 吃掉 POST body，改用 Browserless
2. **Akamai 403** — FCC attachment 下載需要 session cookies，必須在 Browserless 瀏覽器 context 內用 `fetch()` 取得 PDF
3. **application_id 是 Base64** — 不是純數字，URL encode/decode 要小心（`%3D%3D` ↔ `==`）
4. **FCC attachment 路徑** — 正確路徑是 `/eas/GetApplicationAttachment.html`，不是 `/oetcf/eas/reports/`

## 監控對象

Zebra (UZ7) · Honeywell (HD5) · Datalogic (U4F, U4G) · Point Mobile (V2X) · Bluebird (SS4) · Unitech (HLE) · Proglove (2AOJL) · Chainway (2AC6A) · Fujian Newland (2AR9L) · Generalscan (2ADNT)

## 技術棧

Python 3.11+ · loguru · httpx · pydantic · pydantic-settings · SQLite · Browserless.io · Spider.cloud

## 外部服務

| 服務 | 用途 | 環境變數 |
|------|------|----------|
| Browserless.io | Headless Chrome (Akamai bypass) | `BROWSERLESS_API_KEY` |
| Spider.cloud | 備用 proxy/rendering | `SPIDERCLOUD_API_KEY` |
| Telegram Bot | 推播通知 | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |

## Git 慣例

- 主分支: `main`
- 開發分支: `claude/fcc-monitor-setup-qYtN5`
- Commit prefix: `feat:` / `fix:` / `chore:` / `docs:` / `test:` / `style:`

## NERV 團隊角色

| 角色 | 代號 | 職責 |
|------|------|------|
| 司令 | Joe | 最高決策者 |
| PM | 葛城美里 | 任務派遣、進度追蹤 |
| 工程師 | 碇真嗣 (Claude) | 代碼實作、Git |
| Architect | 綾波零 | 架構諮詢、Code Review |
| SRE | 渚薰 | 運維、重大決策 |
| Creative | 明日香 | UI/UX（禁止動代碼） |
| Analyst | 真希波 | 情報分析 |
| Secretary | PenPen | 團隊秘書 |

## 已完成里程碑（2026-03-12 全數達成 ✅）

- [x] Antenna PDF 下載
- [x] 通知排版優化（PDF 放下方）
- [x] Bot token 確認（發到正確頻道）
