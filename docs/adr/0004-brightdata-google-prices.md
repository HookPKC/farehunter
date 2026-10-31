# ADR 0004：用 Bright Data 的免費額度補 Google 實價（警報驗證＋每小時巡檢）

- 狀態：**採用**（2026-10-31）
- 實作：`src/farehunter/brightdata_flights.py`、`src/farehunter/runner.py`（警報前驗證、
  抓完後巡檢）、`storage.api_usage` 帳本、`.github/workflows/monitor.yml`（兩個 secret）
- 實測紀錄：ADR 0003 的來源比較表（run 41181583286）；整合後分支實跑 run 41187845349

## 背景

- 看板與警報的價格絕大多數是 Aviasales **快取估價**；Google 實價只有 SerpAPI（約 6 次／天）
  與 Scrape.do（約 3 次／天）在補。2026-09 起**每一則警報都是「未驗證」**，從沒有一則
  經過 Google 確認。
- KHH-NGO、KHH-CTS 這類冷門線，Aviasales 常常好幾天沒資料，健康檢查一直是 stale。
- Bright Data 的免費層：**每月 5,000 次、每月 1 日重置、用完即停不扣款**（官方
  brightdata-mcp README「Pricing and Free Tier」）。它走自己的代理網路，不受 Google 對
  GitHub 主機限流的影響（fli 觀察期遇過，ADR 0003）。
- 2026-10-31 實測：Web Unlocker 抓 Google Flights 搜尋頁，8 航線直飛最低價與 fli 日曆
  **8/8 完全相同**，還帶航空公司名稱；每頁約 2.2 MB、2–15 秒。

## 決定

把 Bright Data 當成**第三個 Google 實價來源**：結果寫成 `source='google'`、
`provider='brightdata'` 的觀測，之後的價格狀態、看板、冷卻、健康檢查全部沿用既有機制，
**不新增價格或通知語意**。

### 用在哪裡（優先順序）

1. **警報前驗證**：monitor 判定是 deal、而且這一則真的會發出去（不是被去重擋掉）時，
   先查同一行程的 Google 實價：
   - 有同航空公司的直飛價 → 當成「同一輪權威價」傳給 `price_state`（它原本就預留了
     `authoritative` 參數）→ `VERIFIED`，並用實價重新判斷是不是 deal：實價不便宜就不發，
     跟既有 VERIFIED 的語意相同。
   - 只有別家航空公司的價格、查詢失敗、或額度用完 → 照原本的狀態發，**不會因此漏發**。
     查到的實價照樣寫進 DB。
2. **巡檢**（用剩下的額度）：抓完 Aviasales 後，各航線**輪流**挑最便宜、24 小時內還沒查過
   的直飛來回行程去查。「查過」＝任何供應商寫過 Google 實價，或 Bright Data 拿過這趟的頁面
   ——後者記在 `api_checks`，因為那天沒有直飛、或航空公司對不上時不會留下觀測，沒記的話
   每小時都會重查同一趟。
   - **起點每小時換一條航線**：11 月每輪約 7 次、航線 8 條，每輪都從第一條開始的話，
     排最後的 KHH-NGO 整個月輪不到。
   - **快取完全沒資料的航線**改查固定日期：2 週後起每 8 天一個出發日（最遠約 11 週）、
     住 5 晚，起點每天輪替。間隔刻意不是 7 天：冷門線不是天天飛（KHH-NGO 只有週一、週三
     有直飛），間隔 7 天會讓整張清單落在同一個星期幾。

### 額度怎麼分（`run_allowance`）

- 目標：每月用到 **95%（4,750 次）**，留 5% 餘裕。
- 每一輪可用＝本月剩餘 ÷ 本月剩餘小時（monitor 每小時一輪），向上取整、單輪上限 15 次。
  前面的輪次失敗或被 guard 跳過，剩下的額度會攤到後面，月底剛好用完；任何一刻都不會
  超過剩餘額度。月初約每輪 7 次。
- 巡檢在單輪內最多花 5 分鐘（`SWEEP_DEADLINE_S`），monitor 每輪多約 1 分鐘，離 30 分鐘
  上限很遠，也不影響 55 分鐘 guard（guard 只看 `source='aviasales'`）。
- 帳本：prices.db 的 `api_usage`（每日每來源；`requests`＝送出次數、含失敗，保守估算；
  `ok`＝拿到可解析頁面的次數）。配速只看本月（UTC）的 `requests`。

### 解析與航空公司代碼

- 從每個航班的 `aria-label="From N New Taiwan dollars round trip total. Nonstop flight with
  X. …"` 取價（`curr=TWD&hl=en&gl=tw`）。`brd_json=1` 對 Flights 無效。
- Google 頁面只寫航空公司**名稱**；用 `AIRLINE_CODES` 轉成 IATA 代碼。**對不上的不寫入**
  ——寫錯代碼會讓 `same_itinerary` 誤判。「Jetstar」在台灣—日本線上一律是 GK。
- 寫入直飛最低價；若想驗證的那家航空公司不是最低價那家，也寫入它的直飛最低價。
- 拿到頁面卻一筆都解析不到 → 記為錯誤（多半是 Google 改版），不寫資料、抓價照常。

## 後果與風險

- 看板上更多行程從「約 X」變成精確實價；冷門線不再 stale（健康檢查本來就不分來源）；
  便宜日推播（只推「已驗證且新鮮」的）會有更多候選。
- `route_stats`（new_low／big_drop 的基準）不分來源，會納入每天約 150 筆 Google 實價
  （Aviasales 約 2,400 筆），基準會小幅貼近真實價格。
- SerpAPI／Scrape.do 有 72 小時冷卻（`_is_cooled`），會跳過 Bright Data 剛查過的行程，
  三家合起來覆蓋得更廣。
- **Google 改版**：解析不到時只記錯誤；`summary["brightdata"]["errors"]` 連續偏高就要看。
  `no_direct`（頁面正常但沒有可寫入的直飛價）偏高，多半是 `AIRLINE_CODES` 少了新的
  航空公司名稱。
- **安全**：Bright Data 的 zone 管理 API（建立／查詢 zone）回應裡有 zone 密碼，
  **絕不能印進 Actions 紀錄**（公開 repo 誰都看得到）。2026-10-31 探測曾印出一次，紀錄已刪、
  已請使用者重設密碼。正式程式只呼叫 `/request`，錯誤訊息只取開頭並遮掉金鑰。

## 實跑結果（2026-10-31，run 41187845349，分支上的正式程式、不發通知不 commit）

- 這一輪額度 15 次（10 月只剩 15 小時，上限 15）：15 次都拿到頁面、0 錯誤，寫進 14 筆
  Google 實價；整輪 monitor（含 Aviasales 抓價）207 秒，離 30 分鐘上限很遠。
- 6 條有快取的航線各查 2 趟、全部寫進；**KHH-CTS、KHH-NGO 的固定日期 3 次 0 筆**——
  間隔 7 天、那天剛好是週二，兩條線週二都沒有直飛（fli 日曆也是）。據此改成間隔 8 天、
  記下「查過」、起點每小時換一條航線（上面「巡檢」一節）。

## 停用

刪掉 repo secret `BRIGHTDATA_API_KEY`（`BRIGHTDATA_ZONE` 沒設時用 `mcp_unlocker`）。
沒有金鑰時 runner 完全不碰 Bright Data，行為與 2026-10-31 之前相同（有測試鎖住）。
