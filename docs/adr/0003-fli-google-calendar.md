# ADR 0003：用 fli（Google Flights 內部 API）補實價——先觀察 2 週

- 狀態：**觀察中**（2026-10-26 起；預計 2026-11-09 依下方標準決定第二階段）
- 日期：2026-10-25 實測、2026-10-26 決定
- 實作：`src/farehunter/fli_calendar.py`、`.github/workflows/fli-observe.yml`、
  `requirements-fli.txt`、`tests/test_fli_calendar.py`；資料在 `data/fli/`

## 背景

兩個缺口：

1. **冷門線沒有資料。** Travelpayouts 只回 Aviasales 使用者近 7 天搜尋過的票價。
   KHH→NGO 約 1–3 週才被搜一次，07～09 月共 6 段資料，每段剛好活 7 天；09-21
   之後全空（HANDOFF §6）。KHH-CTS、KHH-FUK 也很稀疏（每小時 1–3 個出發日，
   TPE-NRT 是 45 個）。
2. **Google 日曆實價消失了。** SearchApi 的一次性試用 100 次在 2026-08 用完後，
   專案只剩 SerpAPI 單日查詢（免費層 250 次／週期，2026-10 實測幾乎用滿，
   見 `docs/quota.json`）。

## 評估過的來源（2026-10-25，GitHub Actions 上 9 輪真實請求）

| 來源 | 結果 | 判定 |
|---|---|---|
| **fli**（PyPI `flights` 0.9.0，Google Flights 內部 RPC） | 日曆一次請求＝一條航線 61 天直飛來回價；8 航線 × 300 天 40 次請求約 4 分鐘；日曆價與即時查詢 6/6 一致，與 SerpAPI 參考價全部一致（KHH-NGO 12/07 14,891、KHH-CTS 11/13 11,094、TPE-NRT 11/29 11,426） | **採用（觀察中）** |
| Kiwi 公開 MCP（`mcp.kiwi.com`，不需金鑰） | 10/10 成功，8 航線都有含稅價，與 Google 接近（KHH-NGO 14,986 vs 14,891），±3 天彈性，每次最多 15 筆 | 備援候選：官方開給 AI 代理用的端點，ToS 風險最低 |
| 台灣虎航官網日曆（`station-daily-prices`） | 一次請求＝該機場出發所有虎航航線、雙向、一年份每日價；KHH-NGO 159 天 | 不採用：只有虎航、未稅、單程，換算成來回含稅價要另一套語意 |
| fast-flights 3.1.0（Google Flights HTML） | 價格正確，但單次成功率 50–83%，沒有日曆 | 不如 fli |
| Trip.com 日曆（`GetLowPriceInCalender`） | 有資料，但 TPE-NRT 回空 | 不可靠 |
| SerpAPI `google_travel_explore` | 指定目的地時連 TPE-NRT 都回 Empty results（花 2 次額度） | 不採用（同 ADR 0001 的結論延伸） |
| Travelpayouts 其他端點、12 個 `market` | 12 個市場結果完全相同；11 個端點對 KHH-NGO 全空 | 沒用：同一份 7 天快取 |
| Amadeus Self-Service | `test.api.amadeus.com` DNS 已解析不到 | 2026-07-17 停止服務 |
| Kiwi Tequila／Skyscanner／Duffel | API 在，但都要審核或公司帳號 | 拿不到 |
| Apify／Ignav | 需要使用者註冊，未測 | 有 fli＋Kiwi 後不需要 |

實測 run：40184426976（第一輪）… 40188927629（OKA 繞法）。

### fli 的已知問題（都已在實作裡處理）

- **Google 偶發內部錯誤**：回 HTTP 200，但 wrb.fr 裡是 status 13，沒有資料。
  即時查詢單次約 4 成，重試大多救回；日曆 40 次裡有 9 個時段回空，重試一次全部補回。
  → 日曆重試 1 次、影子比價重試 2 次，間隔 12 秒。
- **OKA 別名 bug**：fli 用 `Enum("Airport", {代碼: 機場名})` 建表，機場名重複的
  48 個代碼變成別名，`Airport.OKA` 其實是 `Airport.NAH`（印尼 Tahuna），KHH-OKA
  永遠查不到。→ `resolve_airport()` 自建名稱正確的成員；實測 KHH-OKA 日曆 61 天全有價。
  （同一繞法下 KHH-OKA 的**即時查詢**仍回 0 筆，原因未明；日曆不受影響。）
- **內建重試會藏請求數**：fli 的 Client 用 tenacity 自動重試 3 次。→ 包一層不重試、
  固定 3 秒間隔、單次執行最多 200 個請求的 client，重試由我們明確控制。
- **用到私有模組**（`fli.search._wire`、Client 內部）：版本釘死在 `requirements-fli.txt`，
  升版前要重跑實測。

## 決定

**第一階段：只觀察。** 新增 `fli Observe` workflow，跟在 FSC Snapshot／Verify Airlines
之後跑（每天約 2 次）：

1. 日曆：config 的每條航線 × 未來約 300 天（61 天一段），直飛、來回、住 5 晚。
2. 影子比價：最近 24 小時 SerpAPI／Scrape.do 查過的行程，用 fli 查同一組去回日期，
   記下價差與時間差。比過的不再比（看 `data/fli/` 最近 3 天的紀錄）。

**刻意不寫 prices.db。** observations 的讀者不分來源：`route_stats`（new_low 基準）、
export 的統計、健康檢查都會把新資料算進去；寫成 `source='google'` 還會讓 SerpAPI／
Scrape.do 的 72 小時冷卻以為「已經有實價」而跳過付費驗證——觀察期就會悄悄改變警報與
驗證行為。所以資料只寫 `data/fli/<UTC 時間>.jsonl.gz`：每次一個新檔、寫完不再改，
約 20 KB，git 不會累積大型二進位。

全滅（沒有任何一段日曆有價、也沒有任何比價得到回答）時 workflow 變紅；但這輪的錯誤
紀錄照樣先 commit，報告才數得出被擋了幾次。

## 第二階段的判斷標準（`python -m farehunter.fli_calendar report data/fli 14`）

三項都 PASS 才提第二階段（讓 fli 價格參與警報、逐步取代 SerpAPI 驗證）：

| 標準 | 門檻 | 說明 |
|---|---|---|
| 日曆時段重試後有價的比例 | ≥ 95% | 同一（航線, 時段序號）整段期間從沒拿到價的視為沒有班表、不計 |
| 影子比價與 SerpAPI 同行程價差 ±2% 以內的比例 | ≥ 90% | 只評參考觀測後 6 小時內完成的比價 |
| 封鎖頁（captcha／unusual traffic） | 0 次 | 一出現就要重新評估，不論其他數字 |

任一項 FAIL：改評估 Kiwi 公開 MCP 當主力，或維持現狀。

## 風險

- **非官方介面**：Google 改版就可能失效；使用條款屬灰色地帶。用量刻意壓低
  （每天約 2 次 × 60–90 個請求、間隔 3 秒），只給自己用。使用者已知情並同意。
- 失效時只影響這支觀察 job；現有監控不讀 `data/fli/`。
- 每次佔用 `farehunter` 排隊群組約 5 分鐘。

## 若未來要重新評估

不要重跑上表的來源比較——結論與數據都在這裡。真正該重看的是：
fli 升版後 OKA 別名 bug 是否修好（`Airport.OKA.name == "OKA"`）、Google 是否開始
封鎖 GitHub Actions 的 IP、Kiwi 公開 MCP 是否改成要金鑰。
