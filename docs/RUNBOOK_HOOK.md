# FareHunter RUNBOOK（Hook 專用・不需要 AI 也能照做）

這份文件的對象是**人類 Hook**。任何時候資料停更或看到警示，照這裡做即可。
（AI 接手時的開發規範在 `PLAYBOOK.md`，不在這份。）

---

## 0. 一分鐘總覽：出事時只需要三步

1. **看訊號**：收到 healthchecks email，或首頁出現黃/紅提示條。
2. **手動跑一次**：GitHub → repo → Actions → **FareHunter Monitor** → Run workflow → Run。
3. **看結果**：幾分鐘後首頁時間戳更新、提示條消失＝復原。若 **Deploy Pages** 變紅 → 點進去按 **Re-run all jobs**。

就這樣。以下是細節與設定卡。

---

## 1. 首頁提示條怎麼判讀

| 看到什麼 | 意思 | 要做什麼 |
|---|---|---|
| 沒有提示條 | 資料 90 分鐘內更新過，一切正常 | 不用做任何事 |
| **黃色**「資料已約 X 小時未更新」 | 錯過 1–2 輪更新，可能只是 GitHub 排程延遲 | 可先觀望一小時；急用就照第 2 節手動跑 |
| **紅色**「資料已超過 X 小時未更新」 | 排程很可能又停擺 | 照第 2 節手動跑；若一週內常發生，照第 4 節上外部排程 |
| 收到「All jobs were cancelled」email，或發現某一小時缺一筆更新 | 多半是正常現象：guard 跳過（別的來源剛更新過資料）、兩個排程排隊補跑、或 GitHub 平台偶發取消 | **單次 cancelled 且下一班正常更新＝忽略**。連續 **2 小時以上**首頁時間戳都不動，才照第 0 節處理。細節見 `HANDOFF_AI.md` §4 判讀表 |

提示條每 60 秒自動重算，頁面開著放久也準。

## 2. 手動執行 FareHunter Monitor（30 秒）

1. 開 `github.com/HookPKC/farehunter` → **Actions** 分頁
2. 左側點 **FareHunter Monitor** → 右側 **Run workflow**
3. `force` 選項**平常不用勾**。只有一種情況要勾：資料明明很新（55 分鐘內）
   但你就是要強制重抓——因為系統有「55 分鐘防重複 guard」，資料太新時
   會自動跳過以節省 API（run 會顯示成功但 log 寫「跳過本輪」，這是正常設計，
   不是壞掉）。
4. 按綠色 **Run workflow**，等 1–2 分鐘變綠勾。

## 3. Deploy Pages 失敗時（紅 ✗）

Pages 偶爾一次性抽風（歷史上發生過，artifact 都建好了最後一步掛掉，log 寫「No artifacts named
"github-pages"」）。2026-11-20 起 workflow 會自己等 30 秒再試一次，第二次也失敗才會變紅、寄信。
真的收到失敗信：多半下一輪（一小時內）就自己恢復；想馬上恢復就 Actions → **Deploy Pages** →
點進紅色那筆 → 右上 **Re-run all jobs**。重跑幾乎必過。

若紅的是 **FareHunter Monitor** 本身：點進 log 看第一個紅色步驟——若是「Run monitor」且最後一行是「✖ 本輪 N 次搜尋全部失敗」，往上看 `Search failed` 那幾行：提到 401/token，多半是 Travelpayouts token 過期，到 repo Settings → Secrets 更新 `TRAVELPAYOUTS_TOKEN`；提到 `API error 0: ReadTimeout`／`ConnectionError`，是 Travelpayouts 那端故障，不用處理，恢復後下一班自動補上（只有零星幾個月份逾時時 run 仍是綠的，不會紅）。若訊息是 `ArchiveIntegrityError`，照下面 §3b。

若紅的是 **FSC Snapshot**（或其 log 出現 401 / quota / Invalid API key）：到 SerpAPI dashboard 確認本月額度與 key 狀態（每日最多 6 次、月理論最多約 186 次，方案額度 250，見 dashboard），必要時產生新 key → repo Settings → Secrets 更新 `SERPAPI_KEY` → 舊 key 到 SerpAPI 端撤銷。key 一律不要貼進對話、文件或截圖。

**FSC 每日槽位（每日最多 6 次 SerpAPI）**：固定 3 個輪替槽（Rotation）＋ 最多 3 個實價驗證槽（Verification：Alert 最強低價警報／CTA 首頁推薦按鈕／Hero 首頁大字）。候選不足時驗證槽會少於 3、當天總查詢可能低於 6，屬正常，不是故障。看 log 末行「快照完成」那行的計數：`輪替 3/驗證 N`、`alert/cta/hero` 各幾槽、`API 成功/錯誤/成功無航班`。

