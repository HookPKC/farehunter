"""全測試共用的安全預設。

**測試絕不連外、絕不寫進 docs/。**

專案本來就有「零真實 API」的規範，但那靠的是每個測試自己記得 mock。有兩個
漏洞不是靠自律能補的：

1. `fsc_snapshot.run()` 的 `quota_path` 預設是 `docs/quota.json`，而它的測試
   有二十來個呼叫點都走預設值。若執行測試的環境剛好 export 了 `SERPAPI_KEY`
   （開發者本機很常見；CI 的 Run tests 步驟沒有帶 secrets，所以 CI 安全），
   那些測試就會（a）真的去打 SerpAPI，（b）覆寫 repo 裡真實的 docs/quota.json。
   實測確認過會發生：把 quota 的無金鑰保護拿掉跑一次突變測試，repo 裡就多出
   一個 status='error' 的 docs/quota.json。
2. 同一類隔離漏洞已經在 `docs/data.json` 上咬過兩次（測試讀到 repo 的真實
   看板內容，結果隨資料時綠時紅）。

所以在這裡一次性清掉所有付費 API 的金鑰環境變數。個別測試若要驗證「有金鑰
時」的行為，就明確傳 `api_key=` 參數（見 test_quota.py），意圖留在測試裡，
不依賴執行環境。
"""
import pytest

#: 清掉所有可能讓測試真的連外的金鑰。新增付費 API 時記得加進來。
API_KEY_ENV_VARS = (
    "SERPAPI_KEY",
    "SCRAPEDO_KEY",
    "SEARCHAPI_KEY",          # 子系統已於 2026-08 移除，保留防舊環境殘留
    "TRAVELPAYOUTS_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "LINE_CHANNEL_ACCESS_TOKEN",
)


@pytest.fixture(autouse=True)
def _no_real_api_keys(monkeypatch):
    for name in API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
