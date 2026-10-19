# ADR 0002：prices.db 依出發日封存到 archive/

- 狀態：**已採用**
- 日期：2026-10-12（2026-10-19 全面複查後修正：VACUUM 策略、欄位 DEFAULT、
  開檔即核對、回退限制；見「複查」）
- 實作：`src/farehunter/archive.py`、`.github/workflows/archive-db.yml`、
  `tests/test_archive.py`

## 背景

prices.db 每小時隨 commit 進 repo，實測大小：

| 日期 | 大小 |
|---|---|
| 2026-08-12 | 16.0 MiB |
| 2026-09-12 | 34.3 MiB |
| 2026-10-12 | 44.9 MiB |

GitHub 對單一檔案：超過 50 MiB 只出警告，**超過 100 MiB 拒收**。拒收那天，
monitor / fsc / verify 三個 writer 的 commit 全部失敗，整條抓價停擺。照
9→10 月的速度（每月約 +11 MB）約 2027-03 撞到；若回到 8 月的速度，提早到 2027-01。

**git 歷史不是問題**：SQLite 原檔版本間的差異很小，2026-10-12 量到最近 50 個
小時版本的 pack 總共只有 8.25 MiB（真實 repo 一週約 +4 MiB）。卡住的只有
「單一檔案大小」。封存之後的歷史成本另外實測過，見「複查」：反而比不封存小。

檔案組成：observations 表 77%、idx_obs_route 索引 22.5%；其中 39% 的列
（130,872 筆）出發日已經過了。

## 誰需要讀「出發日已過」的列

逐一盤點所有讀 observations 的查詢，只有下面這些是「全歷史」：

- `storage.route_stats` — new_low 的「航線史上最低」
- `export_web` — 首頁統計（n/min/avg/median）、價格趨勢圖、總筆數、航線清單
- `intelligence._route_context` — FIE 的歷史錨點（min/median/n）
- `health.last_observed_at` — 航線最後一次觀測時間

其餘查詢都只看**未來出發日**（日曆、月度、FIE `_SELECT`、big_drop 的單日基準）
或**近期觀測**（guard、cooldown、cheap_days、carriers 參考）。

## 決定

1. **出發日早於（今天 − 30 天）的觀測**搬到 `archive/observations-NNN.db`
   （同在 git 裡），每檔滿 40 MiB 換新檔。每個封存檔的筆數記在 prices.db 的
   `archive_manifest`。
2. 上面四處全歷史查詢改讀 TEMP VIEW `observations_all`（＝現行 ∪ 封存），
   由 `archive.attach(conn)` 建立。其餘查詢**一個字都沒改**。
3. 搬移由獨立的 `archive-db.yml` 每日執行，與 monitor 同一個 concurrency
   群組。**不在每小時抓價的路徑上**：這支 job 失敗（rollback、不 commit）時
   抓價照跑。（repo 裡的 `archive/` 不完整則是另一回事，見決定 6。）
4. 搬移在單一交易內完成，提交前比對全歷史指紋（每航線×艙等的筆數、最低、
   最高、價格總和、id 總和，加總筆數與相異 id 數），不同就 rollback。
5. **VACUUM：第一次封存一律做；之後只在現行檔比壓實後大 10% 以上時做**
   （`VACUUM_WASTE_RATIO`，壓實大小用 `VACUUM INTO` 暫存檔量）。原本寫的
   「之後刪掉的頁由新觀測重複使用、檔案停在穩定大小」是錯的：id 是
   AUTOINCREMENT，新列永遠接在表的最右邊，被搬走的舊列散在各頁，騰出的頁內
   空間幾乎不會被重用——不再 VACUUM 的話，現行檔仍以原速的約 93% 成長。
6. 封存檔缺檔或筆數與 manifest 不符時，讀者一律拋 `ArchiveIntegrityError`，
   **不默默少算**——全歷史變小會讓 new_low 誤發。（使用者 2026-10-12 決定：
   寧可 monitor 變紅、healthchecks 寄信，也不要只停用 new_low 繼續跑。）
   `Store()` 一開檔就核對：monitor／FSC／Verify／cheap_day_notify 都在打任何
   付費 API 之前建 Store，所以出錯時不會先花掉額度、最後才在 export 失敗把
   結果丟掉。只有 health 是 fail-open（它只是報告，且呼叫端都已核對過）。