**FSC 紅燈 vs 綠燈 warning 判讀**：
- 紅燈（run failed）：只有在「當天所有查詢都失敗」時才會發生（通常是 key 失效 401、額度用盡 quota、或網路問題）——照上一段換 key。**例外**：log 是 `ArchiveIntegrityError` → 跟 key 無關，照 §3b，不要換 key、不要重跑。
- 綠燈但 log 有 `API OK but no matching flight` warning：API 有成功、只是那天那條航線剛好沒有直飛航班可記錄——**這不是故障**，不需處理。

**手動重跑 FSC 注意**：每手動 dispatch 一次最多消耗 6 次額度。偶爾為之無妨（月餘裕約 64 次），但**避免連續多天手動重跑**，以免壓線。若擔心用量，到 SerpAPI dashboard 看當月累計即可。

## 3b. 封存檔出錯時（`ArchiveIntegrityError`）

背景：`prices.db` 只放近幾個月的觀測，出發日已過 30 天的舊資料每天由
**Archive DB** 搬到 `archive/` 資料夾（為了不撞到 GitHub 100 MiB 的單檔上限，
細節見 `docs/adr/0002-prices-db-archive.md`）。`prices.db` 裡記著每個封存檔
應該有幾筆；兩邊對不上時，程式會**故意停下來報錯**，而不是少算歷史資料去
亂發「史上最低」通知。

**怎麼認出來**：Monitor 紅燈，log 裡有 `ArchiveIntegrityError`，後面接
「封存檔 … 不見了」「封存檔 … 有 X 筆，manifest 記 Y 筆」或「封存檔 … 無法讀取」。

**影響範圍**：Monitor、FSC、Verify 一開始讀資料庫就停，**還沒打任何付費 API**
（不會白花額度），但這段期間沒有任何新價格進 repo、首頁時間戳不動。
healthchecks 約 3 小時後寄信；cron-job.org 不會寄（觸發本身是成功的）。
Deploy Pages 照常跑，只是重發沒變的舊資料。**當天處理**。

**絕對不要做**：重跑 Archive DB（它會報同一個錯，修不好）、刪掉 `archive/`
裡的檔案、手動改 `prices.db`、把程式改回「只讀 prices.db」（那樣不會報錯，
但「史上最低」會悄悄算錯，見 ADR 0002「已知限制」）。

**修法：把 `archive/` 還原成最後一次正常封存時的樣子**（約 5 分鐘）。需要：
bash 或 zsh（Windows 用 Git Bash）、能推送到本 repo 的 GitHub 登入（**不是**
cron-job.org 那顆只開 Actions 權限的 token）。git 2.23 以上直接照下面做；更舊
的 git（`git --version`）用下方的替代指令。

```bash
git clone --filter=blob:none https://github.com/HookPKC/farehunter
cd farehunter
# 最後一次正常封存的 commit（只有 Archive DB 會寫 archive/）
SHA=$(git log -1 --format=%H --grep='^chore: archive observations' -- archive/)
# 讓 archive/ 跟那個 commit 一模一樣（多出來的檔案也會一併移除）
git restore --source="$SHA" --staged --worktree -- archive/
git commit -m "fix: 還原封存檔（ArchiveIntegrityError）"
git push            # 被拒的話先 git pull --rebase 再 push
```

git 太舊、沒有 `git restore` 時，把那一行換成
`git rm -r -q --ignore-unmatch archive/ && git checkout "$SHA" -- archive/`（效果相同，已實測）。

然後照 §2 手動跑一次 **FareHunter Monitor**，變綠勾＝復原。若還是同一個錯，
把 log 的錯誤訊息整段交給 AI 處理（ADR 0002「維運」段落有完整說明）。

**若紅的是 Archive DB 本身**：不急，抓價不受影響，漏跑或紅幾天都沒關係。
- 錯誤是「搬移前後不一致…已 rollback」→ 什麼都沒被改動，隔天會自動重試；
  連續幾天都這樣再找 AI 看。
- 錯誤是「不見了」「manifest 記」或「無法讀取」→ 跟上面同一件事，照上面還原。
- 錯誤是「已存在但不在 manifest 裡」→ `archive/` 裡多了一個沒登記的檔案。
  通常是有人在自己電腦上跑封存跑到一半中斷留下的；**不要自己刪**，交給 AI
  確認裡面是否為空再處理（錯誤訊息裡有判斷方式）。
