# -*- coding: utf-8 -*-
"""强制触发 _guard_context 裁剪，观察 Agent 消息序列是否被裁坏。"""
import os, sys, json
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import gateway as gw

BASE = "http://127.0.0.1:18080"
MODEL = {"ctx": 131072, "internal_port": 18080, "host": "127.0.0.1", "reasoning_mode": "auto"}


def p(t):
    print(t, flush=True)


def build_agent_history(n_rounds, user_at_end=True, user_at_start=False):
    msgs = [{"role": "system", "content": "你是编码助手，可用工具读写文件。"}]
    if user_at_start:
        msgs.append({"role": "user", "content": "帮我把这个项目的所有文件重构一遍。"})
    body = "def foo():\n    return 'x' * 50\n" * 60      # 每格 ~2KB
    for i in range(n_rounds):
        msgs.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": "c%d" % i, "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": "/src/m%d.py" % i})}}]})
        msgs.append({"role": "tool", "tool_call_id": "c%d" % i, "content": "文件 %d:\n" % i + body})
    if user_at_end:
        msgs.append({"role": "user", "content": "继续，把改动写入文件。"})
    return msgs


def analyze(tag, msgs, inp_tokens):
    p("-" * 72)
    p(tag)
    p("  原始: %d 条 | 角色序列 = %s" % (len(msgs), [m["role"] for m in msgs[:6]]))
    body = {"messages": [dict(m) for m in msgs], "max_tokens": 4096}
    nm, nmt, drop, trim = gw._guard_context(BASE, MODEL, body, inp_tokens, cap=131072)
    roles = [m.get("role") for m in nm]
    p("  trim=%s | dropped=%d | 裁剪后 %d 条 | new_max_tokens=%s" % (trim, drop, len(nm), nmt))
    p("  裁剪后角色序列 = %s" % (roles[:14] + (["..."] if len(roles) > 14 else [])))
    first = roles[0] if roles else None
    p("  ★ 首条 role = %s" % first)
    if first == "tool":
        p("     !! 危险：首条是 tool 消息，模板 line160 的 previtem 判断落空，不会补 <|im_start|>user → 格式错乱")
    if first not in ("system", "user") and first != "tool":
        p("     !! 注意：首条既非 system 也非 user")
    p("  含 user 消息 = %s" % any(r == "user" for r in roles))
    # 配对检查
    orphan, nores = 0, 0
    for i, m in enumerate(nm):
        if m.get("role") == "tool":
            prev = nm[i - 1] if i > 0 else None
            if not (prev and prev.get("role") == "assistant" and prev.get("tool_calls")):
                orphan += 1
        if m.get("role") == "assistant" and m.get("tool_calls"):
            nxt = nm[i + 1] if i + 1 < len(nm) else None
            if not (nxt and nxt.get("role") == "tool"):
                nores += 1
    p("  孤儿 tool 消息（前面没有 assistant.tool_calls）= %d" % orphan)
    p("  悬空 assistant.tool_calls（后面没有 tool 结果）= %d" % nores)
    if orphan or nores:
        p("     !! 工具调用配对被裁坏，上游可能报错或语义错乱")
    return nm


p("########## 强制触发裁剪（inp_tokens 设为 120000 > cap*0.6）##########")
p("")

# 形态1：Agent 典型 —— system + (assistant/tool)*N + user
m1 = build_agent_history(30, user_at_end=True)
analyze("形态1：system + (assistant/tool)×30 + user（Agent 典型）", m1, 120000)

# 形态2：user 在最前，之后全是 assistant/tool（长任务形态）
m2 = build_agent_history(30, user_at_end=False, user_at_start=True)
analyze("形态2：system + user + (assistant/tool)×30（长任务）", m2, 120000)

# 形态3：极端 —— 只有 tool，没有 user（模拟裁剪后残局）
m3 = [{"role": "system", "content": "sys"}]
for i in range(30):
    m3.append({"role": "assistant", "content": "", "tool_calls": [{
        "id": "c%d" % i, "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": "/x%d" % i})}}]})
    m3.append({"role": "tool", "tool_call_id": "c%d" % i, "content": "内容" * 1500})
analyze("形态3：system + (assistant/tool)×30（完全无 user）", m3, 120000)

# 形态4：单个巨大的 tool 结果（超过整个预算）
m4 = [{"role": "system", "content": "sys"},
      {"role": "user", "content": "读取大文件"},
      {"role": "assistant", "content": "", "tool_calls": [{
          "id": "c0", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
      {"role": "tool", "tool_call_id": "c0", "content": "X" * 400000}]
analyze("形态4：单条超长 tool 结果（40 万字符）", m4, 120000)

p("")
p("########## 真实请求：强制裁剪后再打上游，看是否 200 ##########")
import requests
S = requests.Session(); S.trust_env = False
KEY = "sk-REPLACE_ME"
for tag, mm in (("形态1", m1), ("形态2", m2)):
    try:
        r = S.post("http://127.0.0.1:8000/v1/chat/completions",
                   headers={"Authorization": "Bearer " + KEY}, timeout=600,
                   json={"model": "qwen3.8-27b", "messages": mm, "max_tokens": 512,
                         "chat_template_kwargs": {"enable_thinking": False}})
        j = r.json()
        ch = (j.get("choices") or [{}])[0]
        p("%s 真实请求 → HTTP %s | finish=%s | X-Ctx-Guard=%s | in=%s out=%s" % (
            tag, r.status_code, ch.get("finish_reason"), r.headers.get("X-Ctx-Guard"),
            (j.get("usage") or {}).get("prompt_tokens"), (j.get("usage") or {}).get("completion_tokens")))
        if "error" in j:
            p("   !! 上游错误: " + json.dumps(j["error"], ensure_ascii=False)[:400])
        else:
            p("   content=%r" % ((ch.get("message") or {}).get("content") or "")[:120])
    except Exception as e:
        p("%s 真实请求 → 异常 %r" % (tag, e))

p("")
p("DONE")
