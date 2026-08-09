#!/bin/bash
# PreToolUse hook：在 Claude 執行 Bash 指令前攔截會造成不可逆損失的 git 操作。
#
# 這個專案要保護的具體東西是 prices.db——15MB 的二進位檔、被 git 追蹤、裝著
# 10 萬筆以上的價格觀測，而 .github/workflows/monitor.yml 每小時會 push 一次新
# 資料上 main。因此本機的 main 幾乎永遠落後遠端。二進位檔無法像文字檔那樣合併，
# 一次強制推送就會讓那幾小時的觀測永久消失（workflow 裡那段
# "NON-FAST-FORWARD: refusing to merge binary prices.db" 就是在防同一件事）。
#
# 客製化說明：上游範本連一般的 `git push` 都擋。這裡刻意放行——
# 快轉推送不會弄丟任何東西，全擋的話連正常交付都做不了，反而逼人去繞過保護。
# 擋的是「會覆寫或刪除既有內容」的那些變體。
#
# 來源：.claude/skills/git-guardrails-claude-code/scripts/block-dangerous-git.sh

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')
[ -z "$COMMAND" ] && exit 0

block () {
  echo "BLOCKED: 觸發 git 保護規則（$1）。" >&2
  echo "觸發的片段：$2" >&2
  echo "使用者已停用這個指令。請改用非破壞性的做法，或請使用者自己手動執行。" >&2
  exit 2
}

# 只檢查「真正的指令位置」，不是整條字串。
#
# 為什麼：第一版比對整條指令，結果連 commit 訊息裡提到 "git push --force"、
# 或測試腳本裡 echo 一個危險指令當測資，都會被誤擋——它分不出「執行」和
# 「提到」。這裡先把指令依 ; && || | 換行 切成片段，只對「開頭就是 git」
# 的片段套規則。誤擋降到接近零，真正的呼叫仍然一定落在片段開頭。
#
# 已知取捨：`bash -c "git push --force"` 這種把指令包在引號裡的寫法不會被
# 攔到。這是防手滑的閘門，不是防蓄意繞過的安全邊界，不為此犧牲可用性。
#
# `|| [ -n "$seg" ]` 與 printf 的結尾換行缺一不可：少了它們，最後一個（單行
# 指令時就是唯一一個）片段會被 read 當成 EOF 丟掉，迴圈一次都不跑，閘門
# 靜默地全部放行。這是安全機制最糟的失敗模式，故以 tests 釘住。
while IFS= read -r seg || [ -n "$seg" ]; do
  seg="${seg#"${seg%%[![:space:]]*}"}"          # 去掉開頭空白
  echo "$seg" | grep -qE '^(sudo[[:space:]]+)?git[[:space:]]' || continue

  # ---- 會覆寫／刪除遠端內容的 push ----
  if echo "$seg" | grep -qE '^(sudo[[:space:]]+)?git[[:space:]]+push([[:space:]]|$)'; then
    echo "$seg" | grep -qE -- '--force|--delete|(^|[[:space:]])-f([[:space:]]|$)' \
      && block "強制推送或刪除遠端分支" "$seg"
    echo "$seg" | grep -qE 'push[[:space:]]+[^[:space:]-][^[:space:]]*[[:space:]]+:' \
      && block "以 push origin :branch 刪除遠端分支" "$seg"
  fi

  # ---- 會丟掉本機內容的操作 ----
  echo "$seg" | grep -qE '^(sudo[[:space:]]+)?git[[:space:]]+reset[[:space:]]+.*--hard' \
    && block "reset --hard 會丟棄本機修改" "$seg"
  echo "$seg" | grep -qE '^(sudo[[:space:]]+)?git[[:space:]]+clean[[:space:]]+-[a-zA-Z]*f' \
    && block "clean -f 會刪除未追蹤的檔案" "$seg"
  echo "$seg" | grep -qE '^(sudo[[:space:]]+)?git[[:space:]]+branch[[:space:]]+.*-D' \
    && block "branch -D 會強制刪除分支" "$seg"
  echo "$seg" | grep -qE '^(sudo[[:space:]]+)?git[[:space:]]+(checkout|restore)[[:space:]]+\.([[:space:]]|$)' \
    && block "checkout . / restore . 會丟棄本機修改" "$seg"
done < <(printf '%s\n' "$COMMAND" | tr '\n;|&' '\n\n\n\n')

exit 0
