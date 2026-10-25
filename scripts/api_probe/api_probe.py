"""臨時的免費機票 API 實測總控（只讀；跑完即刪，不進 main）。

每個 probe_<name>.py 依 INTERFACE.md 提供 probe(env, today) -> list[dict]。
這支負責：裝各模組宣告的 PIP_DEPS、逐一執行、把結果印成 log 可 grep 的
`PROBE_ROW {json}` 行與 GITHUB_STEP_SUMMARY 表格。任何模組出錯都不讓 job 失敗。
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

KEYS = ("provider", "case", "route", "ok", "results", "dates", "min_price",
        "currency", "carriers", "calls", "note")
SECRET_ENVS = ("TRAVELPAYOUTS_TOKEN", "SERPAPI_KEY", "IGNAV_API_KEY", "APIFY_TOKEN")


def _scrub(text: str, env: dict) -> str:
    for name in SECRET_ENVS:
        value = env.get(name)
        if value:
            text = text.replace(value, "***")
    return text


def _modules(only: list[str]) -> list[str]:
    names = sorted(p.stem for p in HERE.glob("probe_*.py"))
    return [n for n in names if not only or n.removeprefix("probe_") in only]


def main(argv: list[str]) -> int:
    # only.txt 讓重跑時只跑指定幾家（例如不再花 SerpAPI 額度）
    only = argv[1:]
    only_file = HERE / "only.txt"
    if not only and only_file.exists():
        only = only_file.read_text(encoding="utf-8").split()
    env = dict(os.environ)
    today = date.today()
    rows: list[dict] = []

    loaded = []
    for name in _modules(only):
        try:
            loaded.append((name, importlib.import_module(name)))
        except Exception as exc:  # noqa: BLE001 — 單一模組壞掉不影響其他家
            rows.append({k: None for k in KEYS} | {
                "provider": name, "case": "import", "route": "-", "ok": False,
                "calls": 0, "note": f"import failed: {type(exc).__name__}: {exc}"})

    deps = sorted({d for _, m in loaded for d in getattr(m, "PIP_DEPS", [])})
    if deps:
        print("pip install", " ".join(deps), flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", *deps],
                       check=False)

    for name, mod in loaded:
        started = time.monotonic()
        print(f"== {name}", flush=True)
        try:
            got = mod.probe(env, today)
        except Exception as exc:  # noqa: BLE001
            got = [{k: None for k in KEYS} | {
                "provider": name, "case": "probe()", "route": "-", "ok": False,
                "calls": 0, "note": f"probe raised: {type(exc).__name__}: {exc}"}]
        print(f"   {len(got)} rows in {time.monotonic() - started:.0f}s", flush=True)
        rows.extend(got)

    total_calls = 0
    lines = ["| provider | case | ok | results | dates | min | carriers | calls | note |",
             "|---|---|---|---|---|---|---|---|---|"]
    for row in rows:
        row = {k: row.get(k) for k in KEYS}
        text = _scrub(json.dumps(row, ensure_ascii=False), env)
        print("PROBE_ROW " + text, flush=True)
        row = json.loads(text)
        total_calls += row["calls"] or 0
        lines.append("| " + " | ".join(str(row[k]) if row[k] is not None else "" for k in (
            "provider", "case", "ok", "results", "dates", "min_price", "carriers",
            "calls", "note")).replace("\n", " ") + " |")
    print(f"PROBE_DONE rows={len(rows)} calls={total_calls}", flush=True)

    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"## API probe {today.isoformat()}\n\n" + "\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
