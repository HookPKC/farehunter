# ADR 0005：第三方「更多機票 API」比較表的對照——ChocoData 經通用擷取可補 Google 實價

- 狀態：**評估完成**（2026-11-13）；ChocoData 沒有專用機票端點，但通用擷取 `universal.get` 抓 Google Flights **4/4 與 Bright Data 一致**——**已接入，短期補冷門線**（見文末「接入」）
- 輸入：使用者提供的外部調研比較表「FareHunter 更多機票 API」（2026-11-13，Muse 頁面，
  AI 產生、未經驗證）。本 ADR 只記對照結論，不轉載全文。
- 相關：ADR 0001（SerpAPI deals）、ADR 0003（13 種免費來源實測、fli 觀察期）、
  ADR 0004（Bright Data）

## 背景

比較表列了 20 多家機票 API／擷取服務，結論是「免費做每天監控夠、每小時只有自架 fli」。
它是從零開始調研、沒讀過本專案的實測紀錄，所以有幾處跟我們的現況對不上。這份 ADR 把
「哪些已經有答案」「哪些真的是新的」分開，以後再看到同類清單時先讀這份，不要重跑。

## 比較表過時或不適用的四處

1. **Bright Data「只有二手資料、未實測」**——過時。ADR 0004：2026-10-31 起在正式環境
   跑 Web Unlocker 抓 Google Flights，直飛最低價與 fli 日曆 8/8 相同；免費每月 5,000 次、
   每月 1 日重置，配速目標 95%（4,750 次），單輪上限見 `brightdata_flights.py`。
2. **「沒有來源驗證過樂桃／虎航／星宇查得到」**——已被正式資料推翻。正式 `prices.db`，
   `source='google' AND fare_class='any'`，觀測時間 2026-11-06T05:18Z ～ 2026-11-13T05:18Z
   （共 1,413 筆；brightdata 1,382、serpapi 30、scrapedo 1）。依 `carriers` 計次（聯營
   一筆可算多家）：GK 469、IT 323、MM 161、TR 159、FD 132、AK 75、SL 64、BR 13、CI 8、
   JX 7、VZ 2。
3. **「fli 是唯一無上限的免費路徑」**——不足以當主力。ADR 0003 第二階段報告
   （`python -m farehunter.fli_calendar report data/fli 14`，2026-11-13 跑，28 輪）：
   日曆有價 85.4%（標準 ≥95%，**FAIL**；KHH-NGO 84/140＝60%、KHH-OKA 100/140＝71%）；
   日曆價與參考價 ±2% 內 94.4%（PASS）；封鎖 0 次（PASS）。比較表提到 2026-08 起 Google
   改版、fli 每次回的筆數變少，跟這裡的缺價現象一致但沒有單獨證實因果。
4. **「每小時 12 航線 × 24 × 30 ＝ 8,640 次／月」**——框架不適用。那是假設單一付費來源
   每小時全掃。本專案是 Travelpayouts 每小時免費當主力；Google 實價只用在警報前驗證＋
   每小時巡檢（每天約 160 頁，ADR 0004），瓶頸不在呼叫次數。

## 已有答案、不要重做的

ADR 0003 表格已實測或確認拿不到：fli、Kiwi 公開 MCP、虎航官網日曆、fast-flights、
Trip.com 日曆、SerpAPI `google_travel_explore`、Travelpayouts 其他端點與 `market`、
Amadeus Self-Service（已停）、Kiwi Tequila／Skyscanner／Duffel（要審核或公司帳號）、
Kiwi 網站 GraphQL 日曆、捷星 farecache、Skyscanner 月曆（Actions 上 403）、星宇月曆、
Bright Data。

**Apify、Ignav 不在此列**：ADR 0003 記的是「需要使用者註冊，未測」，到現在仍然沒測過。

## 真正的瓶頸

不是額度，是**冷門線的 Google 實價太少**。同上區間、同條件，各航線筆數：

| 航線 | 筆數 |
|---|---|
| KHH-NRT | 522 |
| TPE-NRT | 341 |
| TPE-KIX | 301 |
| KHH-KIX | 75 |
| KHH-OKA | 58 |
| KHH-FUK | 42 |
| KHH-CTS | 38 |
| KHH-NGO | 36 |

傳統航空（`fare_class='full'`）的 Google 實價同區間只有 25 筆，PR #25 才開始系統性累積。
新來源的價值要用「能不能多補冷門線／傳統航空」衡量，不是「能不能每小時跑」。

## 比較表裡真正新的三家

| 來源 | 比較表的說法（未經本專案驗證） | 判定 |
|---|---|---|
| **ChocoData**（Google Flights 端點） | 每月 5,000 credits、每次 5 credits ≈ 1,000 次；曾有「每月 vs 一次性」說法衝突；靜態金鑰、有 Python SDK | **首選試接**：Google 來源，價格語意跟 Bright Data 相同（google 觀測價精確），不動六處價格語意 |
| **google-flights2**（RapidAPI, DataCrawler） | 每月免費 150 次；CalendarGrid＝去程×回程二維價格表 | **次選**：一次看一整段日期，冷門線每週掃一次額度夠；單一小廠商，先實測 |
| **ReefAPI**（`price_graph`） | 1,000 credits 一次性、不過期；票價來源不公開；2026-02 才上線 | 只適合一次性測試，不排進自動流程 |

