# -*- coding: utf-8 -*-
"""Agent 场景深度验证：思考+tools 共存；超长工具历史触发裁剪后的行为。"""
import os, json, time, sys
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
S = requests.Session(); S.trust_env = False
GW = "http://127.0.0.1:8000/v1/chat/completions"
KEY = "sk-REPLACE_ME"
HDR = {"Authorization": "Bearer " + KEY}
MODEL = "qwen3.8-27b"
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "读取文件内容",
    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


def p(t):
    print(t, flush=True)


p("=" * 72)
p("[A] 思考开启 + tools 共存（Agent 默认配置）")
p("=" * 72)
try:
    t0 = time.time()
    r = S.post(GW, headers=HDR, timeout=600, json={
        "model": MODEL,
        "messages": [{"role": "user", "content": "读取 /etc/hosts 文件。请调用工具。"}],
        "tools": TOOLS, "tool_choice": "auto", "max_tokens": 1024,
        "chat_template_kwargs": {"enable_thinking": True},
    })
    dt = time.time() - t0
    p("HTTP %s | %.1fs | X-Think-Headroom=%s" % (r.status_code, dt, r.headers.get("X-Think-Headroom")))
    j = r.json()
    if "error" in j:
        p("!! 上游错误: " + json.dumps(j["error"], ensure_ascii=False)[:500])
    else:
        ch = (j.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        p("finish_reason = %s" % ch.get("finish_reason"))
        p("tool_calls    = %s" % json.dumps(msg.get("tool_calls"), ensure_ascii=False)[:300])
        p("reasoning 长度= %s 字符" % len(msg.get("reasoning_content") or ""))
        p("usage         = %s" % j.get("usage"))
except Exception as e:
    p("!! 异常: %r" % (e,))

p("")
p("=" * 72)
p("[B] 超长工具历史 → 触发网关裁剪，检查是否报错 / 消息序列是否被裁坏")
p("=" * 72)
# 构造 ~20 轮 assistant(tool_calls) + tool(长内容) 的 Agent 典型历史
big = "文件内容行：" + ("ABCDEFGHIJ" * 200)   # ≈ 2000 字符
msgs = [{"role": "system", "content": "你是一个代码助手，可以使用工具读取文件。"}]
for i in range(22):
    msgs.append({"role": "assistant", "content": "", "tool_calls": [{
        "id": "call_%d" % i, "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"path": "/src/f%d.py" % i})}}]})
    msgs.append({"role": "tool", "tool_call_id": "call_%d" % i, "content": "第%d个文件\n" % i + big})
msgs.append({"role": "user", "content": "总结一下这些文件都做了什么。"})

est = sum(len(str(m.get("content") or "")) for m in msgs)
p("构造消息 %d 条 | content 总字符 ≈ %d | 粗估 token ≈ %d" % (len(msgs), est, est // 2))
p("消息角色序列（首 8 / 尾 8）:")
p("  首: %s" % [m["role"] for m in msgs[:8]])
p("  尾: %s" % [m["role"] for m in msgs[-8:]])

try:
    t0 = time.time()
    r = S.post(GW, headers=HDR, timeout=900, json={
        "model": MODEL, "messages": msgs, "tools": TOOLS,
        "max_tokens": 1024, "chat_template_kwargs": {"enable_thinking": False},
    })
    dt = time.time() - t0
    p("")
    p("HTTP %s | %.1fs" % (r.status_code, dt))
    p("X-Ctx-Guard   = %s" % r.headers.get("X-Ctx-Guard"))
    j = r.json()
    if "error" in j:
        p("!! 上游错误: " + json.dumps(j["error"], ensure_ascii=False)[:800])
    else:
        ch = (j.get("choices") or [{}])[0]
        p("finish_reason = %s" % ch.get("finish_reason"))
        p("content = %r" % ((ch.get("message") or {}).get("content") or "")[:200])
        p("usage   = %s" % j.get("usage"))
except Exception as e:
    p("!! 异常: %r" % (e,))

p("")
p("=" * 72)
p("[C] 直接观察 _guard_context 裁剪后的消息序列形态")
p("=" * 72)
try:
    from app import gateway as gw
    body = {"messages": msgs, "tools": TOOLS, "max_tokens": 1024}
    # 用真实精确计数（和线上路径一致）
    base = "http://127.0.0.1:18080"
    model = {"ctx": 131072, "internal_port": 18080, "host": "127.0.0.1", "reasoning_mode": "auto"}
    n = gw._count_prompt_tokens(base, model, msgs, TOOLS)
    p("精确输入 token（含 tools 估算补齐）= %s" % n)
    nm, nmt, drop, trim = gw._guard_context(base, model, dict(body), n, cap=131072)
    p("trim=%s dropped=%d new_max_tokens=%s" % (trim, drop, nmt))
    p("裁剪后条数 = %d" % len(nm))
    p("裁剪后角色序列 = %s" % [m.get("role") for m in nm])
    p("裁剪后首条 role = %s  (★ 若是 tool 则模板会格式错乱)" % (nm[0].get("role") if nm else None))
    p("裁剪后是否含 user = %s" % any(m.get("role") == "user" for m in nm))
    # 检查 tool 消息是否有前置 assistant(tool_calls)
    orphan_tool = 0
    toolcall_without_result = 0
    for i, m in enumerate(nm):
        if m.get("role") == "tool":
            prev = nm[i-1] if i > 0 else None
            if not (prev and prev.get("role") == "assistant" and prev.get("tool_calls")):
                orphan_tool += 1
        if m.get("role") == "assistant" and m.get("tool_calls"):
            nxt = nm[i+1] if i+1 < len(nm) else None
            if not (nxt and nxt.get("role") == "tool"):
                toolcall_without_result += 1
    p("孤儿 tool 消息（无前置 assistant.tool_calls）= %d" % orphan_tool)
    p("无结果的 assistant.tool_calls = %d" % toolcall_without_result)
except Exception as e:
    import traceback; p("!! 异常: %r" % (e,)); traceback.print_exc()

p("")
p("DONE")
