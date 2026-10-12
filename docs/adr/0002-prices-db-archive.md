# ADR 0002：prices.db 依出發日封存到 archive/

- 狀態：**已採用**
- 日期：2026-10-12
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
小時版本的 pack 總共只有 8.25 MiB。卡住的只有「單一檔案大小」。

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
   群組。**不在每小時抓價的路徑上**：封存壞了，抓價照跑。
4. 搬移在單一交易內完成，提交前比對全歷史指紋（每航線×艙等的筆數、最低、
   最高、價格總和、id 總和，加總筆數與相異 id 數），不同就 rollback。
5. 只有第一次封存後 VACUUM；之後刪掉的頁由新觀測重複使用，檔案停在穩定
   大小，也避免每天整檔重寫讓 git 差異變大。
6. 封存檔缺檔或筆數與 manifest 不符時，讀者一律拋 `ArchiveIntegrityError`，
   **不默默少算**——全歷史變小會讓 new_low 誤發。（使用者 2026-10-12 決定：
   寧可 monitor 變紅、healthchecks 寄信，也不要只停用 new_low 繼續跑。）

**為什麼依出發日而不是觀測時間**：觀測一定早於出發日，所以出發日已過 30 天的
列，觀測時間也一定早於 30 天前——兩種視窗都碰不到。以觀測時間切會切到仍在
未來的出發日，big_drop 的單日基準就少了資料。

## 驗證（2026-10-12，真實 prices.db 副本）

- 現行檔 44.9 → **34.7 MiB**，封存檔 9.1 MiB（69,026 筆，出發日 < 2026-09-12）
- 封存前後：`data.json` 完全相同；每航線 `route_stats`、未來出發日的
  `route_stats_by_date` 完全相同；`ranked.json` 在凍結時鐘下（`faketime -f`）
  完全相同（不凍結時連同一份檔案跑兩次都不同——`decided_at` 與新鮮度分數吃
  真實時鐘）
- 每輪多花約 1 秒（export 4.6→4.9 s、FIE 0.8→1.3 s、route_stats 0.9→1.3 s）；
  查詢計畫在現行檔與封存檔上都走 idx_obs_route
- 重跑一次：搬 0 筆，兩個檔案逐位元不變（不會產生空 commit）
- 穩態估計：近 30 天觀測的出發日平均在 80 天後，現行檔約維持在
  「每日觀測 × (80 + 30) 天」≈ 30–35 MiB；封存檔每年約 +100 MiB（約 2.5 個新檔）

## 不採用的方案

- **壓縮後放 git**：gzip 後只剩 5.2 MiB，但壓縮檔每次整檔都不同，git 無法存
  差異——每小時 +5 MiB 歷史，一個月約 3.7 GiB（對比現在 50 版共 8.25 MiB）。
- **移出 git 改放 GitHub Release**：沒有 100 MiB 問題，但失去「每小時一版、
  可回溯」的備份與 NON-FAST-FORWARD 防護，並新增「下載失敗 → 程式建空 DB →
  覆蓋上傳 → 歷史全毀」的風險。改動面最大。
- **Git LFS**：LFS 不存差異，每小時一份完整 45 MiB，一天約 1 GiB。
- **刪舊資料／統計只看近 N 個月**：最簡單，但改變 new_low「航線史上最低」與
  首頁統計的語意。若未來真的想改語意，另開提案。

## 維運

**封存檔不見或筆數不符**（log 出現 `ArchiveIntegrityError`，monitor 變紅）：
不要重跑封存、不要手改 manifest。從 git 歷史把封存檔還原到「最後一個正常
版本」：`git log --oneline -- archive/<檔名>` 找出異動紀錄，用
`git checkout <正常的那筆> -- archive/<檔名>` 還原、commit、push。還原後的筆數
必須等於 manifest 記的筆數（錯誤訊息裡兩個數字都有）。

**本機查全部歷史**：直接打開 prices.db 只看得到近幾個月。Python 用
`archive.attach(conn)` 後查 `observations_all`；sqlite3 CLI 用
`ATTACH 'archive/observations-001.db' AS a1;` 再自己 UNION ALL。

**新增 observations 欄位（migration）**：不用動封存檔。舊封存列在
`observations_all` 讀出 NULL（與 main 裡 migration 前的舊列一致），下次封存
會把欄位補進目前的封存檔。

## 已知限制與重新評估時機

- SQLite 一次最多 ATTACH 10 個資料庫。照每年約 2.5 個新檔，約 3–4 年後到頂。
  **封存檔到第 8 個時**開始規劃（例如把已凍結的舊檔合併，或預先算好舊檔的
  統計）——屬大改，需提案。
- 若監控航線大增、每日觀測量翻倍，現行檔穩態會跟著變大；看 archive-db 每次
  log 的「現行檔 … MiB」，超過 45 MiB 就該重新評估 `MARGIN_DAYS` 或做法。