- 錯誤是 `too many attached databases` → 封存檔已經滿 10 個（ADR 0002 預估
  約 2–3 年後）。抓價不受影響，但 `prices.db` 會重新開始變大；找 AI 依
  ADR 0002「已知限制」提案。
- 紅在「Commit」且 log 有 `NON-FAST-FORWARD` → 封存期間有人（或合併 PR）
  另外推了 commit。沒有任何資料被改，隔天會自動重做。
- 紅在「Run tests」→ 跟 Monitor 的測試是同一套，Monitor 也會紅，照 §0 處理。

## 3c. fli Observe 紅燈時（觀察期，2026-10-26 起）

**先記住：這支壞了不影響任何東西。** 抓價、LINE 警報、首頁看板都不讀它的資料
（`data/fli/`），所以不用急。它是在試一個免費的 Google 實價來源（ADR 0003）。

紅的是 **observe** 這個 job 的最後一步（「Fail if the observation run failed」）時：
點進「Observe fli calendar + shadow comparison」，看**最後一行**那串 JSON 摘要：
- `"blocked"` 大於 0，或 `"stopped"` 開頭是 `blocked` → Google 擋了 GitHub 的 IP。
  偶發一次觀察就好；連續兩天都這樣，到 Actions → fli Observe → 右上 ⋯ →
  **Disable workflow** 停用，再找 AI。
- `"errors"` 幾乎都是 `google_error` 或 `fli`、而且 `"calendar_ok": 0` → Google 或 fli
  改版了。同樣先停用、再找 AI。
- `"problems"` 有 `shadow_targets` → 讀不到 prices.db；通常跟 Monitor 那邊的問題是同
  一件事，先照 §0 看 Monitor。
- 沒有摘要、log 停在半途 → 程式出錯或逾時；把 log 交給 AI。

其他紅法：
- 紅在「Install dependencies」→ fli 的套件裝不起來（例如被下架）。Monitor 不受影響，
  先停用、再找 AI。
- 紅在「Run tests」→ 跟 Monitor 同一套測試，Monitor 也會紅，照 §0 處理。
- **commit** 這個 job 紅在「Commit」且 log 有 `NON-FAST-FORWARD` → 跟別的寫入者撞到，
  **這一輪的紀錄就不要了**（下一輪照常），不用處理。

**看觀察結果**：Actions → fli Observe → 點最新一筆 → 頁面最下方「fli 觀察期報告」。
`criteria` 四項都是 PASS，就可以請 AI 提第二階段。日曆那項 FAIL 時看
`calendar_by_route`（每條航線「有價段數／計分段數」），就知道是哪條航線拖累；比價那項
FAIL 時看 `shadow_within_by_provider`／`calendar_vs_ref_by_provider`，分得出是 fli 不準
還是某個參考來源（SerpAPI／Scrape.do）本身有落差。

## 3d. Bright Data（Google 實價，2026-10-31 起）

**先記住：Bright Data 壞了不影響抓價和警報。** 它只是在每小時那一輪多查一些 Google
實價；查不到時警報照常發（標「未驗證」），看板照常更新。

- **看用量**：monitor 的 log 裡有一行「Bright Data 本輪：{…}」，`requests` 是這輪送出幾次、
  `errors` 是失敗幾次（其中 `timeouts` 是等不到回應的）、`recorded` 是寫進幾筆 Google 實價、
  `no_direct` 是頁面正常但那趟沒有可寫入的直飛價、`ok_over_60s` 是等超過 60 秒才成功拿到
  結果的次數（2026-11-09 起等待上限放寬到 120 秒，這個數字大致就是放寬救回來的）。每月
  5,000 次免費、1 日重置，程式會自動配速用到約 4,750 次；用完就停，不會扣款。一輪花在
  Bright Data 上的時間到 10 分鐘就不再送新的請求（所以連續逾時的輪次，`requests` 會比
  `allowance` 少）。
- **`errors` 連好幾輪都等於 `requests`**：多半是 Google 改版、解析不到，或金鑰失效。
  抓價不受影響，有空再找 AI 看。
- **想停用**：到 GitHub repo → Settings → Secrets and variables → Actions，刪掉
  `BRIGHTDATA_API_KEY`。下一輪起就完全不碰 Bright Data。
- **安全**：Bright Data 後台或 API 給的 zone 密碼、金鑰，都不要貼進對話、文件或 commit。

