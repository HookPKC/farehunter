"""`.claude/hooks/zh_only.py` 的行為測試（強制用繁體中文回覆的 hook）。

為什麼值得放進 CI：這是一道閘門，最糟的失敗模式是「壞掉時全部放行」或「誤擋讓
Claude 卡住」——兩者都不會有人察覺。所以這裡同時測：英文會擋、中文會過、程式碼
不算英文、已經擋過一次就不再擋、壞掉的輸入一律放行，並用真實格式的對話紀錄跑。
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "zh_only.py"

ENGLISH = ("The first run after the merge went fine, and this time the cold routes had no "
           "wasted requests. The database was committed and passed its integrity check.")
CHINESE = ("合併後第一輪一切正常，冷門線這輪沒有白查。資料庫已存檔，完整性檢查通過，"
           "看板也部署了。KHH-NGO 與 KHH-CTS 都寫入了 Bright Data 的直飛價。")


def _run(mode, payload):
    proc = subprocess.run([sys.executable, str(HOOK), mode], text=True, capture_output=True,
                          input=payload if isinstance(payload, str) else json.dumps(payload))
    assert proc.returncode == 0                  # 任何情況都不能讓 Claude 卡住
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def _transcript(tmp_path, *entries):
    """照 Claude Code 對話紀錄的格式：每個內容區塊一行，使用者訊息與工具結果都是 user。"""
    path = tmp_path / "t.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"type":"summary","summary":"x"}\n半行壞掉的紀錄\n')
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return str(path)


def _prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _say(text):
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


def _tool():
    return [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "1"}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}]


def _check(tmp_path, *entries, active=False):
    return _run("check", {"transcript_path": _transcript(tmp_path, *entries),
                          "stop_hook_active": active})


def test_an_english_reply_is_sent_back_for_a_chinese_rewrite(tmp_path):
    out = _check(tmp_path, _prompt("檢查一下"), *_tool(), _say(ENGLISH))
    assert out["decision"] == "block" and "繁體中文" in out["reason"]


def test_a_chinese_reply_passes(tmp_path):
    assert _check(tmp_path, _prompt("檢查一下"), *_tool(), _say(CHINESE)) is None


def test_code_urls_and_paths_do_not_count_as_english(tmp_path):
    reply = (CHINESE + "\n```python\n" + ENGLISH * 5 + "\n```\n"
             "詳見 `src/farehunter/brightdata_flights.py` 與 https://github.com/HookPKC/farehunter/pull/10")
    assert _check(tmp_path, _prompt("改好了嗎"), _say(reply)) is None


def test_only_the_last_reply_is_judged(tmp_path):
    """前面說了中文、最後一則卻是英文：照樣要擋（之前就是最後的回報滑回英文）。"""
    out = _check(tmp_path, _prompt("檢查一下"), _say(CHINESE * 4), *_tool(), _say(ENGLISH))
    assert out["decision"] == "block"
    # 反過來：前面工具之間零星一句英文、最後是中文 → 放行
    assert _check(tmp_path, _prompt("檢查一下"), _say("Checking the logs."), *_tool(),
                  _say(CHINESE)) is None


def test_long_english_narration_between_tools_is_caught(tmp_path):
    out = _check(tmp_path, _prompt("做"), _say(ENGLISH * 3), *_tool(), _say(ENGLISH * 2),
                 *_tool(), _say(CHINESE))
    assert out["decision"] == "block" and "說明" in out["reason"]


def test_a_second_stop_in_the_same_turn_is_never_blocked(tmp_path):
    """Claude 被擋過一次後還是英文：不再擋，免得無限迴圈。"""
    assert _check(tmp_path, _prompt("檢查一下"), _say(ENGLISH), active=True) is None


def test_short_english_and_system_notices_pass(tmp_path):
    assert _check(tmp_path, _prompt("好"), _say("Done.")) is None
    assert _check(tmp_path, _prompt("好"),
                  _say("You've hit your session limit · resets 3:10pm (UTC)")) is None


def test_broken_input_always_passes(tmp_path):
    assert _run("check", "not json") is None
    assert _run("check", {}) is None
    assert _run("check", {"transcript_path": str(tmp_path / "missing.jsonl")}) is None
    assert _run("check", "[1, 2]") is None


def test_reminder_goes_into_the_context_for_each_event():
    for event in ("UserPromptSubmit", "SessionStart"):
        out = _run("remind", {"hook_event_name": event})
        assert out["hookSpecificOutput"]["hookEventName"] == event
        assert "繁體中文" in out["hookSpecificOutput"]["additionalContext"]


def test_settings_wire_the_hooks_up():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hooks = settings["hooks"]
    commands = {event: [h["command"] for m in hooks[event] for h in m["hooks"]]
                for event in ("UserPromptSubmit", "SessionStart", "Stop")}
    assert all("zh_only.py remind" in c for c in commands["UserPromptSubmit"])
    assert all("zh_only.py remind" in c for c in commands["SessionStart"])
    assert all("zh_only.py check" in c for c in commands["Stop"])
    assert "compact" in hooks["SessionStart"][0]["matcher"]
    assert hooks["PreToolUse"][0]["matcher"] == "Bash"      # 既有的 git 保護還在
    assert "chinese" in settings["language"]
