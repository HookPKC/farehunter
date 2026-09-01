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
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from farehunter import cheap_days as _cheap_days

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


#: 測試期間看板檔案的替身。指向一個不存在的路徑，所有讀取端都會 fail-soft
#: 成空清單——想測看板內容的測試必須明確傳自己的 data_path。
_NONEXISTENT_BOARD = "/nonexistent/tests-must-pass-their-own-data-path.json"


@pytest.fixture(autouse=True)
def _isolate_board_file(monkeypatch):
    """讓測試永遠讀不到 repo 裡真實的 docs/data.json。

    這個 bug 類別已經咬過三次，第三次直接讓生產紅燈：
    test_verification_prefers_route_diversity 沒傳 data_path，於是
    build_verification_plans 讀了真實看板。當晚 verify-airlines 把看板內容
    換成含實價的版本後，cheap_day 池開始供料、多出一筆同 route 的計畫，
    測試就紅了——而 monitor.yml 是先跑 pytest 才抓價，等於每小時的價格
    收集全部停擺。

    修法不是「幫那個測試補參數」，而是讓隔離變成預設：讀取端的預設值都是
    None，到 cheap_days.DEFAULT_DATA_PATH 才解析，這裡把那個常數換成不存在
    的路徑。於是任何忘記傳 data_path 的測試（含以後才寫的）都拿到空看板，
    而不是拿到今天剛好長什麼樣的真實資料。
    """
    monkeypatch.setattr(_cheap_days, "DEFAULT_DATA_PATH", _NONEXISTENT_BOARD)