## 3d-1. 巡檢低價警報（2026-11-16 起；目前開 KHH-CTS／FUK／OKA／NGO／NRT）

巡檢查到的 Google 直飛實價低於門檻，也會發「已驗證」警報。log 會出現「巡檢警報：KHH→OKA …」，
monitor 的 summary 有 `sweep_alerts`（這輪發了幾則）。

- 只有 `config.yaml` 裡寫了 `sweep_alerts: true` 的航線會發。想關掉就刪掉那一行或改成 `false`。
- 想多開一條線，先確認那條線的門檻是用實價重算過的，不然可能一次跳出好幾則。
- **傳統航空特價警報**（2026-11-22 寫好、預設關）：巡檢查到的傳統航空直飛實價，比該線近 14 天傳統航空
  最低價再便宜 5% 以上才發（近 14 天不到 30 筆資料的線不判斷）。不用固定門檻，門檻跟著季節自己移動。
  開關在 `config.yaml` 的 `defaults.full_alerts`；觸發那一行寫「傳統航空 X ≤ 近 14 天最低 Y 再低 5%」。

## 3d-2. ChocoData（冷門線的 Google 實價，2026-11-13 起）

**先記住：ChocoData 壞了或用完都不影響抓價、警報、看板。** 它只在每小時那一輪替四條冷門線
（KHH-CTS、KHH-NGO、KHH-FUK、KHH-OKA）多抓 1–2 頁 Google Flights，抓的是跟 Bright Data
同一個頁面、同一種價格。

- **額度是一次性的**：免費方案註冊時送 5,000 點、每頁扣 5，約 1,000 頁，**不會每月重置**。
  程式每天最多 30 頁、累計 950 頁就停，大約一個月用完。用完後 log 那行的 `allowance` 會是 0，
  這是正常的。
- **看用量**：monitor 的 log 有一行「ChocoData 本輪：{…}」，欄位意思跟 Bright Data 那行相同；
  `exhausted: true` 表示 ChocoData 回「點數不足」，本輪已停。後台（chocodata.com → Billing）
  的 Credit balance 是實際剩餘點數。
- **12/1 順便看一眼後台**：如果點數回到 5,000（其實每月重置），請 AI 把 `TOTAL_MAX` 調高即可。
- **想停用**：刪掉 repo secret `CHOCODATA_API_KEY`，下一輪起就完全不碰 ChocoData。
- **安全**：ChocoData 的金鑰是放在網址裡送出的，程式已經不印網址；金鑰一樣不要貼進對話、文件或 commit。

## 3d-2b. ScrapingAnt（冷門線的 Google 實價，2026-11-22 起，每月重置）

跟 ChocoData 同一套冷門線巡檢，價格跟 Bright Data 一樣是 Google 直飛實價。免費方案每月 10,000 點、
每頁 10 點（約 1,000 頁），週期從註冊日起算（這個帳號是每月 22 日）。ChocoData 約 12 月中用完後由它接手。

- **看用量**：monitor 的 log 有一行「ScrapingAnt 本輪：{…}」；看板最下面的用量面板有一列。程式每輪先查剩餘
  點數，把剩下的平均分到重置日，每輪最多 2 頁（每頁 6–20 秒）。
- **壞了不影響任何事**：只是少了這份實價。想停用就刪掉 repo secret `SCRAPINGANT_API_KEY`。

## 3d-3. google-flights2（警報的第二驗證來源，2026-11-14 起）

Bright Data 驗證不到某一則警報時（頁面上沒有那家航空、逾時、額度用完），發出前會再用
google-flights2（RapidAPI，每月 150 次免費）查一次。查到同一家航空的直飛價就照實價判斷：夠便宜→
以「已驗證」廣播；不便宜→不發。查不到→照舊以「未驗證」只發給你。

- **看用量**：monitor 的 log 有一行「google-flights2 本輪：{…}」。程式每天最多 6 次、30 天最多 130 次。
- **Picker 線索**（同一天起）：每週替 KHH-OKA／NGO／FUK 抓 Google 日曆的每日最低價，告訴巡檢先查哪
  幾天。這個價格含轉機，**只當線索**：不顯示、不發警報、不寫進價格資料；巡檢查到直飛實價才算數。
  log 那行是「google-flights2 Picker 本輪：{…}」，`skipped: fresh` 表示這週都抓過了、`quota` 表示
  額度要留給警報驗證。每月約用 26 次。
- **壞了不影響任何事**：只是少了第二次驗證。想停用就刪掉 repo secret `RAPIDAPI_KEY`。

