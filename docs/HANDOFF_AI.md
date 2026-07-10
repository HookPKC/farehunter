# FareHunter AI 接手指令（HANDOFF_AI）

**讀者**：接手本專案的任何 AI 模型（Claude Opus/Sonnet、Codex、GPT 系列或其他），
以及想了解 AI 協作規範的人類。本文件不綁定任何特定模型；「你」指接手的 AI。
最後更新：2026-07-10。

---

## 0. 第一輪只做這些事

1. 依序讀：`README.md` → `docs/PLAYBOOK_SHORT.md` → `docs/RUNBOOK_HOOK.md` →
   （需要根因案例時才讀）`docs/PLAYBOOK.md`。
2. **不要重分析整個專案**。要定位程式碼用 grep，只讀與當前任務直接相關的檔案。
3. 回覆使用者：確認已讀、複述禁區、等待第一個任務。不要主動提出改造計畫。

## 1. 專案一句話與架構事實

台灣→日本 8 航線機票**低價提示器**（不是即時報價引擎）：
KHH→NRT/KIX/CTS/FUK/OKA/NGO、TPE→NRT/KIX。單檔 PWA（`docs/`）由 GitHub
Pages 服務，SQLite（`prices.db`）存價，LINE 推播警報。

**資料來源三層（更新頻率天差地遠，這是理解一切價差問題的鑰匙）**：
- 快取估價：Travelpayouts/Aviasales，monitor **每小時**（頁面標「≈」「約」）
- Google 實價：SerpAPI fsc-snapshot **每日** 06:10 UTC（「傳統航空最低」等精確價）
- Google 日曆實價：SearchApi gcal-sweep **每週一** 05:30 UTC、longrange-sweep
  **每月 3/18 日**（綠色月度數字）

**觸發與防護四層（2026-07-07 全部就位）**：
1. 雙觸發：GitHub schedule `:07`（不可靠，實測命中率曾僅 ~7%，保留為備援）＋
   **cron-job.org `:17` 外部觸發（2026-07-07 上線，主力）**
2. 55 分鐘防重複 guard（`runner.guard_decision`，fail-open）：資料齡 <55 分即跳
   過並寫 `GITHUB_OUTPUT skip=true`，讓 export/commit 步驟一併跳過
3. 使用者端：首頁兩級新鮮度提示（90 分黃、3 小時紅）
4. 維運者端：monitor 尾端 optional 心跳（repo secret `HC_PING_URL`，未設即休眠）
   ＋ cron-job.org 失敗 email

**concurrency**：monitor/fsc/gcal/longrange/verify 共用 `farehunter` 群組
（`cancel-in-progress: false`＝排隊不互砍）；deploy-pages 獨立 `pages` 群組。

## 2. 絕對禁區（違反任一項＝事故）

- 不重寫 Recommendation Engine；不動 Google Flights 深連結的 `q=` 參數（實測
  過會解析退化，已凍結）
- 不改價格語意（六處對齊：google 觀測價精確、快取一律「約 NT$X」百位化）；
  不改通知語意（3 處測試鎖定）
- 不把「run success / repo 有新 code」當部署驗收（見 §5）
- token 絕不印出、不寫檔、不進 commit；任何 ping URL / secret 值不得出現在
  文件、log、回報中（文件只能寫 secret 名稱如 `HC_PING_URL`）
- 一輪一件事；先提案（問題理解＋最小改動＋允許檔案清單＋風險＋驗證方式）
  等使用者確認才實作；需要動允許清單以外的檔案時，停下回報

## 3. 鐵律：排程表 ≠ 實際執行，先取證再修

GitHub 排程投遞不穩，fsc 名義上每日 06:10 UTC，實際觀測散落 11:49～17:18
（各日不同）。因此：**任何「兩個排程撞在一起」的推論，在改 cron 之前必須用
實際 run 紀錄證明撞車真的發生過**。2026-07-09/10 有一次完整誤診（詳
`PLAYBOOK.md` 案例 1-7）：表面上 fsc 06:10 每天必撞 monitor 06:17，實查後
發現撞車幾乎不存在、該次 cancelled 另有原因，改 cron 的提案因此撤案。
不要重蹈：先列出「若假設為真，run 紀錄應該長什麼樣」，再去撈紀錄比對。

