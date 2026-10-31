# FareHunter AI 接手指令（HANDOFF_AI）

**讀者**：接手本專案的任何 AI 模型（Claude Opus/Sonnet、Codex、GPT 系列或其他），
以及想了解 AI 協作規範的人類。本文件不綁定任何特定模型；「你」指接手的 AI。
最後更新：2026-10-31。

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
- 航空公司官網實價：Scrape.do verify-airlines **每日**（驗證看板候選）
- Google 實價（量最大）：Bright Data，monitor **每小時**順手查（ADR 0004）——警報發出前先驗證
  同一行程，剩下的額度巡檢各航線最便宜的行程；每月 5,000 次免費、配速用到 95%，帳本在
  prices.db 的 `api_usage`。沒有 `BRIGHTDATA_API_KEY` 時完全不碰。
- **觀察中（不參與任何判斷）**：fli Google 日曆 `fli-observe`，跟在 fsc／verify 之後
  每天約 2 次，只寫 `data/fli/*.jsonl.gz`、**不進 prices.db**（理由與第二階段標準見
  ADR 0003）。看板、警報、健康檢查都不讀它。

> 曾有第三層「Google 日曆實價」（SearchApi gcal-sweep／longrange-sweep）。
> SearchApi 的額度是**一次性試用 100 次**（實測跨月不重置），2026-08 用盡且
> 不續費，該層的程式已於同月移除；留下的 `long_range` 表（1,277 筆、零讀者）經使用者
> 同意，2026-10-26 起由 `Store.__init__` 刪除（舊資料在 git 歷史的 prices.db 裡）。
> 歷史 `provider='searchapi'` 觀測仍留在 DB，標籤對應表因此保留。

**付費 API 額度（2026-08 起可觀測，不要再憑印象推論）**：
`docs/quota.json` 由 fsc-snapshot 每日寫入並 commit，內容是 SerpAPI 真實
方案、本月用量、剩餘次數與判讀（`farehunter.quota`；打的是免費且不計額度的
`account.json`）。改 `SEARCHES_PER_DAY` 之前先看這份紀錄與它的 git 歷史。
背景：`serpapi_flights` 的註解長期誤寫「free tier (~100/month)」，而實測
180 次/月從未被擋——整個系統圍繞一個錯的前提設計。status 為 `low` /
`exhausted` 時會記 warning；`headroom` 是機會提示不是警報。
Bright Data 的用量看 prices.db 的 `api_usage`（每日每來源），配速規則見 ADR 0004；
**它的 zone 管理 API 回應含 zone 密碼，任何探測都不得印出**。

**觸發與防護四層（2026-07-07 全部就位）**：
1. 雙觸發：GitHub schedule `:07`（不可靠，實測命中率曾僅 ~7%，保留為備援）＋
   **cron-job.org `:17` 外部觸發（2026-07-07 上線，主力）**
2. 55 分鐘防重複 guard（`runner.guard_decision`，fail-open）：資料齡 <55 分即跳
   過並寫 `GITHUB_OUTPUT skip=true`，讓 export/commit 步驟一併跳過
3. 使用者端：首頁兩級新鮮度提示（90 分黃、3 小時紅）
4. 維運者端：monitor 尾端 optional 心跳（repo secret `HC_PING_URL`，未設即休眠）
   ＋ cron-job.org 失敗 email

**concurrency**：monitor/fsc/verify/archive-db/fli-observe 共用 `farehunter` 群組
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

## 3b. 時鐘：注入要嘛完整、要嘛不要做

**半套的時鐘注入比完全不注入更危險**——它讓人以為時間已經被控制住了。
2026-09 五天內因此停擺兩次、損失 14 班觀測（monitor.yml 先跑 pytest 才抓價，
測試紅燈＝抓價停擺）：

| 日期 | 半套在哪 | 後果 |
|---|---|---|
| 09-02 | `export_web.export()` 有 `now=` 但測試沒傳 | 種子時間 24 小時後過期，缺 4 班 |
| 09-06 | `runner.run(now=)` 沒蓋到 `today_iso` / `upcoming_months()` | 寫死的出發日隔天變「昨天」，缺 9 班 |

規則：
- 函式收了 `now` / `now_ref` / `today`，就要**貫穿到所有時間比較**，
  包含 SQL 裡的 `date('now')` / `julianday('now')`（那些也要吃參數）。
- 測試裡**不要寫死未來日期**。由注入的時鐘偏移導出（`NOW.date() + 40 天`），
  否則真實時間走過去那天就會紅。
- 改完跑 `scripts/time_travel_tests.sh`——它用 faketime 把整套測試放到未來
  幾個時間點跑，會直接指出引爆日。**不要用 freezegun**：它只騙 Python，
  SQLite 的 `date('now')` 仍是真實時間，會產生大量偽陽性（實測 7 個裡 6 個假）。

## 4. 判讀規範：缺一筆 hourly commit 的三種正常原因