## 3d-4. 虎航官網日曆（收集中、巡檢線索，2026-11-21 起）

每輪 monitor 順便抓一次虎航官網的每日單程最低價（桃園、高雄 2 個請求，不用金鑰）。這個價格**單程、
未稅**，不是來回實價：不顯示、不發警報、不寫進價格資料。

- **收集**：價格有變才記一筆到 `tigerair_fares` 表，先收 1–2 週看特賣怎麼出現，再決定要不要做特賣提醒。
  目前**不發任何通知**。只記未來 120 天內出發的日子，變化紀錄只留 14 天，表約停在 3 MB 內。
- **巡檢線索**：官網去回加總最便宜的日子，Bright Data／ChocoData 會先去查 Google 直飛實價；查到才算數。
- **看狀況**：monitor 的 log 有一行「虎航官網日曆本輪：{…}」。`ok: 2` 是兩個機場都抓到；`written` 是這輪
  記了幾筆變化；`hint_routes` 是幾條線有線索。Bright Data 那行的 `tiger_hinted` 是排了幾個虎航線索。
- **壞了不影響任何事**：log 會出現「虎航官網日曆 TPE：HTTP 403」之類的警告，只是少了線索，巡檢照舊。
  一直壞的話（虎航改版或擋掉），請 AI 看一下；要停用得改 `runner.py` 那一段。

## 3e. LINE 通知發給誰、什麼時候響（2026-11-10 起）

**先記住：你永遠收得到每一則。** 朋友收到幾則，由 LINE 免費額度決定。

- **你的通知走 Telegram**（2026-11-21 起，bot：@Flightpkbot）：每則警報與便宜日推播都先發
  Telegram。原本 LINE「只發給你」的那一則，Telegram 送到了就不再用 LINE 發——LINE 每則扣 1 則
  額度，省下來給朋友的廣播。Telegram 送不出去時自動退回 LINE 發給你，不會漏。log 會出現
  「擁有者已經從 Telegram 收到這則，不再用 LINE 發」。要停用 Telegram：刪掉 repo secret
  `TELEGRAM_BOT_TOKEN`，一切回到只用 LINE。
- **每日行情摘要**（2026-11-21 起，只走 Telegram）：每天台灣時間早上 8 點那輪，發一則各線近 48 小時
  的最低 Google 直飛實價（住 2–10 晚）、跟上週比漲跌、低於門檻的標 ✅，另列昨天警報數與 LINE 剩餘。
  那輪沒送成會在下一輪補，最晚到中午；一天只發一次（prices.db 的 `digest_sends`）。只列 Google 實價，
  不列快取估價與虎航官網價。log 會出現「每日摘要已發到 Telegram」。
  2026-11-22 起另列「傳統航空最低」（華航、長榮、星宇等的直飛實價，沒有傳統航空直飛的 CTS／NGO 不列）。

- **廣播與額度**：`LINE_BROADCAST=1` 時，通知會廣播給官方帳號的所有好友。每位收件人算一則；
  免費方案每月約 200 則，5 個人時一次廣播就用掉 5 則。
- **朋友只收驗證過的**（2026-11-14 起）：只有經 Google 驗證的警報才廣播；「疑似低價・未驗證」和
  「價格有落差」的只發給你。11/1–11/13 的 10 則未驗證警報有 3 則事後看是錯的，朋友收到錯價會
  不再相信警報。log 會出現「這則警報沒有經 Google 驗證，只發給擁有者、不廣播」。
- **配速**：為了讓朋友整個月都收得到，而不是前 20 天用完、月底完全沒有：
  - 台灣日期每天的**第一則**照常廣播。
  - 同一天的**第二則以後**，只有額度還夠「之後每天一則」時才廣播，否則只發給你。前面沒用完的
    額度，會留給後面通知比較多的日子。
  - 本月剩不到保留額度時，一律只發給你：有設 Telegram 時保留 10 則（只當 Telegram 壞掉時的備援），
    沒設時保留 30 則。
  - log 會出現「LINE 配速：…這則只發給擁有者」，這是正常的，不是故障。
- **朋友人數**：程式讀 LINE 前一天的好友統計來算一次廣播用掉幾則；好友少時 LINE 不提供，
  就用 workflow 的 `LINE_RECIPIENTS`（沒設時當 5 人）。朋友人數變了，可以順手改這個值。
- **半夜靜音**：台灣時間 23:00–07:00 送出的通知改成靜音送達——訊息照常到，手機不響。
- 發送紀錄在 prices.db 的 `line_sends`（每則一筆：時間、廣播或只發給你）。

