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