monitor 每小時 `:17` 應產生一筆 `chore: price observations` commit。**缺一筆
不等於故障**，先分辨（均有 2026-07 真實案例）：

| 樣態 | 特徵 | 案例 | 處置 |
|---|---|---|---|
| **guard 跳過** | 該班 run 綠色成功，「Run monitor」log 有「跳過本輪」，Export／Commit 步驟被略過；55 分鐘內**另一班 monitor** 已經抓過價（guard 只看 monitor 寫的 aviasales 觀測，FSC／Verify 的 commit 不會觸發跳過）。常見情形：晚到的 GitHub `:07` 排程與 `:17` 外部觸發落在同一小時 | 10/18 10:54：GitHub 排程那班晚到，距 10:17 那班抓價不到 55 分 → 正常跳過。若晚到的那班反而先抓了價（git log 上前一小時多一筆不在 :17／:18 的 commit），下一個 :17 就會跳過、看起來缺一筆 | 不處理。資料其實剛被抓過 |
| **排隊補跑** | 兩個 workflow 同時到場，晚到的 queued 後照跑，commit 時間略延後 | 7/9 16:17：verify 與 monitor 同場，兩者都成功（16:17 與 16:18 各一筆 commit） | 不處理。concurrency 正常運作 |
| **平台單次取消** | run 顯示 cancelled，但當下群組內無人佔用、也非手動取消 | 7/9 06:17：monitor cancelled，當日 fsc/verify 均在 16:1x 才跑（無撞車）→ GitHub 平台偶發 | 單次＋下一班正常＝忽略 |

**升級門檻**：連續 **≥2 小時**沒有新的 `chore: price observations` commit（其他寫入者
的 commit 不算）、且首頁時間戳不動，才視為異常，
照 `RUNBOOK_HOOK.md` §0 三步處理。

## 5. 部署驗收與 token 規則

- 部署驗收＝**三方 SHA 一致**：main HEAD ＝ Deploy Pages run 的 `head_sha` ＝
  github-pages environment 最新 deployment 的 SHA。缺一不可。
  自動 commit 裡唯一的合法落差：main HEAD 是 `chore: archive observations` 或
  `chore: fli observe` 時（合併 PR 等人工 commit 同理）。Deploy Pages 只跟在 Monitor／FSC／Verify 之後
  跑，封存與 fli 觀察都不改 `docs/`，所以不會為它們部署；以下一班 Monitor 觸發的 deploy 為準
  （通常一小時內），屆時三方會對上。
- 手機版 UI 改動需使用者 iOS 實機最終確認。
- token：fine-grained、只限本 repo、短效期、用完提醒使用者刪除。改一般檔案＝
  Contents RW；動 `.github/workflows/` 必須加 Workflows RW（缺了 push 會被拒）；
  要 dispatch 驗證再加 Actions RW。平常任務（唯讀調查、提案）**不需要 token**。

## 6. 已知非問題（不要「修」它們）

- **「點進 Google Flights 價格跟本站不同」≠ bug**。本站是觀測價/快取估價/歷史
  低價提示（更新頻率見 §1），Google 點入後是**即時**搜尋結果，兩者必有時間差；
  票價本身也隨時波動。頁面已用「≈」「約」「近期觀測」「以 Google Flights 為準」
  誠實標示。使用者或親友再問，指向 `RUNBOOK_HOOK.md` 的白話說明即可。除非
  使用者明確立案，否則不要為此改前端語意。**也不是市場（`gl`）造成的**：2026-10-31
  實測預設與 `gl=TW` 的價格完全相同（ADR 0003），不必替 SerpAPI／fli 加 `gl`。
- 首頁黃/紅提示條是誠實訊號不是 bug；出現時沿源頭排查（PLAYBOOK 1-6），
  不要去改提示條本身。
- 手動 Run 後 log 出現「跳過本輪」是 guard 設計行為。
- **Deploy Pages 偶發 `Failed to get ID Token`**：訊息結尾那句
  `Ensure GITHUB_TOKEN has permission "id-token: write"` **會誤導** —— 該權限
  本來就在 deploy-pages.yml 裡。真正的錯誤在上一行：`Request timeout:
  /6//idtoken/...` at `OidcClient`，是 GitHub 取 OIDC token 逾時，平台偶發。
  實測 2,129 次執行 22 次失敗（約 1%），從 2026-07 就是這個基準率，與任何
  程式或 action 版本無關。
  而且 `workflow_run` 會讓同一個 commit 觸發多次 deploy，失敗那次通常前一
  分鐘已經有同 SHA 成功發佈（2026-09-03 04:18 成功 / 04:19 失敗，同 SHA）。
  **判讀方式**：比對「最後一次成功 deploy 的 head_sha」與 main HEAD，一致就
  沒事（§5 的三方 SHA 規則；main HEAD 剛好是封存 commit 時見 §5 的例外）。不要為此改 permissions 或 action 版本。
