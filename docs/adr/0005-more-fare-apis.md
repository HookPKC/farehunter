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

ADR 0003 表格已實測或確認拿不到：fli、Kiwi 公開 MCP、虎航官網日曆（價格語意不合；2026-11-21 起
只當收集與巡檢線索，見最後一節）、fast-flights、
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
- `getCalendarGrid`（去程×回程價格表）：必填 `departure_id`／`arrival_id`（`origin`／`destination`
  會被退回）。不帶範圍時回預設的近 7 天；帶 `start_date_x/end_date_x/start_date_y/end_date_y`
  回 `Something went wrong`（2026-11-14 試一次，沒再追）。
- `getCalendarPicker`：**可用**。`departure_id`、`arrival_id`、`start_date`、`end_date`、
  `trip_type=ROUND`、`trip_days=5` → 一次回 62 天、每天一筆「出發日＋5 晚」的最低價。KHH-NGO
  有直飛的日子跟當天的 Bright Data／ChocoData 直飛價一致（12/02 10,910、12/09 9,110、12/12 12,237、
  12/14 9,910、12/21 18,561、12/23 12,910、12/30 12,810）。**但它是含轉機的最低價、沒有航空公司**：
  沒有直飛的日子回的是轉機價（一整排 13,359／13,442），有直飛但轉機更便宜的日子也回轉機價
  （01/13 回 13,359，直飛是 16,710）；`stops=1` 參數被忽略。所以**不能當直飛的 Google 實價寫入**，
  只能當「哪幾天可能便宜」的線索，交給 Bright Data／ChocoData 去查實價。尚未接入。

接入（`src/farehunter/gflights2_flights.py`）：`Verifier` 繼承 Bright Data 的同一套流程
（`brightdata_flights.Verifier` 的 provider／parse／額度改成可由子類別換掉），provider='gflights2'、
source 仍是 'google'。`runner` 只在 Bright Data 之後警報仍不是 VERIFIED 時才問它；查到同航空的
直飛價就照實價重判（不便宜不發），查不到照原狀態（未驗證→只發給擁有者）。額度每輪 2、每天 6、
近 30 天 130 次；不巡檢。

## 2026-11-14 Picker 線索：告訴巡檢先查哪幾天

使用者確認：Picker 價只當線索；**只要直飛**，挑出來的日子要 Bright Data／ChocoData 查到直飛實價才算，
查不到就丟掉、不寫入、不發警報。CTS 不抓；固定 5 晚。

四條冷門線抽查（Picker 價 vs 資料庫裡同一趟近兩天的直飛實價）：KHH-OKA 13 天有 11 天相同（另 2 天
的實價是前一天查的，差 1–6%）；KHH-NGO 有直飛的 7 天全相同；KHH-FUK 11 天只有 5 天相同、KHH-CTS
4 天只有 1 天，其餘是便宜 4,000–15,000 的轉機價。Picker 價含轉機，所以不會高於直飛實價。

實作：

- `calendar_hints.py`（挑）＋`storage.calendar_hints` 表（存）。價格、看板、警報、統計都不讀這張表。
- `gflights2_flights.refresh_hints`：KHH-OKA／NGO／FUK 各 2 段（每段 62 天，共約未來 4 個月），每段
  7 天重抓一次、每輪最多抓 1 段。跟警報驗證共用額度：近 30 天用量 ≥ 65 或當天 ≥ 3 次就不抓，留給
  驗證。每月約 26 次。回應只收「回程＝去程＋5 晚、日期在範圍內、價格為正數」的列。
- 巡檢（Bright Data 與 ChocoData）：每條線把線索價最低的幾天排在候選最前面（Bright Data 6 天、
  ChocoData 12 天），之後才是快取候選與固定日期。過濾：最近查了沒有直飛的星期幾不挑（KHH-NGO 只剩
  週一、週三）；線索價超過門檻 ×1.25 的不挑；線索超過 9 天沒更新就不用（Picker 壞掉時自然退回
  固定日期）。
- 線索日查到直飛實價 → 照既有流程寫入（那是真的直飛價）；沒有直飛 → 不寫入，並記成那個星期幾
  沒有直飛。警報只看直飛實價與快取價，跟線索無關。
