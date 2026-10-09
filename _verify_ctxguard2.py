# -*- coding: utf-8 -*-
"""补测：user 消息集中在前部时，裁剪是否会把它整个丢掉。"""
import sys
import os

for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import gateway as gw  # noqa: E402

CAP = 131072
BASE = "http://127.0.0.1:18080"
MODEL = {"name": "qwen3.8-27b", "ctx": CAP, "host": "127.0.0.1", "internal_port": 18080}


def mk(role, chars):
    return {"role": role, "content": "中" * chars}


def raw(msgs):
    return sum(gw._est_tokens(gw._msg_text(m)) for m in msgs if isinstance(m, dict))


def run(tag, msgs, mt=16384):
    inp = raw(msgs)
    body = {"messages": msgs, "max_tokens": mt}
    nm, nmt, drop, trim = gw._guard_context(BASE, MODEL, body, inp, cap=CAP)
    roles = [m["role"] for m in nm]
    has_user = "user" in roles
    print("[%s] 原 %d 条 / 粗估 %d -> trimmed=%s drop=%d mt=%s" % (tag, len(msgs), inp, trim, drop, nmt))
    print("    裁剪后 %d 条，角色序列前6=%s 后6=%s" % (len(nm), roles[:6], roles[-6:]))
    print("    含 user: %s" % has_user)
    print("    总量 %d + %s = %d <= cap? %s" % (raw(nm), nmt, raw(nm) + (nmt or 0), raw(nm) + (nmt or 0) <= CAP))
    if not has_user:
        print("    >>> 危险：裁剪后没有 user，Bonsai2 模板会 500（Qwen 也可能异常）")
    if len(roles) >= 2 and roles[-1] == "assistant" and roles[-2] == "assistant":
        print("    >>> 注意：结尾有连续 assistant，部分客户端会报 400")
    print()


print("=" * 70)
print("A. user 集中在最前，后接大量长 assistant（长任务典型形态）")
run("A", [mk("system", 500), mk("user", 5000)] + [mk("assistant", 20000)] * 20)

print("=" * 70)
print("B. 两轮 user，后面全是超长 assistant")
run("B", [mk("system", 500), mk("user", 3000), mk("assistant", 3000),
          mk("user", 3000)] + [mk("assistant", 25000)] * 20)

print("=" * 70)
print("C. 正常交替但总量巨大（约 40 万 token）")
msgs = [mk("system", 800)]
for _ in range(200):
    msgs.append(mk("user", 1000))
    msgs.append(mk("assistant", 1000))
run("C", msgs)
