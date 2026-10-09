# -*- coding: utf-8 -*-
"""验证网关 _guard_context 在接近/超过窗口时的行为（不真发请求）。"""
import sys
import os

for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import gateway as gw  # noqa: E402

CAP = 131072
BASE = "http://127.0.0.1:18080"
MODEL = {"name": "qwen3.8-27b", "ctx": CAP, "alias": "qwen3.8-27b",
         "host": "127.0.0.1", "internal_port": 18080}


def mk(role, chars):
    return {"role": role, "content": "中" * chars}


def raw_tokens(msgs):
    return sum(gw._est_tokens(gw._msg_text(m)) for m in msgs if isinstance(m, dict))


def show(tag, msgs, mt, inp):
    body = {"messages": msgs, "max_tokens": mt}
    nm, nmt, drop, trim = gw._guard_context(BASE, MODEL, body, inp, cap=CAP)
    after = raw_tokens(nm)
    roles = [m["role"] for m in nm]
    has_user = "user" in roles
    total = after + (nmt or 0)
    print("  [%s] 原 %d 条 / 粗估 %d" % (tag, len(msgs), raw_tokens(msgs)))
    print("      -> trimmed=%s dropped=%d 新 mt=%s" % (trim, drop, nmt))
    print("      -> 裁剪后 %d 条 roles=%s" % (len(nm), roles))
    print("      -> 含 user? %s | 裁剪后估算 %d + mt %s = %d <= cap? %s"
          % (has_user, after, nmt, total, total <= CAP))
    if not has_user:
        print("      !! 裁剪后没有任何 user 消息 —— Bonsai2 模板会 raise_exception('No user query found')")
    return has_user, total <= CAP


print("=" * 70)
print("场景 1：常规多轮（system + 40轮 user/assistant），输入接近窗口")
msgs = [mk("system", 500)]
for _ in range(40):
    msgs.append(mk("user", 1500))
    msgs.append(mk("assistant", 1500))
show("常规", msgs, 16384, raw_tokens(msgs))

print()
print("=" * 70)
print("场景 2：结尾连续 assistant（模拟工具调用/多步推理后的续写）")
msgs2 = [mk("system", 500)]
for _ in range(30):
    msgs2.append(mk("user", 2000))
    msgs2.append(mk("assistant", 2000))
msgs2 += [mk("assistant", 3000), mk("assistant", 3000), mk("assistant", 3000)]
show("尾部assistant", msgs2, 16384, raw_tokens(msgs2))

print()
print("=" * 70)
print("场景 3：极端超限（输入约 20 万 token，远超 131072）")
msgs3 = [mk("system", 800)]
for _ in range(120):
    msgs3.append(mk("user", 900))
    msgs3.append(mk("assistant", 900))
show("极端超限", msgs3, 16384, raw_tokens(msgs3))

print()
print("=" * 70)
print("场景 4：只有 system + assistant（客户端消息序列异常）")
msgs4 = [mk("system", 500)] + [mk("assistant", 2000)] * 40
show("无user", msgs4, 16384, raw_tokens(msgs4))

print()
print("=" * 70)
print("场景 5：单条超长（一条 user 就 15 万 token）")
msgs5 = [mk("system", 500), mk("user", 150000)]
show("单条超长", msgs5, 16384, raw_tokens(msgs5))
