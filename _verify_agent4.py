# -*- coding: utf-8 -*-
"""对比：思考开/关 下工具调用的稳定性（重复 3 次）。"""
import os, json, time
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
import requests
S = requests.Session(); S.trust_env = False
GW = "http://127.0.0.1:8000/v1/chat/completions"
HDR = {"Authorization": "Bearer sk-REPLACE_ME"}
TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "读取文件内容",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_dir", "description": "列出目录内容",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
]
Q = "读取文件 /src/main.py 的内容。"


def run(think, n=3):
    res = []
    for i in range(n):
        try:
            t0 = time.time()
            r = S.post(GW, headers=HDR, timeout=600, json={
                "model": "qwen3.8-27b",
                "messages": [{"role": "user", "content": Q}],
                "tools": TOOLS, "tool_choice": "auto", "max_tokens": 1024,
                "chat_template_kwargs": {"enable_thinking": think},
            })
            dt = time.time() - t0
            j = r.json()
            ch = (j.get("choices") or [{}])[0]
            msg = ch.get("message") or {}
            tc = msg.get("tool_calls")
            res.append({
                "http": r.status_code, "dt": round(dt, 1),
                "finish": ch.get("finish_reason"),
                "has_tc": bool(tc),
                "fn": (tc[0]["function"]["name"] if tc else None),
                "reasoning_len": len(msg.get("reasoning_content") or ""),
                "content_head": (msg.get("content") or "")[:80],
                "ct": (j.get("usage") or {}).get("completion_tokens"),
            })
        except Exception as e:
            res.append({"err": repr(e)})
    return res


print("=" * 72)
print("思考关闭 enable_thinking=False")
print("=" * 72)
for i, x in enumerate(run(False), 1):
    print("  #%d %s" % (i, json.dumps(x, ensure_ascii=False)))

print()
print("=" * 72)
print("思考开启 enable_thinking=True")
print("=" * 72)
for i, x in enumerate(run(True), 1):
    print("  #%d %s" % (i, json.dumps(x, ensure_ascii=False)))
