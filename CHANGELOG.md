
# NERV FCC-Monitor 更新日誌 (Changelog)

## v2.1.0 (2026-03-12)

**🚀 新功能 (Features)**
* **多檔案 PDF 同時抓取:** 爬蟲邏輯升級，不僅抓取標籤 (Label) PDF，只要有上傳天線規格 (Antenna) 的 PDF 也會一併攔截下來。(`src/pdf_fetcher.py`)
* **Telegram MediaGroup 整合:** 修改通知模組 (`src/notifier.py`)，若有超過一個 PDF 檔案被下載，系統會自動使用 Telegram 的 `sendMediaGroup` 將檔案群組化傳送，避免通知洗版。
* **排版優化:** 發送通知時，強制作為情報總結的純文字獨立發送於最上方，確保所有 PDF 附件乖乖排在文字下方。
* **C2PC 判定與通知升級:** 從原先僅回報標題，改為將記錄內容、產品描述、日期及 FCC ID 加入通知，並特別標示是否為 C2PC 變更案。
* **無頭瀏覽器突穿 Akamai (AT 力場):** 引進 Browserless 自動化流程，完美繼承 Spidercloud 搜尋階段的通行證 (Cookies) 來進行實體 PDF 的繞道下載，完全迴避 FCC Akamai 的 `403/401` 與封鎖阻斷。
* **品牌資料庫擴增與篩選器:** 修正了 Datalogic 等品牌的正確 Grantee 對應，並加入了新對手 Generalscan 的追蹤；新增 `since_date` 永久過濾器與 `--since-days` 等精準度篩選。

**🐞 修復 (Bug Fixes)**
* **SpiderCloud Payload 修復:** 修正了 HTTP POST payload 會觸發 Spidercloud API 400 Bad Request 的 JSON 解析問題。
* **無狀態存取失效 (PDF Download):** 修補了直接用 `httpx` GET 下載 PDF 被 Akamai 切斷的問題，強制要求所有 PDF 必須在 Browserless 開啟的帶狀態 Session 下擷取。
