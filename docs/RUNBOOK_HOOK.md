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

Pages 偶爾一次性抽風（歷史上發生過，artifact 都建好了最後一步掛掉）。
Actions → **Deploy Pages** → 點進紅色那筆 → 右上 **Re-run all jobs**。重跑幾乎必過。

## 4. 設定卡：cron-job.org 外部排程（讓更新不再依賴 GitHub 排程器）

**什麼時候需要**：GitHub 排程命中率持續很差（首頁常掛黃/紅條）。2026-07 實測
命中率一度只有約 7%，因此強烈建議設定。

**步驟 A — 開 GitHub token（2 分鐘）**
1. github.com/settings/personal-access-tokens → Generate new token（Fine-grained）
2. Repository access：**Only select repositories** → 只勾 `HookPKC/farehunter`
3. Permissions → Repository permissions：**只開 Actions: Read and write**
   （Metadata read-only 會自動附帶。**絕對不要開 Contents 或 Workflows**——
   外部服務只需要「按啟動鈕」的權力，不需要「改程式」的權力）
4. Expiration：**90 days**。⚠ 立刻在手機行事曆建一個 80 天後的提醒：
   「換 FareHunter cron-job token」——到期沒換，外部觸發會悄悄失效
   （cron-job.org 會寄失敗通知，這是最後防線）。
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

## 6. 安全守則（重要）

- **不要**把任何 token 貼進與 AI 的對話（真的需要時，用完立刻刪）
- cron-job.org 的 token **只給 Actions 權限**，永遠不給 Contents / Workflows
- 懷疑 token 外洩 → github.com/settings/tokens 立即 **Revoke**，再開新的換上
- token 90 天到期前照行事曆提醒換新（cron-job.org 編輯 job 換 header 即可）

## 7. 沒有 AI 之後的日常維護（每週 1 分鐘）

- 每週開一次首頁：沒提示條＝健康，關掉即可
- 收到 healthchecks 或 cron-job.org email → 照第 0 節三步走
- 三步走了還是不行 → `docs/PLAYBOOK.md` 案例 1-6 有完整排查法
  （順序：Scheduled run 是否存在 → workflow state → 手動 dispatch 是否正常）

## 8. 系統防護總覽（給未來接手者的地圖）

| 層 | 元件 | 守什麼 |
|---|---|---|
| 觸發 | GitHub schedule `:07` ＋（建議）cron-job.org `:17` | 兩個獨立失效域互為備援 |
| 免疫 | 55 分鐘防重複 guard（fail-open） | 多觸發源不重複燒 API；guard 壞掉時寧可重複、絕不停擺 |
| 偵測（使用者） | 首頁黃/紅新鮮度提示 | 任何人都能看出資料是舊的 |
| 偵測（Hook） | healthchecks 心跳 ＋ cron-job.org 失敗通知 | 停擺 3 小時內 email 到你 |
| 復原 | 本 RUNBOOK 第 0 節三步 | 人類 30 秒可執行 |