## 3f. 看各服務本月用了多少（2026-11-11 起）

看板最下面有一塊「本月用量（管理用）」，點開就看得到，不用翻 log：

- **Bright Data**：本月送出幾次／5,000 次，其中成功與逾時各幾次。程式會配速用到約 4,750 次。
- **ChocoData**：累計用了幾頁／950 頁（一次性額度，跨月累計、用完不會補）、今天幾頁、照目前速度
  大約幾天後用完。
- **google-flights2**：近 30 天程式用了幾次／130 次（只在警報驗證不到時才用，常常是 0；手動探測
  不算在內，實際剩餘以 RapidAPI 後台為準）。
- **SerpAPI**：還剩幾次／250 次，以及預計哪天重置（數字來自每日 FSC 快照）。
- **Scrape.do**：本月用了幾點／1,000 點。便宜日已經有 Google 實價時不會再查，所以常常是 0。
- **LINE**：本月廣播幾次、只發給你幾次、今天有沒有廣播過，以及最近一次發送時大約還剩幾則。
  本月還沒發過通知時，剩餘則數會顯示「—」。

數字每小時跟著看板一起更新；哪一項讀不到就不顯示，不會塞假數字。

## 3g. 連假日曆每年更新一次（2026-11-13 起）

看板的「請假 N 天」「🎌連假」標籤和「接下來的連假」清單，都讀 `data/tw_holidays.json`。
目前收 2026–2027 年。**資料沒有涵蓋的年份不顯示標籤**，不會猜，也不會影響價格與通知。

- **什麼時候更新**：人事行政總處每年約 6 月公布隔年的「政府行政機關辦公日曆表」。公布後請 AI
  更新這個檔案（例如：「把 2028 年的辦公日曆加進 data/tw_holidays.json」）。
- **資料來源**：政府資料開放平台資料集 14718；github.com/ruyut/TaiwanCalendar 有整理好的 JSON
  （`data/{年份}.json`），檔案開頭的 `_說明` 記著用的是哪一版。
- **格式**：`off` 列有名稱的放假日（含落在週末的節日）和平日放假；`workdays` 列週末補班日；其餘
  週六日照常放假。2026、2027 年沒有補班日。
- 臨時加放的颱風假不算（那是各縣市當天決定的）。

## 4. 設定卡：cron-job.org 外部排程（讓更新不再依賴 GitHub 排程器）

> ✅ **狀態：已於 2026-07-07 完成上線**（每小時 `:17`，時區 Asia/Taipei，
> 任務名 FareHunter Monitor）。以下步驟保留，供重建、換帳號或換 token 時照做。

**什麼時候需要**：GitHub 排程命中率持續很差（首頁常掛黃/紅條）。2026-07 實測
命中率一度只有約 7%，因此強烈建議設定。

**步驟 A — 開 GitHub token（2 分鐘）**
1. github.com/settings/personal-access-tokens → Generate new token（Fine-grained）
2. Repository access：**Only select repositories** → 只勾 `HookPKC/farehunter`
3. Permissions → Repository permissions：**只開 Actions: Read and write**
   （Metadata read-only 會自動附帶。**絕對不要開 Contents 或 Workflows**——
   外部服務只需要「按啟動鈕」的權力，不需要「改程式」的權力）
4. Expiration：依你的安全偏好選擇（越短越安全，但要記得換）。**實際到期日
   一律以 GitHub token 頁面顯示為準**——目前線上這顆依設定可能至 2027-07，
   請自行到 github.com/settings/personal-access-tokens 確認。⚠ 不論效期多長，
   立刻在手機行事曆建「到期前 2～4 週」的提醒：「換 FareHunter cron-job
   token」——到期沒換，外部觸發會悄悄失效（cron-job.org 會寄失敗通知、
   healthchecks 超過 grace 也會通知，這兩層是最後防線）。
5. Generate → 複製 token（只顯示一次）。

**步驟 B — 建 cron-job.org 任務（3 分鐘）**
1. cron-job.org 註冊免費帳號（建議在 Settings 開啟 MFA）
2. Create cronjob，逐欄填：
   - **URL**：`https://api.github.com/repos/HookPKC/farehunter/actions/workflows/monitor.yml/dispatches`
   - **Method**（在 Advanced）：`POST`
   - **Headers** 三條：
     - `Authorization` → `Bearer 貼上你的token`
     - `Accept` → `application/vnd.github+json`
     - `Content-Type` → `application/json`
   - **Body**：`{"ref":"main"}`
   - **Schedule**：Every hour at minute **17**（與 GitHub 自己的 :07 錯開；
     同一小時兩邊都跑到也沒關係——guard 會自動跳過重複那次）
   - **Notifications**：開啟 job 失敗通知（email）
