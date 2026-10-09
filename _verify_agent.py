# -*- coding: utf-8 -*-
"""Agent 场景专项验证：工具调用是否端到端可用（非流式 / 流式 / 多轮回传）。"""
import os, json, time, sys
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)

import requests
S = requests.Session(); S.trust_env = False

GW = "http://127.0.0.1:8000/v1/chat/completions"
KEY = "sk-REPLACE_ME"
HDR = {"Authorization": "Bearer " + KEY}
MODEL = "qwen3.8-27b"

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的实时天气",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名称，例如 北京"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city"],
        },
    },
}]

Q = "北京现在天气怎么样？请调用工具查询。"


def p(t):
    print(t, flush=True)


p("=" * 70)
p("[1] 非流式 + tools → 期望返回 tool_calls")
p("=" * 70)
try:
    t0 = time.time()
    r = S.post(GW, headers=HDR, timeout=300, json={
        "model": MODEL,
        "messages": [{"role": "user", "content": Q}],
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": 512,
        "chat_template_kwargs": {"enable_thinking": False},
    })
    dt = time.time() - t0
    p("HTTP %s | %.1fs | X-Think-Headroom=%s" % (r.status_code, dt, r.headers.get("X-Think-Headroom")))
    j = r.json()
    if "error" in j:
        p("!! 上游错误: " + json.dumps(j["error"], ensure_ascii=False)[:400])
    ch = (j.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    p("finish_reason = %s" % ch.get("finish_reason"))
    p("tool_calls    = %s" % json.dumps(msg.get("tool_calls"), ensure_ascii=False)[:500])
    p("content       = %r" % (msg.get("content") or "")[:300])
    p("usage         = %s" % j.get("usage"))
except Exception as e:
    p("!! 异常: %r" % (e,))

p("")
p("=" * 70)
p("[2] 流式 + tools → 期望 SSE 分片里出现 tool_calls，且拼接后为合法 JSON")
p("=" * 70)
try:
    t0 = time.time()
    r = S.post(GW, headers=HDR, timeout=300, stream=True, json={
        "model": MODEL,
        "messages": [{"role": "user", "content": Q}],
        "tools": TOOLS,
        "tool_choice": "auto",
        "max_tokens": 512,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
    })
    p("HTTP %s | %s" % (r.status_code, r.headers.get("Content-Type")))
    if r.status_code != 200:
        p("!! 响应体: " + r.text[:400])
    n_data = 0
    n_toolfrag = 0
    name = None
    args_buf = ""
    finish = None
    usage = None
    raw_samples = []
    for line in r.iter_lines(decode_unicode=True):
        if not line:
            continue
        if not line.startswith("data: "):
            if len(raw_samples) < 3:
                raw_samples.append("非data行: " + line[:120])
            continue
        payload = line[6:]
        if payload.strip() == "[DONE]":
            break
        n_data += 1
        try:
            o = json.loads(payload)
        except Exception:
            if len(raw_samples) < 5:
                raw_samples.append("解析失败: " + payload[:120])
            continue
        if o.get("usage"):
            usage = o["usage"]
        for c in (o.get("choices") or []):
            if c.get("finish_reason"):
                finish = c["finish_reason"]
            d = c.get("delta") or {}
            for tc in (d.get("tool_calls") or []):
                n_toolfrag += 1
                if tc.get("function", {}).get("name"):
                    name = tc["function"]["name"]
                if tc.get("function", {}).get("arguments"):
                    args_buf += tc["function"]["arguments"]
    dt = time.time() - t0
    p("耗时 %.1fs | data 帧数=%d | tool_calls 分片数=%d" % (dt, n_data, n_toolfrag))
    p("finish_reason = %s" % finish)
    p("函数名        = %s" % name)
    p("参数拼接      = %r" % args_buf[:300])
    if args_buf:
        try:
            json.loads(args_buf)
            p("参数 JSON 校验 = 合法 ✅")
        except Exception as e:
            p("参数 JSON 校验 = !! 非法: %r" % (e,))
    p("usage         = %s" % usage)
    for s in raw_samples:
        p("  sample> " + s)
except Exception as e:
    p("!! 异常: %r" % (e,))

p("")
p("=" * 70)
p("[3] 多轮：回传 tool 结果 → 期望模型基于结果给出自然语言答复")
p("=" * 70)
try:
    msgs = [
        {"role": "user", "content": Q},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "get_weather", "arguments": json.dumps({"city": "北京"}, ensure_ascii=False)},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": json.dumps(
            {"city": "北京", "temp_c": 21, "weather": "晴", "wind": "西北风3级"}, ensure_ascii=False)},
    ]
    t0 = time.time()
    r = S.post(GW, headers=HDR, timeout=300, json={
        "model": MODEL, "messages": msgs, "tools": TOOLS,
        "max_tokens": 512, "chat_template_kwargs": {"enable_thinking": False},
    })
    dt = time.time() - t0
    p("HTTP %s | %.1fs" % (r.status_code, dt))
    j = r.json()
    if "error" in j:
        p("!! 上游错误: " + json.dumps(j["error"], ensure_ascii=False)[:600])
    else:
        ch = (j.get("choices") or [{}])[0]
        p("finish_reason = %s" % ch.get("finish_reason"))
        p("content = %r" % ((ch.get("message") or {}).get("content") or "")[:400])
except Exception as e:
    p("!! 异常: %r" % (e,))

p("")
p("DONE")