不採用：

- **Decodo**（Skyscanner 擷取）：價格是 Skyscanner 語意，跟 google／快取兩種都不同，
  要另開語意；回傳是 HTML、parser 要自己寫。
- **LetsFG MCP**：免費管道走 MCP／OAuth，沒有可放 GitHub Actions 的靜態金鑰，不能無人值守。
- **付費方案**（Sky-Scrapper、FlightPowers、SearchApi、HasData、Duffel 等）：瓶頸不在額度，
  現在不需要付費。
- 比較表「避開清單」（Aviation Edge、Tripgic、AeroDataBox、OpenSky 等）與我們的認知一致。

## ChocoData 試接前必須先確認（不要寫死）

1. **實際扣點**：一次 Google Flights 查詢到底扣幾 credits、額度是每月重置還是一次性。
   「1,000 次 ≈ Bright Data 的兩成」只在「一次查詢＝5 credits、每月重置」時成立。
2. **回傳有沒有航空公司名稱**：驗證與 LCC／傳統航空分類都靠 `carriers`，沒有就不能用。
3. **價格對得上**：同一輪、同一趟（去回日）跟 Bright Data 比，沿用 ADR 0003 的 ±2% 標準，
   航線先用冷門線 KHH-CTS、KHH-NGO、KHH-FUK。
4. **金鑰不外洩**：repo 公開、Actions 紀錄公開；secret 名稱定為 `CHOCODATA_API_KEY`，
   回應原文與金鑰一律不印出。

試接方式：使用者註冊後，先做手動觸發的一次性探測（新增腳本＋workflow，不動正式流程）；
結果過關再另外提案接進巡檢。

## 若未來要重新評估

- fli 日曆有價比例回到 ≥95% → 回 ADR 0003 第二階段，重新考慮它當主力。
- Bright Data 免費層變動 → 先試 ChocoData／google-flights2 補位，不急著付費。
- 冷門線 Google 實價筆數追上熱門線 → 新來源的價值下降，可以不接。

## 2026-11-13 ChocoData 實測結果：不採用

使用者註冊並存好 secret `CHOCODATA_API_KEY` 後，用一次性 workflow `Probe ChocoData` 探測
（run 43385166572、43385381300、43385535411；探測腳本與 workflow 已刪除，見 git 歷史 #27/#28）：

- 呼叫格式：`GET https://api.chocodata.com/api/v1/{target}/{resource}?api_key=…`（官方 SDK
  `chocodata` 0.1.0；金鑰在網址查詢參數，header 認證回 401）。
- `google/flights`、`google_flights/search` 等 6 個候選全部 404 `unknown_target`。錯誤回應附了
  **全部 497 個可用端點**，其中跟 Google／旅遊有關的只有：`google.search`、`googlenews.*`、
  `googleplay.*`、`agoda.*`、`airbnb.*`、`expedia.hotel/search`、`kayak.hotel/search`、
  `priceline.hotel/search`、`tripadvisor.*`——**沒有 Google Flights，也沒有任何機票端點**。
- `kayak/search` 不帶參數回 400 `invalid_params`，唯一必填是 `location`（單一地點＝住宿搜尋的
  形狀，不是出發地＋目的地的航班搜尋）。
- 404 與 400 的回應 header 都是 `Spb-Cost: 0`：打錯路徑、缺參數不扣點；三輪探測共花 0 點。

當時的結論「不採用」只看了專用端點，**漏查通用擷取端點**，見下一節。

## 2026-11-13 補查：通用擷取 `universal.get` 可以抓 Google Flights

使用者問「完全沒有機會嗎」，補查（run 43386107658，0 點）：把 497 個端點全部列出，對疑似通用
擷取與旅遊 search 端點各打一次不帶參數的請求。`agoda／expedia／kayak／priceline.search` 必填都是
`location`（住宿）；**`universal.get` 唯一必填 `url`**——給網址抓頁面。

用它抓 Bright Data 同一個網址（`brightdata_flights.search_url()`），用同一個 `parse()` 解析
（run 43386372844、43386513149、43386517867、43386521548）：

| 行程 | ChocoData 直飛最低 | Bright Data（同一趟） | 差 |
|---|---|---|---|
| KHH-NGO 2026-12-07～12-12 | 8,310 IT | 8,310 IT（06:17Z） | 0 |
| KHH-CTS 2027-01-11～01-16 | 28,212 SL | 28,189 SL（06:17Z） | +0.08% |
| KHH-NGO 2027-01-13～01-18 | 16,710 IT | 16,710 IT（04:17Z） | 0 |
| KHH-CTS 2027-01-02～01-07 | 27,311 IT | 27,311 IT（04:17Z） | 0 |

- **4/4 在 ±2% 內**（ADR 0003 標準），航空公司也一致；ChocoData 抓的時間是 07:31–07:33Z，
  比 Bright Data 晚 1–3 小時，所以 CTS 那筆 +23 元合理。