3. 按 **Test run** → 應顯示成功（HTTP 204）→ 到 GitHub Actions 應看到
   一筆新的 FareHunter Monitor run。看到就完成了。

**回復方式**：cron-job.org 停用/刪除該 job＋到 GitHub 撤銷 token，系統即回到
純 GitHub 排程狀態，零殘留。

**替代路徑**（若不想把 token 存在第三方）：Google Apps Script 建時間觸發器，
token 存自己 Google 帳號的 Script Properties，用 UrlFetchApp 發同一個 POST。
可靠度同級，但要自己維護一小段程式且沒有現成失敗通知——一般情況不需要。

## 5. 設定卡：healthchecks.io（資料停更時主動 email 你）

1. healthchecks.io 註冊免費帳號 → Add Check，名稱如 `farehunter-monitor`
2. **Schedule**：Period = 1 hour、**Grace = 3 hours**（與首頁紅色門檻一致：
   超過 3 小時沒心跳才通知，避免正常延遲誤報）
3. 複製該 check 的 **Ping URL**（`https://hc-ping.com/…`）
4. GitHub repo → Settings → Secrets and variables → Actions →
   **New repository secret**：名稱 `HC_PING_URL`、值貼 Ping URL
5. 完成。monitor 每次成功（含 guard 跳過）會自動 ping；超過 grace 沒 ping
   到，healthchecks 會 email 你。**secret 沒設之前這功能自動休眠，不影響任何東西。**

**設定後怎麼確認真的通了（一次性，5 分鐘）**
1. 等下一班 `:17` 過後，到 Actions 點開最新的 FareHunter Monitor run，看最後的
   Heartbeat 步驟：log 從「HC_PING_URL 未設定，跳過心跳」變成 curl 執行＝GitHub
   端 OK。
2. 回 healthchecks.io 儀表板：該 check 轉綠、**Last Ping** 顯示幾分鐘前＝接收端
   OK。兩邊都對上就是閉環完成。
3. 順手確認 check 設定：**Period = 1 hour、Grace = 3 hours**。若 Grace 留在預設
   （例如 1 day），偵測會鈍化到失去意義。

**收到 missed-ping email 時的三步判讀**
1. 開首頁看「資料整理於」：若其實很新（<90 分），多半是 healthchecks 端設定或
   單次網路抖動，觀察下一班即可。
2. 真的舊了 → Actions 看 FareHunter Monitor 最近的 run：**零 run**＝觸發層問題
   （查 cron-job.org 儀表板該 job 的執行歷史與 token 是否過期）；**有 run 但紅**＝
   執行層問題（點進去看第一個紅色步驟；「Run monitor」紅且 log 提 401/token
   ＝ Travelpayouts token 過期，去 Secrets 更新 `TRAVELPAYOUTS_TOKEN`；提
   `ReadTimeout`／`ConnectionError` ＝ Travelpayouts 端故障，等它恢復；log 是
   `ArchiveIntegrityError` ＝照 §3b）；
   **run 綠但首頁沒變**＝ deploy 問題（照 §3 Re-run）。
3. 照 §0 三步手動補一次資料，恢復後再回頭追原因。

**收到「DOWN」信但首頁資料很新：自我檢查發現 Google 實價出問題（2026-11-24 起）**

monitor 每輪 commit 之後會做一次自我檢查（`health.self_check`，只讀 prices.db），有問題時心跳改回報
`/fail` 並附上原因，healthchecks 立刻寄「DOWN」信；問題解除後下一次正常回報會寄「UP」信。中間不會
重複寄。原因在 healthchecks 該 check 的 **Events／最後一次回報內容**，Actions 裡 Heartbeat 步驟的
log 也印同樣的文字（「自我檢查發現問題」）。三種問題：