7. 封存檔缺少的欄位（封存之後 main 才 migration 新增的）在 `observations_all`
   補 **main 的 DEFAULT**，沒有 DEFAULT 才補 NULL；下次封存時把欄位連同
   DEFAULT／NOT NULL 補進封存檔。這跟 main 裡 migration 前的舊列讀出來一致——
   fare_class 當年就是 `ADD COLUMN ... DEFAULT 'any'`，全歷史查詢都濾它，
   補 NULL 會讓封存列悄悄掉出統計，筆數卻照樣對得上。

**為什麼依出發日而不是觀測時間**：觀測一定早於出發日，所以出發日已過 30 天的
列，觀測時間也一定早於 30 天前——兩種視窗都碰不到。以觀測時間切會切到仍在
未來的出發日，big_drop 的單日基準就少了資料。

## 驗證（2026-10-12，真實 prices.db 副本；穩態數字已由「複查」更正）

- 現行檔 44.9 → **34.7 MiB**，封存檔 9.1 MiB（69,026 筆，出發日 < 2026-09-12）
- 封存前後：`data.json` 完全相同；每航線 `route_stats`、未來出發日的
  `route_stats_by_date` 完全相同；`ranked.json` 在凍結時鐘下（`faketime -f`）
  完全相同（不凍結時連同一份檔案跑兩次都不同——`decided_at` 與新鮮度分數吃
  真實時鐘）
- 每輪多花約 1 秒（export 4.6→4.9 s、FIE 0.8→1.3 s、route_stats 0.9→1.3 s）；
  查詢計畫在現行檔與封存檔上都走 idx_obs_route
- 重跑一次：搬 0 筆，兩個檔案逐位元不變（不會產生空 commit）
- ~~穩態估計 30–35 MiB~~：前提（不 VACUUM 也不長）是錯的，見「複查」

## 複查（2026-10-19，八個面向獨立驗證＋真實資料重播）

- **輸出等價（真實 main 副本，356,183 筆）**：凍結時鐘下 21 項輸出封存前後
  完全相同（export／`data.json` 逐位元、FIE、8 航線 route_stats、未來出發日
  route_stats_by_date、FSC／Verify 選樣、alert 判斷……），Python 3.12 與 3.13
  皆同；之後再做 3 次增量封存、延後到 11-08／12-03 評估也相同。runner.run
  端到端（假 API）通知與寫入也相同。**反證**：用 PR 之前只讀現行表的程式讀
  封存後的檔，new_low 從 154 則變成 624 則——實驗確實抓得到退化，也正是
  「不能單純 revert」的證據。
- **現行檔大小（真實資料逐日重播，每天封存一次）**：

  | 做法 | 30 天後現行檔 | 備註 |
  |---|---|---|
  | 只有第一次 VACUUM（原設計） | 41.1 MiB，壓實後 35.0 | 每天仍長約 0.28 MiB |
  | 超過 10% 才 VACUUM（現行） | 35–38.4 MiB | 約 18–20 天 VACUUM 一次 |

  100 天重播（07-10 起）最大 37.8 MiB。壓實大小會隨每日觀測量慢慢變動
  （目前約 35 MiB；以 Little's law「每日觀測 × (平均提前天數 + 31)」估，
  觀測量回到每天 3,000 筆時約 40 MiB）。
- **git 歷史成本（真實資料 10-12 起逐小時重播 3 天、每小時 commit，四種做法
  用同一量法互相比較）**：不封存每天 +5.45 MiB；封存（不 VACUUM 的日子）每天 +3.9 MiB；
  每次 VACUUM 當天約多 +1.1–1.5 MiB（每天都 VACUUM 的極端情況也只回到不封存
  的水準）。量法是本機 git 的預設 delta，GitHub 實際存得更省（真實 repo 一週
  約 +4 MiB、實測每日封存 commit 的 push pack 約 0.15 MiB），但相對大小成立：
  **封存讓歷史長得更慢**。一開始用合成資料（每小時重複插同一批鍵）量到
  VACUUM 後每小時 +0.6 MiB，是測試資料不真實造成的假象，真實資料重播沒有出現。
- **封存檔增長**：重播中每天約 +0.27 MiB（約 100 MiB／年）；以近 7 天的觀測量
  （約 2,900 筆／天、每列約 148 B）算是 130–150 MiB／年，也就是每年約 2.5–3.7
  個新檔。
- **其他**：突變測試 20 種退化全部被測試抓到；RUNBOOK §3b 的還原指令在
  「刪除／改名多出一檔／內容被改」三種情境實測可還原；MARGIN_DAYS=30 是下限
  （export 的 route_common 回看 30 天觀測，margin 29 就會少列）。

## 不採用的方案