- 用當天的 Picker 資料加正式 prices.db 模擬：KHH-NGO 挑到的 8 天全是週一、週三且等於直飛實價；
  KHH-OKA 10 天裡 3 天還沒查過（含 12/13 線索價 5,884，低於門檻 6,500）；KHH-FUK 11 天裡 3 天還沒
  查過、3 天其實是轉機價（直飛 15,381–25,291）。
- 探測 workflow 與腳本已刪除（git 歷史 #35、#38）。`getCalendarGrid` 不再追。

## 2026-11-21 虎航官網日曆：只收集、當巡檢線索

使用者問「虎航或一般航班有特賣時就很便宜，可以定期去看嗎？」。ADR 0003 不採用虎航官網日曆，是因為
它的價格是**單程、未稅**，不能當直飛來回實價；這裡也不當實價，只用在兩件不碰價格語意的事。

2026-11-20 探測（GitHub Actions 上一次性跑，桃園、高雄各 1 個請求，各約 1 秒、不用金鑰）：

- 一個機場一次回所有進出航線、約一年份的每日最低價（桃園 16,637 列、高雄 9,768 列）。我們 8 條線
  去回兩向都有：熱門線約 340 天，CTS 182 天、FUK／NGO 約 147 天（不是天天飛）。
- 跟資料庫裡同一趟的 Google 直飛實價配到 91 趟：「官網去程＋回程」通常比實價少 1,100–2,900，
  各線中位數 KHH-NRT 1,113 到 KHH-CTS 2,913，大致是來回的稅和雜費。
- 每列有更新時間，最舊的是 9 天前：少數日子會過時，有幾趟官網價反而比實價高。
- 當天就看到 KHH→OKA 單程 999 的促銷日（12/3、12/8–12/11）。

實作：

- `tigerair_calendar.py`：每輪 monitor 抓桃園、高雄 2 個請求（每天 48 次），只留我們航線的
  去回兩向。`tigerair_fares` 表**只在價格變了才寫一筆**（從日曆上消失的日子不記），出發日過了
  30 天的刪掉。價格、看板、警報、統計都不讀這張表。
- **收集 1–2 週、不發任何通知**：看特賣多常出現、跌多少、撐多久，再提特賣提醒的方案。
- **巡檢線索**：每條線從這一輪抓到的資料挑「去程＋住 2–10 晚的回程」加總最低的組合，每個去程日
  取一個（同價取接近 5 晚），加總＋1,000（各線稅費中位數最低的一條取整）仍超過門檻 ×1.25 的不挑。
  Bright Data 每條線排 4 個、ChocoData（只有冷門線）排 8 個，放在 Picker 線索之前：虎航在這些航線
  只飛直飛，比含轉機的 Picker 準。CTS 也有（Picker 不抓 CTS）。
- 線索日查到直飛實價 → 照既有流程寫入；沒有直飛 → 什麼都不寫。查到的實價會不會發警報照舊：
  `sweep_alerts` 航線（CTS／FUK／OKA／NGO）看門檻，其他四條線只寫進價格資料、不從巡檢發警報。
- 抓不到（虎航改版、擋掉、逾時）只少了這份線索，巡檢照舊；Aviasales 全滅的輪次不抓（不會 commit）。
- 測試一律不連外：`tests/conftest.py` 把預設抓取換成一律失敗，要測抓取結果的測試明確傳 fetcher。
- 探測 workflow 與腳本已刪除（git 歷史 #46）。捷星官網 farecache 之後可以照同樣方式評估。

上線後第一天（2026-11-21）：

- 第一輪記全年 4,376 筆，之後**每小時 240–270 筆變化**（更新時間都是當天、漲跌各半，約三成在兩個
  價格間來回跳），prices.db 每小時多長約 30–40 KB（一天約 0.7 MB）。封存（ADR 0002）管不到這張表，
  照這速度幾天內會碰到 48 MiB 的重新評估線。改成**只記未來 120 天內出發的日子**（約佔變化的四成）、
  **變化紀錄只留 14 天**（更早的每格只留最新一筆）；正式資料試算 4,883 → 1,783 筆，之後約停在 3 MB 內。
  巡檢線索不受影響（用當輪抓到的全年資料）。
