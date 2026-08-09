"""`.claude/hooks/block-dangerous-git.sh` 的行為測試。

為什麼這個 hook 值得放進 CI：它保護的是 prices.db——15MB 二進位檔、被 git
追蹤、monitor.yml 每小時 push 一次新觀測上 main，所以本機 main 幾乎永遠落後
遠端。二進位檔無法合併，一次強制推送就會讓那幾小時的觀測永久消失。

而安全閘門有一個特別惡劣的失敗模式：**壞掉時全部放行**。開發過程中真的發生
過一次——`printf '%s'` 少了結尾換行，`while read` 把唯一的片段當成 EOF 丟掉，
迴圈一次都沒跑，每一條危險指令都被放行，而且完全無聲。單看「沒有東西被擋」
是看不出閘門已經死掉的。因此這裡不只測「該擋的有擋」，也測「該放的有放」，
並且對那個具體的失敗模式留一條回歸測試。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".claude" / "hooks" / "block-dangerous-git.sh"
CASES_FILE = Path(__file__).parent / "fixtures" / "git_guardrail_cases.jsonl"

BLOCK_EXIT = 2          # Claude Code 的 PreToolUse 約定：exit 2 = 擋下

pytestmark = pytest.mark.skipif(
    not HOOK.exists() or shutil.which("jq") is None,
    reason="需要 hook 腳本與 jq",
)


def _run(command: str) -> int:
    """把一條指令餵給 hook，回傳結束碼。"""
    payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(HOOK)], input=payload, text=True,
        capture_output=True,
    ).returncode


def _cases():
    with CASES_FILE.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                c = json.loads(line)
                yield pytest.param(c["command"], c["expect"],
                                   id=c["command"][:48].replace("\n", "⏎"))


@pytest.mark.parametrize("command,expect", list(_cases()))
def test_hook_verdict(command, expect):
    code = _run(command)
    got = "block" if code == BLOCK_EXIT else "pass"
    assert got == expect, f"指令 {command!r} 預期 {expect}，實得 {got}（exit {code}）"


def test_gate_is_not_silently_open():
    """回歸：單行指令（結尾無換行）必須被擋。

    這正是那次 fail-open 的形狀——迴圈讀不到唯一的片段就直接結束，
    腳本回 0，看起來一切正常，實際上什麼都沒在擋。
    """
    assert _run("git reset --hard HEAD~5") == BLOCK_EXIT


def test_gate_blocks_at_least_something():
    """整份測資裡必須真的有被擋下的案例。

    如果哪天有人把樣式全刪光，上面的參數化測試會因為預期值也被改掉而
    無聲通過；這條確保「閘門完全失效」一定會變紅。
    """
    blocked = sum(1 for c, e in ((p.values[0], p.values[1]) for p in _cases())
                  if e == "block" and _run(c) == BLOCK_EXIT)
    assert blocked >= 10, f"只有 {blocked} 條指令被擋下，閘門可能已失效"
