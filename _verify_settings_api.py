# -*- coding: utf-8 -*-
"""验证系统设置「思考预留」接口（含合法/非法取值与回读一致性），测试后恢复原值。"""
import json
import os
import sys
import time

for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import admin, store  # noqa: E402

orig = dict(store.get_general_settings())
print("原始设置:", json.dumps(orig, ensure_ascii=False))

users = (store.get_users() or {}).get("users") or []
adm = next((u for u in users if u.get("role") == "admin"), None)
assert adm, "找不到管理员账号"
admin._SESSIONS["verifytoken"] = {"uid": adm["id"], "role": "admin", "exp": time.time() + 600}

cli = admin.app.test_client()
H = {"Authorization": "Bearer verifytoken"}


def call(payload):
    r = cli.post("/api/settings/general", json=payload, headers=H)
    return r.status_code, r.get_json()


ok = True
for val in (2048, 0, 4096, 32768):
    code, d = call({"thinking_headroom": val})
    back = store.get_general_settings().get("thinking_headroom")
    good = (code == 200 and back == val)
    ok = ok and good
    print("  设 %-6s -> HTTP %s  回读=%s  %s" % (val, code, back, "OK" if good else "FAIL"))

for val in (-1, 99999, "abc"):
    code, d = call({"thinking_headroom": val})
    good = (code != 200)
    ok = ok and good
    print("  非法 %-6s -> HTTP %s  %s  %s" % (val, code,
          (d or {}).get("error", "")[:40], "OK(已拦截)" if good else "FAIL(未拦截)"))

# 顺带回归：原有思考策略字段仍可写
code, d = call({"reasoning_allow_user_override": True, "reasoning_default_mode": "inherit"})
print("  回归写思考策略 -> HTTP %s %s" % (code, "OK" if code == 200 else "FAIL"))
ok = ok and code == 200

# 恢复原始设置
store.set_general_settings(orig)
print("已恢复:", json.dumps(store.get_general_settings(), ensure_ascii=False))
print("\n结果:", "全部通过" if ok else "存在失败项")