- 第一輪的 2 則巡檢警報（KHH-FUK 6,781、KHH-NGO 7,210）都來自虎航線索日。
- 線索日的實價比「官網去回加總」多 2,400–6,600，比探測時隨機配對的 1,100–2,900 大：官網最低價
  （例如 999）挑出來的多半在 Google 上訂不到（可能賣完或沒更新）。特賣提醒不能直接看官網價。

## 2026-11-22 ScrapingAnt 實測：抓 Google Flights 跟 Bright Data 3/3 完全一致

為什麼找：ChocoData 是一次性額度，約 12 月中用完；冷門線的 Google 實價會掉回只剩 Bright Data。
要找**每月重置**的免費來源。ScrapingAnt 免費方案每月 10,000 點、不用信用卡，週期從註冊日起算
（這個帳號是每月 22 日）。

探測（run 44971164562，一次性 workflow，已刪除）：用 Bright Data 同一個 `search_url()`／`parse()`／
`select()`，請 ScrapingAnt `/v2/general`（`browser=true`、預設機房 proxy、金鑰放 header `x-api-key`）
抓 prices.db 裡同一小時剛查過的 3 趟：

| 行程 | ScrapingAnt | 同一小時的 Google 實價 | 頁面 |
|---|---|---|---|
| KHH-FUK 12/11–12/16 | 8,173（直飛 4／18 個行程） | Bright Data 8,173 | 2.6 MB、6.1 秒 |
| KHH-NGO 12/12–12/16 | 9,110（直飛 2／10） | ChocoData 9,110 | 2.5 MB、17.9 秒 |
| KHH-NRT 12/07–12/10 | 8,490（直飛 14／18） | Bright Data 8,490 | 2.7 MB、19.8 秒 |

- **價格 3/3 完全相同**，目標站都回 200，機房 proxy 就夠（沒用到住宅 proxy）。
- `/v2/usage` 前後 10,000 → 9,970：**每頁 10 點**，每月約 1,000 頁（每天約 30 頁），跟 ChocoData 的
  每天 30 頁同級，而且每月重置。
- 每頁 6–20 秒；免費方案同時只能 1 個請求，所以每輪頁數要少（跟 ChocoData 一樣每輪 2 頁）。
- 價格語意跟 Bright Data 相同（google 觀測價精確），不動六處價格語意。接入另案提案。

## 2026-11-22 ScrapingAnt 接入：第三個冷門線巡檢，接替 ChocoData

- `scrapingant_flights.py`：沿用 `chocodata_flights.ColdSweeper` 整套巡檢（只查 CTS／NGO／FUK／OKA、
  虎航與 Picker 線索優先、快取候選、固定日期補滿、沒飛的星期幾避開），只換 provider（'scrapingant'）、
  抓取（`/v2/general`、`browser=true`、金鑰在 header）、額度、固定日期錯開 2 天（Bright Data 0、
  ChocoData 4）。`ColdSweeper` 因此改成可由子類別換掉這幾項。
- **24 小時內三家誰拿過這趟的頁面都不再查**（Bright Data／ChocoData／ScrapingAnt 互相看得到），
  沒飛的星期幾也三家共用。
- **配速照實際剩餘點數**：每輪先查 `/v2/usage`（不扣點），留 500 點緩衝，剩下的頁數平均分到重置日；
  今天最多「(剩餘頁數＋今天已用) ÷ 含今天的剩餘天數」，每輪最多 2 頁。查不到剩餘點數時退回每天 30 頁、
  近 30 天 950 頁。點數用完（402／訊息含 credits、quota、exceed）本輪立刻停。
- 查到的實價跟其他巡檢一樣進 `_sweep_alerts`（SWEEP_PROVIDERS 加入 scrapingant）。
- 看板用量面板加一列（近 30 天程式用量／950）。
- 不拿來做警報驗證：11/14 起 31 則警報 29 則已驗證，google-flights2 近 30 天才用 18／130，不缺。

