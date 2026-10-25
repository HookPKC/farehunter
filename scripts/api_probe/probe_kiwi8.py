"""Kiwi 公開 MCP 交叉比對：config.yaml 全部 8 條航線，同一天出發（today+43、住 5 晚、±3 天彈性）。
沿用 probe_kiwimcp 的握手與解析，只把航線換成 8 條。共 2 + 8 = 10 次請求。"""
from __future__ import annotations

import probe_kiwimcp
from probe_kiwimcp import PIP_DEPS  # noqa: F401

ROUTES = ["TPE-NRT", "TPE-KIX", "KHH-NRT", "KHH-KIX", "KHH-CTS", "KHH-FUK", "KHH-OKA", "KHH-NGO"]


def probe(env, today):
    probe_kiwimcp.CASES = [(r, probe_kiwimcp.DEPART_DAYS) for r in ROUTES]
    return probe_kiwimcp.probe(env, today)
