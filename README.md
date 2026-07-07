# FareHunter v1 — 台灣出發機票低價監控

自動監控指定航線票價，記錄歷史價格，偵測低價並透過 Telegram / LINE 通知。
資料來源為 **Travelpayouts / Aviasales Data API**（合法官方 API，非爬蟲）。
（v1 原使用 Amadeus Self-Service，因其於 2026-07-17 停止服務，v1.1 已切換資料源。）

## 架構

```
config.yaml ──> runner ──> Aviasales Data API ──> parse ──> SQLite (prices.db)
                                                        │
                                              analyzer（三條規則）
                                                        │
                                        Telegram / LINE 通知（含去重）
```

- `src/farehunter/travelpayouts.py` — Aviasales prices_for_dates 客戶端，含 429 重試
- `src/farehunter/storage.py` — SQLite 價格觀察與警報紀錄
- `src/farehunter/analyzer.py` — 低價判斷規則
- `src/farehunter/notify.py`  — Telegram Bot API / LINE Messaging API push
- `src/farehunter/runner.py`  — 主流程與日期取樣

## 低價判斷規則（任一觸發即通知）

| 規則 | 條件 | 何時可用 |
|---|---|---|
| `absolute` | 價格 ≤ 該航線設定的絕對門檻 | **第一天就可用** |
| `new_low` | 價格 < 歷史最低 | 需累積 ≥ 30 筆觀察 |
| `big_drop` | 價格 ≤ 中位數 × 75% | 需累積 ≥ 30 筆觀察 |

同一航線+日期 24 小時內只通知一次，除非新價格再便宜 5% 以上。

## 安裝

```bash
pip install -r requirements.txt
cp .env.example .env    # 填入金鑰後 export，或直接設定環境變數
```

### 1. 取得 Travelpayouts Token（免費）

1. 到 https://www.travelpayouts.com 註冊（開放註冊，填 email 即可）
2. 登入後到個人資料（Profile）的 **API token** 區塊複製 token
3. 設定環境變數 `TRAVELPAYOUTS_TOKEN`

資料來自 Aviasales 使用者近期搜尋的快取低價，天生適合價格監控；
但不是即時報價，下訂前一律點警報裡的連結核對現價。

### 2. Telegram 通知（可選）

1. 跟 @BotFather 建立 bot，取得 `TELEGRAM_BOT_TOKEN`
2. 跟你的 bot 說一句話，然後開 `https://api.telegram.org/bot<TOKEN>/getUpdates` 找到 `chat.id` → `TELEGRAM_CHAT_ID`

### 3. LINE 通知（可選）

1. 到 https://developers.line.biz 建立 Messaging API channel
2. 取得 Channel access token → `LINE_CHANNEL_ACCESS_TOKEN`
3. 加你的官方帳號為好友，從 webhook 或 channel 設定頁取得你的 User ID → `LINE_USER_ID`

兩個通知渠道都沒設定時，警報會直接印在終端機。

## 執行

```bash
python run.py                        # 用 config.yaml 與 prices.db
python run.py myconfig.yaml my.db    # 指定檔案

python -m pytest tests/ -v           # 跑測試（15 個，不需網路與金鑰）
```

## 免費雲端部署（GitHub Actions）

1. 把整個資料夾 push 到一個 **private** GitHub repo
2. Repo → Settings → Secrets and variables → Actions，新增 secret：
   `TRAVELPAYOUTS_TOKEN`，以及選用的
   `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`、`LINE_CHANNEL_ACCESS_TOKEN`、`LINE_USER_ID`
3. Actions 頁面手動觸發一次 `FareHunter Monitor` 確認能跑
4. 之後每 6 小時自動執行，`prices.db` 與 `docs/data.json` 會自動 commit 回 repo

## 手機 App（GitHub Pages + PWA）

`docs/` 內含完整儀表板：出境看板風格、每航線價格趨勢圖、未來出發日票價日曆
（點日期直接開 Google Flights）、警報清單。每次監控執行後 `data.json` 自動更新。

啟用步驟：
1. Repo → Settings → Pages → Source 選 **Deploy from a branch**，
   Branch 選 `main`、資料夾選 **/docs** → Save
2. 開啟 `https://<你的帳號>.github.io/<repo名>/`
3. iPhone：Safari 分享 → **加入主畫面**。之後點圖示就是全螢幕 app，
   離線也能看最後一次的資料（service worker 快取）

注意：Pages 網址是公開的（repo 可以維持 private，Pages 需要付費方案才能設私有）。
內容只有票價資料，無金鑰無個資；介意的話可改用 Cloudflare Access 等方式擋。
首次執行前，儀表板會顯示標記為 SAMPLE 的示範資料。

## 調整監控航線

編輯 `config.yaml`。每條航線可覆寫 defaults，例如：

```yaml
  - origin: KHH
    destination: HND
    absolute_threshold: 8500
    non_stop: true
```

## 已知限制（v1.1 誠實聲明）

- **快取資料，非即時報價**：價格來自 Aviasales 使用者近期搜尋，熱門航線（TPE 出發）
  更新頻繁，冷門航線（KHH 出發）可能稀疏或缺資料——首跑後看 log 的
  `No cached fares` 訊息即可判斷，必要時可在 config 加 `market` 參數調整資料來源市場
- **看到的低價可能已失效**：快取價有時效，警報附訂票連結，下訂前務必核對
- **Google Flights 深連結僅帶「航線＋去回日期」**：連結使用 `q=` 自然語言查詢，
  可用但脆弱；旅客數/艙等依 Google 預設（1 成人、經濟艙），無法保證落到同一筆票或同一價格。
  曾實測在 `q=` 附加 `for 1 adult economy class` 會導致 Google 落地頁解析退化
  （目的地與日期落空），已回退。若未來要精準控制旅客數/艙等，需另開任務研究
  Google 的結構化參數（如 `tfs=` 編碼），不要再往 `q=` 疊加自然語言。
- **統計規則需要時間**：`new_low` / `big_drop` 要累積約 2–4 週資料才會啟用
- 尚未涵蓋：飯店、Error Fare 交叉比價、價格預測——這些是 v2+ 範圍，
  等歷史資料累積起來才有意義

## 開發準則

改動本專案前請先讀 [`docs/PLAYBOOK_SHORT.md`](docs/PLAYBOOK_SHORT.md)（60 秒速讀）；完整準則見 [`docs/PLAYBOOK.md`](docs/PLAYBOOK.md)，可複用的任務模板見 [`docs/PROMPT_TEMPLATES.md`](docs/PROMPT_TEMPLATES.md)。

維運與排程異常自救流程（非開發文件，供日常維護與無 AI 時使用）見 [`docs/RUNBOOK_HOOK.md`](docs/RUNBOOK_HOOK.md)。
