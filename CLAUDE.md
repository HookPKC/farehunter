# FareHunter

台灣出發機票低價監控（Python）。資料源為 Travelpayouts / Aviasales Data API。
專案結構與執行方式見 `README.md`。

**一律用繁體中文回覆使用者**，包括工具呼叫之間的說明、排程檢查的回報；程式碼、指令、
檔名、專有名詞可以保留原文。`.claude/settings.json` 設了 `language`，另有 hook 每則訊息提醒、
回覆完檢查（`.claude/hooks/zh_only.py`）：最後一則回覆主要是英文就會被擋下重寫。

## Agent skills

### Issue tracker

Issues 記在 GitHub（`HookPKC/farehunter` 的 Issues）。See `docs/agents/issue-tracker.md`.

### Triage labels

沿用五個標準 triage 標籤，名稱不做改寫。See `docs/agents/triage-labels.md`.

### Domain docs

Single-context：根目錄一份 `CONTEXT.md` + `docs/adr/`。See `docs/agents/domain.md`.

已記錄的決策（做同類評估前先讀，避免重跑已經有答案的實驗）：
- `docs/adr/0001-google-flights-deals.md` — 為何不採用 SerpAPI 的 deals 引擎
- `docs/adr/0002-prices-db-archive.md` — prices.db 依出發日封存到 `archive/`（防 GitHub 100 MiB 單檔上限）
- `docs/adr/0003-fli-google-calendar.md` — 13 種免費機票來源實測比較；fli（Google 日曆）觀察期與第二階段標準
- `docs/adr/0004-brightdata-google-prices.md` — Bright Data 免費額度補 Google 實價（警報前驗證＋每小時巡檢、配速、帳本）
- `docs/adr/0005-more-fare-apis.md` — 外部「更多機票 API」比較表的對照；ChocoData 經通用擷取 universal.get 抓 Google Flights 與 Bright Data 4/4 一致（接入另案）
