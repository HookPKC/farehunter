#!/usr/bin/env bash
# 把測試套件放到未來跑，找出「今天綠、某天必紅」的定時炸彈。
#
# 為什麼需要這支：monitor.yml 是先跑 pytest 才抓價，所以一個會隨時間腐爛的
# 測試＝抓價停擺。2026-09 五天內發生兩次，合計損失 14 班觀測：
#   09-02  test_cheap_day_notify 的種子時間釘死，24 小時後必紅（4 班）
#   09-06  test_price_state 的出發日釘死在 2026-09-05，隔天變「昨天」（9 班）
#
# **必須用 faketime 而不是 freezegun。** freezegun 只騙 Python，SQLite 的
# date('now') 仍是真實時間；任何「Python 日期 vs SQL 日期」的比較都會假性
# 失敗。實測 freezegun 報 7 個炸彈，faketime 校正後只有 1 個是真的——
# 其餘 6 個是方法本身的偽陽性。
#
# 用法：
#   scripts/time_travel_tests.sh                 # 預設幾個時間點
#   scripts/time_travel_tests.sh 2027-03-01 ...  # 指定日期
#
# 何時跑：改動任何跟日期／新鮮度／額度窗口有關的程式或測試之後。
set -uo pipefail
command -v faketime >/dev/null 2>&1 || {
  echo "需要 faketime：apt-get install -y faketime" >&2; exit 2; }

DATES=("$@")
if [ ${#DATES[@]} -eq 0 ]; then
  DATES=("$(date -u +%F)" "$(date -u -d '+10 days' +%F)" "$(date -u -d '+30 days' +%F)"
         "$(date -u -d '+90 days' +%F)" "$(date -u -d '+180 days' +%F)"
         "$(date -u -d '+400 days' +%F)")
fi

fail=0
for d in "${DATES[@]}"; do
  line=$(faketime "$d 09:30:00" python -m pytest -q tests/ 2>&1 | tail -1)
  printf '  %s → %s\n' "$d" "$line"
  case "$line" in *failed*|*error*) fail=1 ;; esac
done
[ "$fail" -eq 0 ] && echo "OK：沒有隨時間腐爛的測試" \
                  || echo "有測試會隨時間腐爛——上面標 failed 的日期就是引爆日" >&2
exit "$fail"