## 4. 判讀規範：缺一筆 hourly commit 的三種正常原因

monitor 每小時 `:17` 應產生一筆 `chore: price observations` commit。**缺一筆
不等於故障**，先分辨（均有 2026-07 真實案例）：

| 樣態 | 特徵 | 案例 | 處置 |
|---|---|---|---|
| **guard 跳過** | 該班 run 綠色成功，log 有「跳過本輪」；稍早（<55 分）有 fsc/gcal 等來源剛 commit 並更新過 `data.json` | 7/8：fsc 漂移至 12:05 跑完並 export，12:17 monitor 見資料齡 12 分 → 正常跳過 | 不處理。資料其實剛被刷新 |
| **排隊補跑** | 兩個 workflow 同時到場，晚到的 queued 後照跑，commit 時間略延後 | 7/9 16:17：verify 與 monitor 同場，兩者都成功（16:17 與 16:18 各一筆 commit） | 不處理。concurrency 正常運作 |
| **平台單次取消** | run 顯示 cancelled，但當下群組內無人佔用、也非手動取消 | 7/9 06:17：monitor cancelled，當日 fsc/verify 均在 16:1x 才跑（無撞車）→ GitHub 平台偶發 | 單次＋下一班正常＝忽略 |

**升級門檻**：連續 **≥2 小時**沒有新 commit、且首頁時間戳不動，才視為異常，
照 `RUNBOOK_HOOK.md` §0 三步處理。

## 5. 部署驗收與 token 規則

- 部署驗收＝**三方 SHA 一致**：main HEAD ＝ Deploy Pages run 的 `head_sha` ＝
  github-pages environment 最新 deployment 的 SHA。缺一不可。
- 手機版 UI 改動需使用者 iOS 實機最終確認。
- token：fine-grained、只限本 repo、短效期、用完提醒使用者刪除。改一般檔案＝
  Contents RW；動 `.github/workflows/` 必須加 Workflows RW（缺了 push 會被拒）；
  要 dispatch 驗證再加 Actions RW。平常任務（唯讀調查、提案）**不需要 token**。

## 6. 已知非問題（不要「修」它們）

- **「點進 Google Flights 價格跟本站不同」≠ bug**。本站是觀測價/快取估價/歷史
  低價提示（更新頻率見 §1），Google 點入後是**即時**搜尋結果，兩者必有時間差；
  票價本身也隨時波動。頁面已用「≈」「約」「近期觀測」「以 Google Flights 為準」
  誠實標示。使用者或親友再問，指向 `RUNBOOK_HOOK.md` 的白話說明即可。除非
  使用者明確立案，否則不要為此改前端語意。
- 首頁黃/紅提示條是誠實訊號不是 bug；出現時沿源頭排查（PLAYBOOK 1-6），
  不要去改提示條本身。
- 手動 Run 後 log 出現「跳過本輪」是 guard 設計行為。
- 「All jobs were cancelled」email：先套 §4 判讀表，多數可忽略。

## 7. 已知長期課題（P2，非急務）

- **prices.db 成長**：SQLite 檔案每小時隨 commit 進 repo（2026-07 已 12,000+
  筆觀測）。一兩年尺度 repo 會肥大、clone 變慢。可能方向：歷史歸檔、shallow
  clone 指引、或改存放策略——屬大改，需完整提案與使用者明確核准，勿順手做。
- 實價來源（fsc/gcal/longrange）僅靠 GitHub 排程、時間漂移嚴重。已評估過
  「比照 monitor 加外部觸發」：因付費 API 消耗與低頻本質，**判定不值得**，
  維持現狀。除非使用者重新立案，不要主動重提。

## 8. 給接手者的工作哲學

這個專案最大的敵人不是難題，是「順手」——順手重構、順手改文案、順手擴大
範圍；歷史上的事故幾乎都源於此。它的靈魂是**誠實**：寧可顯示「資料舊了」也
不假裝新鮮；寧可 guard 重複跑也不讓它擋路。測試綠不代表 live 對，沒有紅字
不代表排程活著——**去驗證「該發生的事有沒有發生」**。使用者給的限制清單是
保護你的，照著走就不會出事。
