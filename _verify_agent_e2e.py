# -*- coding: utf-8 -*-
"""P1 修复的端到端验证：走真实网关 http://127.0.0.1:8000/v1。

覆盖：
  A. 基础工具调用（agent 单轮）
  B. 工具结果回传后的第二轮 ReAct（验证 tool 消息配对没被破坏）
  C. 超窗长历史：网关裁剪后必须 200，而不是 400/500
"""
import json
import os
import sys
import time

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)

import requests  # noqa: E402

GW = "http://127.0.0.1:8000/v1/chat/completions"
KEY = sys.argv[1] if len(sys.argv) > 1 else None
if not KEY:
    ks = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "data", "keys.json"), encoding="utf-8"))
    ks = ks if isinstance(ks, list) else ks.get("keys", [])
    for k in ks:
        if k.get("name") == "本机":
            KEY = k.get("token") or k.get("key")
            break
H = {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"}
MODEL = "Qwen3.8-27B"

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  :: " + detail) if detail else ""))


TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取本地文件的完整内容",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "文件绝对路径"}},
                       "required": ["path"]},
    },
}, {
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "列出目录下的文件",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string", "description": "目录绝对路径"}},
                       "required": ["path"]},
    },
}]


def post(payload, timeout=300):
    t0 = time.time()
    r = requests.post(GW, headers=H, json=payload, timeout=timeout)
    dt = time.time() - t0
    return r, dt


print("=" * 74)
print("A. 基础工具调用（agent 单轮）")
print("=" * 74)
p1 = {"model": MODEL, "max_tokens": 1024, "stream": False, "tools": TOOLS,
      "messages": [{"role": "user", "content": "请调用 read_file 工具读取 /proj/main.py 的内容。"}]}
r1, d1 = post(p1)
print("  HTTP %s  用时 %.1fs" % (r1.status_code, d1))
print("  X-Think-Headroom:", r1.headers.get("X-Think-Headroom"))
check("返回 200", r1.status_code == 200, "code=%s" % r1.status_code)
j1 = {}
try:
    j1 = r1.json()
except Exception:
    pass
if r1.status_code != 200:
    print("  body:", r1.text[:400])
ch = (j1.get("choices") or [{}])[0]
msg = ch.get("message") or {}
tcs = msg.get("tool_calls") or []
usage = j1.get("usage") or {}
print("  finish_reason=%s  tool_calls=%d  content_len=%d  usage=%s"
      % (ch.get("finish_reason"), len(tcs), len(msg.get("content") or ""), usage))
if tcs:
    print("  调用:", [(t.get("function") or {}).get("name") for t in tcs])
    print("  参数:", [(t.get("function") or {}).get("arguments") for t in tcs])
check("模型正常返回（有 tool_calls 或正文）", bool(tcs) or bool(msg.get("content")))

print()
print("=" * 74)
print("B. 工具结果回传后的第二轮 ReAct（tool 消息配对）")
print("=" * 74)
if tcs:
    p2 = {"model": MODEL, "max_tokens": 1024, "stream": False, "tools": TOOLS,
          "messages": [
              {"role": "user", "content": "请调用 read_file 工具读取 /proj/main.py 的内容。"},
              {"role": "assistant", "content": msg.get("content") or "", "tool_calls": tcs},
              {"role": "tool", "content": "def main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n"},
              {"role": "user", "content": "这个文件做了什么？一句话回答。"},
          ]}
    r2, d2 = post(p2)
    print("  HTTP %s  用时 %.1fs" % (r2.status_code, d2))
    check("工具结果回传后仍返回 200（未出现 No user query / 模板断言失败）",
          r2.status_code == 200, "code=%s" % r2.status_code)
    if r2.status_code != 200:
        print("  body:", r2.text[:400])
    else:
        j2 = r2.json()
        c2 = ((j2.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        print("  回答:", c2.strip()[:120].replace("\n", " "))
        check("第二轮给出了正文回答", bool(c2.strip()))
else:
    print("  （首轮未产生 tool_calls，跳过）")

print()
print("=" * 74)
print("C. 超窗长历史：网关裁剪后必须 200 而非 400/500")
print("=" * 74)
msgs = [{"role": "system", "content": "你是严谨的编码助手。"},
        {"role": "user", "content": "请完整分析项目并给出重构方案。" + "要求非常详细。" * 20}]
for i in range(420):
    msgs.append({"role": "assistant", "content": "", "tool_calls": [{
        "type": "function",
        "function": {"name": "read_file",
                     "arguments": json.dumps({"path": "/proj/src/mod_%d.py" % i})}}]})
    msgs.append({"role": "tool", "content": "mod_%d.py 内容：" % i + "def foo(): pass\n" * 60})
msgs.append({"role": "user", "content": "请基于以上内容，用一句话总结。"})
p3 = {"model": MODEL, "max_tokens": 512, "stream": False, "tools": TOOLS, "messages": msgs}
body_size = len(json.dumps(p3, ensure_ascii=False))
print("  构造消息数=%d  请求体≈%.1f MB" % (len(msgs), body_size / 1048576.0))
r3, d3 = post(p3)
print("  HTTP %s  用时 %.1fs" % (r3.status_code, d3))
print("  响应头 ctx-guard:", r3.headers.get("X-Ctx-Guard"),
      " X-Think-Headroom:", r3.headers.get("X-Think-Headroom"))
check("超窗请求被网关接住并返回 200", r3.status_code == 200, "code=%s" % r3.status_code)
if r3.status_code != 200:
    print("  body:", r3.text[:500])
else:
    j3 = r3.json()
    c3 = ((j3.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    print("  finish_reason=%s" % (j3.get("choices") or [{}])[0].get("finish_reason"))
    print("  回答:", c3.strip()[:120].replace("\n", " "))
    check("裁剪后仍能产出回答", bool(c3.strip()))
    check("未因 MAX_TOKENS 截断（finish_reason 非 length）",
          (j3.get("choices") or [{}])[0].get("finish_reason") != "length")

print()
print("=" * 74)
print("结果：PASS=%d  FAIL=%d" % (len(OK), len(BAD)))
for b in BAD:
    print("   -", b)
print("=" * 74)
sys.exit(1 if BAD else 0)
