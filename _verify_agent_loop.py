# -*- coding: utf-8 -*-
"""模拟真实 Agent 多轮工具循环（流式），验证端到端稳定性。"""
import os, json, time
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
import requests
S = requests.Session(); S.trust_env = False
GW = "http://127.0.0.1:8000/v1/chat/completions"
HDR = {"Authorization": "Bearer sk-REPLACE_ME"}

TOOLS = [
    {"type": "function", "function": {
        "name": "list_dir", "description": "列出目录下的文件",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "读取文件内容",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "写入文件",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
]

# 假的工具实现
def fake_tool(name, args):
    if name == "list_dir":
        return {"files": ["main.py", "utils.py", "README.md"]}
    if name == "read_file":
        return {"path": args.get("path"), "content": "def main():\n    print('hello')\n\nif __name__ == '__main__':\n    main()\n"}
    if name == "write_file":
        return {"ok": True, "path": args.get("path"), "bytes": len(str(args.get("content", "")))}
    return {"error": "unknown tool"}


def stream_one(messages, tag):
    """发一次流式请求，返回 (content, tool_calls, finish, usage)。"""
    r = S.post(GW, headers=HDR, timeout=900, stream=True, json={
        "model": "qwen3.8-27b", "messages": messages, "tools": TOOLS,
        "tool_choice": "auto", "max_tokens": 1024, "stream": True,
        "stream_options": {"include_usage": True},
        "chat_template_kwargs": {"enable_thinking": False},
    })
    if r.status_code != 200:
        return None, None, "HTTP-%s:%s" % (r.status_code, r.text[:200]), None
    content, finish, usage = "", None, None
    tcs = {}          # index -> {name, args}
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        pl = line[6:]
        if pl.strip() == "[DONE]":
            break
        try:
            o = json.loads(pl)
        except Exception:
            continue
        if o.get("usage"):
            usage = o["usage"]
        for c in (o.get("choices") or []):
            if c.get("finish_reason"):
                finish = c["finish_reason"]
            d = c.get("delta") or {}
            if d.get("content"):
                content += d["content"]
            for tc in (d.get("tool_calls") or []):
                i = tc.get("index", 0)
                e = tcs.setdefault(i, {"id": None, "name": None, "args": ""})
                if tc.get("id"):
                    e["id"] = tc["id"]
                f = tc.get("function") or {}
                if f.get("name"):
                    e["name"] = f["name"]
                if f.get("arguments"):
                    e["args"] += f["arguments"]
    calls = [{"id": v["id"] or ("call_%d" % i), "type": "function",
              "function": {"name": v["name"], "arguments": v["args"]}} for i, v in sorted(tcs.items())]
    return content, calls, finish, usage


print("=" * 74)
print("模拟 Agent 多轮工具循环（流式，最多 6 轮）")
print("=" * 74)
messages = [{"role": "user", "content": "帮我看看当前目录有哪些文件，然后读取 main.py 并告诉我它做了什么。"}]
ok = True
for step in range(1, 7):
    t0 = time.time()
    content, calls, finish, usage = stream_one(messages, "step%d" % step)
    dt = time.time() - t0
    if calls is None:
        print("  第%d轮 失败: %s" % (step, finish)); ok = False; break
    print("  第%d轮 | %.1fs | finish=%-11s | tool_calls=%d | content=%d字符 | usage=%s" % (
        step, dt, finish, len(calls), len(content), usage))
    if calls:
        # assistant 消息带 tool_calls
        messages.append({"role": "assistant", "content": content or "", "tool_calls": calls})
        for c in calls:
            fn = c["function"]["name"]
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except Exception as e:
                print("     !! 工具参数 JSON 非法: %r | 原始=%r" % (e, c["function"]["arguments"][:200]))
                ok = False
                args = {}
            res = fake_tool(fn, args)
            print("     → 调用 %s(%s) = %s" % (fn, json.dumps(args, ensure_ascii=False)[:60],
                                             json.dumps(res, ensure_ascii=False)[:80]))
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps(res, ensure_ascii=False)})
    else:
        print("     ✓ 循环结束，最终回答：")
        print("     " + (content or "")[:400].replace("\n", "\n     "))
        break
else:
    print("  !! 6 轮仍未收敛")
    ok = False

print()
print("消息历史长度 = %d 条 | 角色序列 = %s" % (len(messages), [m["role"] for m in messages]))
print("结论: %s" % ("✅ Agent 多轮循环端到端正常" if ok else "❌ 存在问题"))
