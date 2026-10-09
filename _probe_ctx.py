# -*- coding: utf-8 -*-
"""直接向上游 llama-server 施压，探测真实可用上下文窗口（绕过网关裁剪）。"""
import os
import sys
import time

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)

import requests  # noqa: E402

B = "http://127.0.0.1:18080"
UNIT = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "


def ntok(s):
    r = requests.post(B + "/tokenize", json={"content": s}, timeout=(3, 180)).json()
    return len(r.get("tokens") or [])


def finish_endpoint():
    """探测可用的生成端点（新版 /completion 或 /v1/completions）。"""
    for path in ("/completion", "/v1/completions"):
        try:
            r = requests.post(B + path, json={"prompt": "hi", "n_predict": 1},
                              timeout=(5, 60))
            if r.status_code in (200, 400, 422):
                return path
        except Exception:
            pass
    return None


ep = finish_endpoint()
print("生成端点 =", ep)
u = ntok(UNIT)
print("单元 token 数 =", u)

print()
print("slot 现状:")
for s in requests.get(B + "/slots", timeout=(3, 30)).json():
    print("  ", {k: s.get(k) for k in ("id", "n_ctx", "is_processing")})

for target in (20000, 60000, 100000, 125000):
    k = max(1, target // u)
    txt = UNIT * k
    n = ntok(txt)
    t0 = time.time()
    try:
        r = requests.post(B + ep,
                          json={"prompt": txt, "n_predict": 4, "temperature": 0.0},
                          timeout=(60, 900))
        dt = time.time() - t0
        body = r.text[:200].replace("\n", " ")
        print("目标 %6d -> 实际 %6d tokens : HTTP %s  (%.0fs)  %s"
              % (target, n, r.status_code, dt, "" if r.status_code == 200 else body))
    except Exception as e:
        print("目标 %6d -> 实际 %6d tokens : 异常 %s (%.0fs)"
              % (target, n, str(e)[:100], time.time() - t0))
    sys.stdout.flush()
