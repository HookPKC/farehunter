#!/usr/bin/env python3
"""Claude Code hook：強制用繁體中文回覆使用者。

為什麼有這支：使用者一再要求只用中文，Claude 還是在對話壓縮後、排程檢查醒來、
或跑完一長串工具之後滑回英文（2026-10-31～11-02 至少 5 次）。靠記憶不可靠，所以：

- `remind`（UserPromptSubmit／SessionStart）：每則訊息、開新對話、對話壓縮後，
  在 context 裡放一句提醒。約 30 個 token。
- `check`（Stop）：回覆完讀對話紀錄的最後一則回覆，去掉程式碼、網址、路徑後，
  英文字母遠多於中文字就擋下，要 Claude 用中文重寫。檢查在本機跑，不花 token；
  只有擋下時多一輪重寫。

失敗一律放行（exit 0、不輸出）：這支壞掉不能讓 Claude 卡住。
"""
from __future__ import annotations

import json
import re
import sys

REMINDER = ("提醒：一律用繁體中文回覆使用者（包括工具呼叫之間的說明、排程檢查的回報）；"
            "程式碼、指令、檔名、專有名詞可以保留原文。")

#: 英文字母至少這麼多、而且超過中文字的這個倍數，才算「主要是英文」。
#: 中文回覆裡夾的航線代碼、服務名稱（KHH-NGO、Bright Data）遠少於中文字，不會誤判。
MIN_LATIN = 40
LATIN_PER_CJK = 2.0
#: 工具呼叫之間的說明加起來，英文要到這麼多才擋（零星一兩句不擋）
MIN_LATIN_NARRATION = 200
#: 對話紀錄可能有幾十 MB，只讀結尾這麼多
TAIL_BYTES = 2_000_000

_FENCED = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`\n]*`")
_URL = re.compile(r"https?://\S+")
_LINK_TARGET = re.compile(r"\]\([^)]*\)")
_PATHISH = re.compile(r"\S*[/\\]\S*")
_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_LATIN = re.compile(r"[A-Za-z]")


def counts(text: str) -> tuple[int, int]:
    """(中文字數, 英文字母數)，不算程式碼、網址、連結目標、路徑。"""
    for pattern, repl in ((_FENCED, " "), (_INLINE, " "), (_URL, " "),
                          (_LINK_TARGET, "]"), (_PATHISH, " ")):
        text = pattern.sub(repl, text)
    return len(_CJK.findall(text)), len(_LATIN.findall(text))


def mostly_english(text: str, min_latin: int = MIN_LATIN) -> bool:
    cjk, latin = counts(text)
    return latin >= min_latin and latin > LATIN_PER_CJK * cjk


def _entries(path: str) -> list[dict]:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        raw = f.read().decode("utf-8", errors="replace")
    lines = raw.splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]                       # 第一行多半被切掉一半
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _texts(entry: dict) -> list[str]:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text") or "" for b in content
                if isinstance(b, dict) and b.get("type") == "text"]
    return []


def _is_prompt(entry: dict) -> bool:
    """使用者自己送出的訊息（不是工具結果、不是系統塞進來的說明）。"""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "text" for b in content)


def last_turn(entries: list[dict]) -> tuple[str, str]:
    """(最後一則回覆, 這一輪其他說明)：最後一則＝最後一筆 user（含工具結果）之後的文字；
    這一輪＝使用者上一則訊息之後、最後一則之前的文字。"""
    final, narration, in_final = [], [], True
    for entry in reversed(entries):
        kind = entry.get("type")
        if kind == "assistant":
            (final if in_final else narration).extend(reversed(_texts(entry)))
        elif kind == "user":
            if _is_prompt(entry):
                break
            in_final = False
    return "\n".join(reversed(final)), "\n".join(reversed(narration))


def check(payload: dict) -> dict | None:
    if payload.get("stop_hook_active"):
        return None                              # 已經擋過一次：不再擋，免得無限迴圈
    path = payload.get("transcript_path")
    if not path:
        return None
    final, narration = last_turn(_entries(path))
    if mostly_english(final):
        cjk, latin = counts(final)
        return {"decision": "block", "reason": (
            f"剛才的最後一則回覆主要是英文（英文字母 {latin} 個、中文字 {cjk} 個）。"
            "使用者要求一律用繁體中文：請用繁體中文把那則回覆完整重寫一次，"
            "不用道歉或解釋這個提醒。")}
    if mostly_english(narration, MIN_LATIN_NARRATION):
        return {"decision": "block", "reason": (
            "這一輪工具呼叫之間的說明大多是英文。使用者要求一律用繁體中文："
            "請用繁體中文簡短補述剛才說明裡重要的內容，之後的說明也都用中文。")}
    return None


def remind(payload: dict) -> dict:
    event = payload.get("hook_event_name") or "UserPromptSubmit"
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": REMINDER}}


def main(argv: list[str]) -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
        result = check(payload) if argv[1:] == ["check"] else remind(payload)
        if result:
            print(json.dumps(result, ensure_ascii=False))
    except Exception:                            # 壞掉一律放行，不能讓 Claude 卡住
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
