"""不需帳號就能確認的部分：Kiwi Tequila、Amadeus Self-Service、Duffel、
Skyscanner 的 API 端點還活不活、無金鑰時回什麼。

這幾家都要先註冊（或已停止服務），所以這裡只看「端點狀態」，不是價格。
每家 1–2 次請求，總共 ≤ 8 次。
"""
from __future__ import annotations

import time

import requests

PIP_DEPS: list[str] = []

CHECKS = [
    # (provider, case, method, url, kwargs, 解讀提示)
    ("kiwi_tequila", "search API without key", "GET",
     "https://api.tequila.kiwi.com/v2/search",
     {"params": {"fly_from": "KHH", "fly_to": "NGO"}},
     "401/403 = API alive but needs key"),
    ("kiwi_tequila", "partner portal page", "GET",
     "https://tequila.kiwi.com/portal/login", {},
     "look for signup wording"),
    ("amadeus_self_service", "oauth token with dummy creds", "POST",
     "https://test.api.amadeus.com/v1/security/oauth2/token",
     {"data": {"grant_type": "client_credentials",
               "client_id": "x", "client_secret": "x"}},
     "401 invalid_client = alive; DNS fail/404/410 = decommissioned"),
    ("amadeus_self_service", "developer portal page", "GET",
     "https://developers.amadeus.com/register", {}, ""),
    ("duffel", "API without token", "GET",
     "https://api.duffel.com/air/airlines", {"headers": {"Duffel-Version": "v2"}},
     "401 = alive; real fares need a live-mode account"),
    ("skyscanner", "live search without key", "POST",
     "https://partners.api.skyscanner.net/apiservices/v3/flights/live/search/create",
     {"json": {}}, "401/403 = alive but gated"),
]

SIGNUP_WORDS = ("sign up", "signup", "register", "create account", "get started")


def probe(env, today):
    rows = []
    for provider, case, method, url, kwargs, hint in CHECKS:
        row = {"provider": provider, "case": case, "route": "-", "ok": False,
               "results": None, "dates": None, "min_price": None,
               "currency": None, "carriers": None, "calls": 1, "note": ""}
        try:
            resp = requests.request(method, url, timeout=20, allow_redirects=True,
                                    **kwargs)
            body = resp.text[:2000]
            row["ok"] = True
            words = [w for w in SIGNUP_WORDS if w in body.lower()]
            row["note"] = (f"HTTP {resp.status_code} final={resp.url.split('?')[0]} "
                           f"body={body[:160]!r}"
                           + (f" signup_words={words}" if words else "")
                           + (f" | {hint}" if hint else ""))[:300]
        except Exception as exc:  # noqa: BLE001
            row["note"] = f"{type(exc).__name__}: {exc}"[:300]
        rows.append(row)
        time.sleep(0.6)
    return rows