- 「All jobs were cancelled」email：先套 §4 判讀表，多數可忽略。
- monitor 綠色但 log 有 `Search failed … API error 0: ReadTimeout`（或
  `ConnectionError`）＝單一月份連線逾時，設計行為：2026-10-18 一次逾時讓整輪中止、
  那一小時全丟之後，改成照月份跳過；同一輪累計 3 次就熔斷、不再呼叫 API
  （`runner.MAX_TRANSPORT_ERRORS`）。只有「整輪一次都沒成功」才會 exit 1 變紅
  （`runner.all_searches_failed`），token 過期也走這條。
- 綠色 run 的 log 有 `LINE send failed (ReadTimeout)`／`Telegram send failed
  (ConnectionError)` 接著「通知發送失敗，保留至下一輪重試」（Verify Airlines 則是
  「推播失敗」）＝推播管道一時連不上，設計行為：那則通知沒記成已送出，下一輪會再送。
  2026-10-26 以前這種例外會直接讓整輪掛掉、Export／Commit 都不跑（`notify.py`）。
  只有整天都送不出去才需要查 token 或管道設定。
- **KHH→NGO 長期 stale/dead ≠ 斷線**。Travelpayouts 只回 Aviasales 使用者近 7 天
  實際搜尋過的票價，這條線約 1–3 週才被搜一次：2026-07～09 共 6 段資料，每段
  都是一筆台灣虎航直飛票價、剛好存活 7 天後消失（最後一段 09-14～09-21），
  中間是空窗；07-29～08-12 那次促成 health.py 的空窗也是同一原因。其餘時間
  靠 Google 每 4–5 天抽查，所以健康狀態在 stale/dead 間跳。這條線仍值得留著：
  09 月前的 4 段快取票價每段都觸發門檻警報，且與 Google 價格吻合。KHH-CTS
  （每小時約 1–2 個出發日）是同類薄航線。**不要再試 `market` 參數**：2026-10-25
  實測 12 個市場結果完全相同、11 個端點對 KHH-NGO 全空（ADR 0003）。冷門線的解法
  改走 fli 觀察期（同一份 ADR）。
- **fli Observe 紅燈 ≠ 監控故障**。它只寫 `data/fli/`，抓價、警報、看板都不讀；紅燈
  代表這輪 fli 拿不到資料、被封鎖或讀不到 prices.db，處理見 RUNBOOK §3c。
  `data/fli/` 的 `chore: fli observe` commit 每天約 2 筆，屬正常。打 Google 的 observe
  job **刻意不進** `farehunter` 群組（只有 20 秒的 commit job 進），所以不會拖到 monitor；
  別把 observe 也加進群組（理由見 ADR 0003「決定」）。

## 7. 已知長期課題（P2，非急務）

- **fli 觀察期**（ADR 0003）：2026-10-26 開始，預計 2026-11-09 跑
  `python -m farehunter.fli_calendar report data/fli 15`（涵蓋 10-26 第一輪），四項標準都
  PASS 才提第二階段（讓 fli 價格參與警報、逐步取代 SerpAPI）。日曆標準的計分規則
  2026-10-26 修正過：出過錯的時段照算，只有每次都明確回空的才當成沒有班表（ADR 0003）；
  看 `calendar_by_route` 找拖累的航線。2026-10-31 已排一次性提醒（2026-11-10 08:30 台灣）
  到時跑這份報告並整理結論；沒收到提醒就照上面的指令手動跑。
  觀察期間不要讓任何既有讀者讀 `data/fli/`。

- **prices.db 成長**：2026-10 已處理（ADR 0002）。出發日已過 30 天的觀測由
  `archive-db` 每日搬到 `archive/`，現行檔超過壓實大小 10% 才 VACUUM，預估
  在約 37–45 MiB 之間起伏（隨每日觀測量變動）；全歷史查詢讀 `observations_all`。
  封存讓 git 歷史長得比不封存更慢（ADR 0002「複查」）。剩下的長期課題：SQLite
  一次最多掛 10 個封存檔，照每年 3–4.5 個新檔，約 2–3 年後到頂——封存檔
  到第 8 個時開始提案。**不能單純 revert 這組改動**：封存跑過之後，讀者改回只讀
  prices.db 不會報錯，但「史上最低」會悄悄算錯。封存檔遺失或筆數不符時的還原
  步驟見 RUNBOOK §3b。
- 實價來源（fsc/verify）僅靠 GitHub 排程、時間漂移嚴重。已評估過
  「比照 monitor 加外部觸發」：因付費 API 消耗與低頻本質，**判定不值得**，
  維持現狀。除非使用者重新立案，不要主動重提。

## 8. 給接手者的工作哲學

這個專案最大的敵人不是難題，是「順手」——順手重構、順手改文案、順手擴大
範圍；歷史上的事故幾乎都源於此。它的靈魂是**誠實**：寧可顯示「資料舊了」也
不假裝新鮮；寧可 guard 重複跑也不讓它擋路。測試綠不代表 live 對，沒有紅字
不代表排程活著——**去驗證「該發生的事有沒有發生」**。使用者給的限制清單是
保護你的，照著走就不會出事。
