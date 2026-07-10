# FareHunter Playbook（短版・開工前 60 秒速讀）

完整版：`PLAYBOOK.md`｜Prompt 模板：`PROMPT_TEMPLATES.md`｜人類復原手冊：`RUNBOOK_HOOK.md`｜AI 接手指令：`HANDOFF_AI.md`

## 13 條守則

1. 部署驗收看 deploy run 的 `head_sha == main HEAD` ＋ deployment 環境 SHA；不看 success、不看 repo 檔案。
2. push 後等 ≥30 秒才 dispatch deploy（或等 workflow_run 自動觸發）。
3. `gfLink` 的 `q=` 凍結；要控旅客/艙等 → 另開 `tfs=` 研究任務。
4. google 觀測價＝精確數字；快取/估價＝「約 NT$X」百位化。全站含通知，無例外。
5. 同一數值只有一個計算點；用共用常數，不複製邏輯。
6. 續作第一步：`git status` + `git diff`。
7. 改文案先 grep 測試裡的舊字串，同輪更新。
8. 探針報 ✗ 先驗探針，再動產品碼。
9. 語意類改動先提案等確認；外部服務行為要真機驗收才算完成。
10. 一輪一件事；回報必含剩餘限制與「無法驗證的部分」。
11. 資料沒更新時，先查 Scheduled run **是否存在**（零 run ≠ run failed）；沿
    源頭往下排查：排程 → run → commit → deploy → 快取。詳見完整版 1-6。
12. 觸發源可以有多個，抓價入口只有一個 guard：55 分鐘內跳過、**fail-open**
    （guard 壞掉寧可重複，絕不停擺）；skip 必須連 export/commit 一起跳過，
    否則會空轉重寫 generated_at。人類復原流程一律指向 `RUNBOOK_HOOK.md`。
13. 缺一筆 hourly commit ≠ 故障：先分辨 guard 跳過／concurrency 排隊補跑／
    平台單次取消（判讀表見 `HANDOFF_AI.md` §4）；連續 ≥2 小時無新 commit 才
    升級。**排程表 ≠ 實際執行**——修任何「撞車」前，先用 run 紀錄證明它真的
    發生過（完整版 1-7）。

## 5 個高風險區（未授權不碰）

1. `gfLink` / Google 深連結（實測過會解析退化）
2. 價格語意文案體系（六處對齊：Hero/日期籤/月度/推薦/次要籤/通知）
3. `export_web` latest 查詢 ↔ `intelligence._SELECT`（相同視窗＋去重才保證 Hero==cheapest）
4. 通知模板（3 處測試鎖定；全是快取價）
5. `render(data)` 流程與 deploy workflow 設定（重繪副作用／部署競態史）

## SOP（6 步）

1. `git status`/`diff` 對齊現況
2. grep 直接相關檔案，不重分析專案
3. 語意/引擎/外部整合/UI 結構 → 先提案；純文案/對照表/顯示層排序 → 可直接做
4. 最小 diff 實作，一輪一件事
5. pytest＋smoke → 線上實資料全頁渲染比對 → ✗ 先驗探針
6. 部署核對 head_sha＋環境 SHA；外部行為待 Hook 真機確認

## 先提案 vs 可直接做

| 先提案等確認 | 可直接做（仍要驗證） |
|---|---|
| 價格/時間/推薦/通知語意 | 純文案字串替換（同步更新測試） |
| 引擎權重/視窗/排名 | 對照表擴充（航空、來源） |
| 深連結、通知管道 | 顯示層排序/過濾 |
| UI 結構（新增區塊/移位） | 一行導讀/免責、`<details>` 內容 |
| render 流程重構 | README 已知限制補充 |