- 回應 JSON：`url`、`content_type: html`、`html`（約 2.05 MB，已是 JS 渲染後的頁面，含
  10–14 個 `round trip total` aria-label）。不用加 `render_js` 之類的參數。
- **每次 `Spb-Cost: 5`**，約 2–8 秒；`X-Concurrency-Limit: 10`。四次共 20 點。
- 「每月 5,000 credits＝1,000 次」的**次數換算成立**；「每月重置還是一次性」API 回應看不出來，
  要看 ChocoData 後台（剩餘點數與重置日）。

結論：ChocoData 等於一個**小號的 Bright Data**（同一個網址、同一個解析器、同一種 google 價格
語意），每月約多 1,000 頁，可以專門分給冷門線巡檢。接入方式（配速、帳本 provider、只跑冷門線）
另案提案，不在本 ADR 決定。探測腳本與 workflow 已刪除（git 歷史 #27/#28/#30/#31）。

## 2026-11-13 接入：一次性額度，短期補冷門線

後台 Billing 的方案卡寫「1,000 requests (one-time)」、「Forever free on signup」：免費點數是
**註冊時一次性**的（同頁「Credits granted this month 5,000」應是註冊當月的紀錄；12/1 看餘額有沒有
回到 5,000 即可確認）。所以不做長期整合，只用這批點數把冷門線的 Google 實價補一段時間，給 NGO／CTS
門檻重算與傳統航空門檻用。

- 實作：`src/farehunter/chocodata_flights.py`（`ColdSweeper`）；`runner.py` 在 Bright Data 巡檢
  **之後**呼叫（不重查它這輪剛查過的行程；Aviasales 全滅的輪次不跑，帳本不會被 commit）；
  `monitor.yml` 加 secret `CHOCODATA_API_KEY`。
- 只查 `COLD_ROUTES`＝KHH-CTS、KHH-NGO、KHH-FUK、KHH-OKA。候選＝本輪快取最便宜的直飛來回，
  再用 `anchor_trips` 補到 12 趟；固定日期往後錯開 4 天，跟 Bright Data 的日子交錯（不錯開時，
  用正式資料模擬一天只有 14 趟沒被「24 小時內查過」擋掉，錯開後 36 趟）。
- 額度：每輪 2 頁、每天 30 頁、累計 950 頁（`api_usage` provider='chocodata'），約 32 天用完；
  ChocoData 回點數不足（402 或錯誤碼含 credit／insufficient／quota／balance）本輪立刻停。
- 寫入：沿用 Bright Data 的 `parse()`／`select()`，provider 改成 'chocodata'、source 仍是
  'google'；含傳統航空那筆（fare_class='full'）。排名的可靠度與新鮮度都依 source 查表，不受影響。
- 金鑰在網址查詢參數：連線錯誤只留例外類型（`from None`），HTTP 錯誤訊息遮掉金鑰的原文與三種
  網址編碼；預期外的例外只記類型。測試鎖住這幾點。

## 2026-11-14 google-flights2（RapidAPI）：接成警報的第二驗證來源

使用者訂閱 BASIC（$0）並存好 secret `RAPIDAPI_KEY`。探測（workflow `Probe RapidAPI`，共 6 次）：

- 額度：回應 header `X-RateLimit-Requests-Limit: 150`，約 30 天重置；參數錯誤（400 類）也扣 1 次。
- `GET /api/v1/searchFlights`（`departure_id`、`arrival_id`、`outbound_date`、`return_date`、
  `currency=TWD`…）可用。跟 Bright Data 同一小時（06:17Z 查、06:42Z 比）的兩趟，4 個價格完全相同：
  KHH-NRT 11/30–12/07 捷星 11,114、泰亞航 13,087、華航 14,468；KHH-OKA 01/15–01/17 捷星 7,478。
- 回應的坑：`stops` 連轉機航班都是 0（直飛要看 `flights` 只有一段）；`price` 可能是 "unavailable"。
  航空公司代碼取班號前綴，不用名稱對照（所以聯合航空這類 Bright Data 對照表沒有的也讀得到）。
- 附帶 `priceHistory`：Google 的 low／typical／high 區間與 61 天走勢。**這次沒有存**，之後可拿來
  補冷門線的「Google 判定」。
- `getCalendarGrid`（去程×回程價格表）：`departure_id`／`arrival_id` 正確（用 `origin`／
  `destination` 會被退回），但指定日期範圍的參數名稱還沒找到，回的是預設的近 7 天且含轉機價。
  待使用者提供 RapidAPI 頁面的參數清單；探測 workflow 先留著。

接入（`src/farehunter/gflights2_flights.py`）：`Verifier` 繼承 Bright Data 的同一套流程
（`brightdata_flights.Verifier` 的 provider／parse／額度改成可由子類別換掉），provider='gflights2'、
source 仍是 'google'。`runner` 只在 Bright Data 之後警報仍不是 VERIFIED 時才問它；查到同航空的
直飛價就照實價重判（不便宜不發），查不到照原狀態（未驗證→只發給擁有者）。額度每輪 2、每天 6、
近 30 天 130 次；不巡檢。