- **壓縮後放 git**：gzip 後只剩 5.2 MiB，但壓縮檔每次整檔都不同，git 無法存
  差異——每小時 +5 MiB 歷史，一個月約 3.7 GiB（對比現在 50 版共 8.25 MiB）。
- **移出 git 改放 GitHub Release**：沒有 100 MiB 問題，但失去「每小時一版、
  可回溯」的備份與 NON-FAST-FORWARD 防護，並新增「下載失敗 → 程式建空 DB →
  覆蓋上傳 → 歷史全毀」的風險。改動面最大。
- **Git LFS**：LFS 不存差異，每小時一份完整 45 MiB，一天約 1 GiB。
- **刪舊資料／統計只看近 N 個月**：最簡單，但改變 new_low「航線史上最低」與
  首頁統計的語意。若未來真的想改語意，另開提案。
- **封存表不用 id 當主鍵（讓附加永遠落在表的最右邊）**：實測對 git 歷史沒有
  幫助（每天 +0.04 MiB，反而略多），不值得多一套建表邏輯。
- **每次封存都 VACUUM**：檔案最小，但每次都整檔重寫；超過 10% 才做，換來
  每年少二十幾次重寫，檔案只多 3–4 MiB。

## 維運

**封存檔不見或筆數不符**（log 出現 `ArchiveIntegrityError`，monitor 變紅）：
不要重跑封存、不要手改 manifest。只有 archive-db 會寫 `archive/`，而 manifest
也只在封存時改變，所以把整個 `archive/` 還原成最後一筆
`chore: archive observations` commit 的版本（`git restore --source=… --staged
--worktree -- archive/`，連多出來的檔案一起移除），筆數就一定對得上 manifest。
逐步指令（已實測）見 `docs/RUNBOOK_HOOK.md` §3b。

**「已存在但不在 manifest 裡」**：`archive/` 裡有個沒登記的檔，通常是本機跑
封存時被硬中斷（SIGKILL、斷電）留下的空檔——Python 例外會自動清掉，硬中斷
不會。打開看 observations 是 0 筆或沒有這張表就可以刪；有資料就不要刪，先
查清楚來源。新檔一律用 O_EXCL 獨佔建立，失敗時只刪自己建的，所以兩個封存
同時跑也不會刪到對方的檔（CI 本來就靠 concurrency 群組排隊，不會同時跑）。

**本機查全部歷史**：直接打開 prices.db 只看得到近幾個月。Python 用
`archive.attach(conn)` 後查 `observations_all`；sqlite3 CLI 用
`ATTACH 'archive/observations-001.db' AS a1;` 再自己 UNION ALL。

**新增 observations 欄位（migration）**：ADD COLUMN（有沒有 DEFAULT 都可以）
不用動封存檔：舊封存列在 `observations_all` 讀出 main 的 DEFAULT（沒有就是
NULL），與 main 裡 migration 前的舊列一致（決定 7）；下次封存會把欄位補進
目前的封存檔。**改名或刪欄位不行**：view 依欄位名對齊，封存檔裡舊名字的值
會讀不到——這種 migration 要連封存檔一起處理，另開提案。

## 已知限制與重新評估時機

- **不能單純 revert**：封存跑過一次之後，prices.db 只剩近幾個月的資料。把讀者
  改回只讀 `observations`（revert 這組改動，或把某個全歷史查詢改回去）**不會
  報任何錯**，但 new_low 的「史上最低」、首頁統計與趨勢圖會悄悄只算現行檔
  （實測 8 條航線有 6 條的史上最低變高、new_low 誤發數倍）。要回退得先把
  `archive/` 併回 prices.db，另開提案。
- SQLite 一次最多 ATTACH 10 個資料庫（Python 內建的 SQLite 編譯上限，不能
  調高）。照每年 2.5–3.7 個新檔，約 2.5–3.5 年後到頂；到頂時是封存那支 job
  在建第 11 個檔時報 `too many attached databases`，讀者仍正常，但現行檔會
  重新開始長。**封存檔到第 8 個時**開始規劃（例如把已凍結的舊檔合併，或預先
  算好舊檔的統計）——屬大改，需提案。
- 一次搬移不拆檔：停擺很久後補跑，那一次的量會全部進同一個檔，可能超過
  `ROLL_BYTES`（上限是現行檔本身的大小，不會到 100 MiB）。
- 若監控航線大增、每日觀測量翻倍，現行檔穩態會跟著變大；看 archive-db 每次
  log 的「現行檔 … MiB」，超過 45 MiB 就該重新評估 `MARGIN_DAYS` 或做法。