| 問題文字 | 意思 | 怎麼處理 |
|---|---|---|
| 全部 N 條航線已 X 小時沒有新的 Google 實價：… | Google 實價全面中斷（平常最久空 1.8 小時，門檻 3 小時）。警報會變成「尚未經 Google 驗證」、朋友收不到 | 看冒號後面的原因：**解析不到航班（Google 可能改版）**＝要修 `brightdata_flights.parse`，三家共用，找 AI；**都沒拿到頁面**＝金鑰過期或額度用完，看 Bright Data 後台與 `BRIGHTDATA_API_KEY`；**沒有送出任何查詢**＝額度配速歸零或巡檢被跳過，看 monitor log 的「Bright Data 本輪」 |
| XXX-YYY 已 X 小時沒有新的 Google 實價：… | 只有這條線斷了（11/13 起最久 KHH-CTS 17 小時；冷門線查得少時 KHH-NGO 曾到 25 小時，門檻 36 小時） | 「沒有直飛價」多半是這段日子沒班機，觀察即可；「解析不到」「沒拿到頁面」照上一列 |
| 通知連續 2 輪送不出去 | 警報觸發了但 Telegram、LINE 都沒送到（會一直重試） | 看 Run monitor log 的「發送失敗」；Telegram 檢查 `TELEGRAM_BOT_TOKEN`，LINE 檢查 `LINE_CHANNEL_ACCESS_TOKEN` 與本月額度 |

自我檢查程式本身出錯時也會回報 `/fail`（「自我檢查程式出錯」），避免壞掉的檢查一直說正常。
**快取價（Aviasales）停更不在這裡**：整輪抓不到時 monitor 會變紅（上面的 missed-ping），個別航線
停更看首頁的黃／紅提示條。

## 6. 安全守則（重要）

- **不要**把任何 token 貼進與 AI 的對話（真的需要時，用完立刻刪）
- cron-job.org 的 token **只給 Actions 權限**，永遠不給 Contents / Workflows
- 懷疑 token 外洩 → github.com/settings/tokens 立即 **Revoke**，再開新的換上
- token 90 天到期前照行事曆提醒換新（cron-job.org 編輯 job 換 header 即可）

## 7. 沒有 AI 之後的日常維護（每週 1 分鐘）

- 每週開一次首頁：沒提示條＝健康，關掉即可
- （每月一次，可選）Actions → **Archive DB** 最近一次 run →「Archive old
  observations」步驟的最後兩行：現行檔平常在約 37–45 MiB 之間起伏（大約每
  2–3 週 VACUUM 一次，會掉回 37–40 上下）。超過 48 MiB，或封存檔編號到 `observations-008.db`，
  就找 AI 看（原因見 ADR 0002「已知限制」）。2026-11-23 起 Aviasales 快取價改成「沒變就不重寫、每 6 小時
  至少一筆」，monitor log 的 Run summary 有 `unchanged`（這輪跳過幾筆同價）；每天成長應該在約 0.3 MB
- 收到 healthchecks 或 cron-job.org email → 照第 0 節三步走
- 三步走了還是不行 → `docs/PLAYBOOK.md` 案例 1-6 有完整排查法
  （順序：Scheduled run 是否存在 → workflow state → 手動 dispatch 是否正常）

## 8. 親友問：「點進 Google Flights，價格怎麼跟你網站不一樣？」

可以直接這樣回（複製貼上也行）：

> 這個網站顯示的是「**最近觀測到的低價**」，不是即時報價——有的價每小時
> 更新、有的一天或一週才核對一次。點進 Google Flights 看到的是**當下即時
> 搜尋**的結果，票價本來就隨時在變，所以兩邊常常不同：有時 Google 更便宜
> （趕快訂！），有時變貴了（代表低價已經消失）。網站的用途是**提示你
> 「這條航線最近出現過什麼好價、值不值得現在去查」**，最終價格永遠以
> Google Flights 點進去看到的為準。

這不是故障，不用回報、不用修。頁面上的「≈」「約」「近期觀測」「以 Google
Flights 為準」字樣就是在標示這件事。

## 9. 系統防護總覽（給未來接手者的地圖）

| 層 | 元件 | 守什麼 |
|---|---|---|
| 觸發 | GitHub schedule `:07` ＋（建議）cron-job.org `:17` | 兩個獨立失效域互為備援 |
| 免疫 | 55 分鐘防重複 guard（fail-open） | 多觸發源不重複燒 API；guard 壞掉時寧可重複、絕不停擺 |
| 偵測（使用者） | 首頁黃/紅新鮮度提示 | 任何人都能看出資料是舊的 |
| 偵測（Hook） | healthchecks 心跳 ＋ cron-job.org 失敗通知 | 停擺 3 小時內 email 到你 |
| 容量 | Archive DB 每日封存（ADR 0002） | `prices.db` 不會撞到 GitHub 100 MiB 單檔上限 |
| 復原 | 本 RUNBOOK 第 0 節三步 | 人類 30 秒可執行 |
