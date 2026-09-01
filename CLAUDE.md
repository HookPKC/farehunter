# FareHunter

台灣出發機票低價監控（Python）。資料源為 Travelpayouts / Aviasales Data API。
專案結構與執行方式見 `README.md`。

## Agent skills

### Issue tracker

Issues 記在 GitHub（`HookPKC/farehunter` 的 Issues）。See `docs/agents/issue-tracker.md`.

### Triage labels

沿用五個標準 triage 標籤，名稱不做改寫。See `docs/agents/triage-labels.md`.

### Domain docs

Single-context：根目錄一份 `CONTEXT.md` + `docs/adr/`。See `docs/agents/domain.md`.

已記錄的決策（做同類評估前先讀，避免重跑已經有答案的實驗）：
- `docs/adr/0001-google-flights-deals.md` — 為何不採用 SerpAPI 的 deals 引擎
