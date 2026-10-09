# -*- coding: utf-8 -*-
"""P1 三项修复的回归验证（不经过 HTTP 网关，直接打函数）。

P1-1 客户端 maxInputTokens 超窗        -> 见 models.json（本脚本只读校验）
P1-2 裁剪产生孤儿 tool 消息             -> _guard_context / _hard_fit 块化裁剪
P1-3 token 估算漏算 tool_calls / tools -> _msg_text / _count_prompt_tokens
"""
import json
import os
import sys

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import gateway as gw  # noqa: E402

BASE = "http://127.0.0.1:18080"
OK = []
BAD = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  [PASS] " if cond else "  [FAIL] ") + name + (("  :: " + detail) if detail else ""))


def tools_def():
    return [{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取指定路径的文件内容，用于分析代码/日志",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径"},
                    "encoding": {"type": "string", "description": "编码，默认 utf-8"},
                },
                "required": ["path"],
            },
        },
    }]


def long_history(n_turns=40):
    """构造 agent 形态的长历史：user 任务 + 多轮 (assistant.tool_calls -> tool) + 尾巴。"""
    msgs = [{"role": "system", "content": "你是一个严谨的编码助手。"}]
    msgs.append({"role": "user", "content": "请完整分析这个项目并给出重构方案。" + "要求非常详细。" * 20})
    for i in range(n_turns):
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call_%d" % i,
                "type": "function",
                "function": {"name": "read_file",
                             "arguments": json.dumps({"path": "/proj/src/mod_%d.py" % i})},
            }],
        })
        msgs.append({"role": "tool", "content": "文件 mod_%d.py 的内容：" % i + "def foo(): pass\n" * 60})
    msgs.append({"role": "assistant", "content": "阶段性结论：" + "目前共读取了若干文件。" * 10})
    return msgs


def analyze(msgs):
    """返回裁剪结果的健康度指标。"""
    orphan = 0            # tool 消息前面没有 assistant(tool_calls)
    naked_tc = 0          # assistant(tool_calls) 后面没有 tool 结果
    armed = False
    for m in msgs:
        if not isinstance(m, dict):
            continue
        r = m.get("role")
        if r == "assistant":
            if armed and naked_tc >= 0:
                pass
            armed = bool(m.get("tool_calls"))
            if armed:
                naked_tc += 1
        elif r == "tool":
            if not armed:
                orphan += 1
            elif naked_tc > 0:
                naked_tc -= 1
        else:
            armed = False
    n_user = sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "user")
    head_bad = bool(msgs) and isinstance(msgs[0], dict) and msgs[0].get("role") == "assistant" \
        and not msgs[0].get("tool_calls")
    return {"n": len(msgs), "orphan": orphan, "naked_tc": naked_tc,
            "n_user": n_user, "head_bad": head_bad}


print("=" * 74)
print("P1-3  token 估算：必须把 tool_calls / tools 计入")
print("=" * 74)
m_tc = {"role": "assistant", "content": "", "tool_calls": [
    {"id": "c1", "type": "function",
     "function": {"name": "read_file", "arguments": json.dumps({"path": "/x/y.py"})}}]}
t_tc = gw._msg_text(m_tc)
t_body = gw._msg_body_text(m_tc)
print("  _msg_text(tool_calls 消息) ->", repr(t_tc[:110]))
check("_msg_text 含 tool_calls 内容", "read_file" in t_tc and "/x/y.py" in t_tc)
check("_msg_body_text 不含 tool_calls（可安全回写 content）", "read_file" not in t_body)

msgs_small = [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "读取 /tmp/a.py"}]
n_plain = gw._count_prompt_tokens(BASE, {}, msgs_small, None)
n_tools = gw._count_prompt_tokens(BASE, {}, msgs_small, tools_def())
print("  /apply-template token: 不带 tools=%s  带 tools=%s" % (n_plain, n_tools))
check("精确计数可用（上游在线）", n_plain is not None and n_tools is not None)
if n_plain and n_tools:
    check("工具定义已计入精确计数（带 tools 明显更大）", n_tools > n_plain + 10,
          "delta=%d" % (n_tools - n_plain))
    # 旧实现只按粗估补：对比粗估与实际渲染的差距
    est = gw._tools_est_tokens(tools_def())
    print("  粗估 tools_tokens=%d，实际渲染增量=%d" % (est, n_tools - n_plain))
check("_tools_rendered 能识别已渲染", gw._tools_rendered(
    "get_weather read_file", [{"type": "function", "function": {"name": "read_file"}}]))

print()
print("=" * 74)
print("P1-2  _guard_context 块化裁剪：不得产生孤儿 tool 消息")
print("=" * 74)
msgs = long_history(40)
raw = sum(gw._est_tokens(gw._msg_text(m)) for m in msgs)
print("  原始消息数=%d  粗估 tokens=%d" % (len(msgs), raw))
body = {"messages": msgs, "max_tokens": 1024, "tools": tools_def()}
nm, nmt, drop, trim = gw._guard_context(BASE, {"ctx": 3000}, body,
                                        inp_tokens=10 ** 7, cap=3000)
st = analyze(nm)
print("  裁剪后: 消息数=%d max_tokens=%s dropped=%s trimmed=%s" % (st["n"], nmt, drop, trim))
print("  指标:", st)
check("确实发生了裁剪", trim and st["n"] < len(msgs))
check("无孤儿 tool 消息（P1-2 核心）", st["orphan"] == 0, "orphan=%d" % st["orphan"])
check("保留至少一条 user（防上游 500）", st["n_user"] >= 1)
check("不以 assistant 开头", not st["head_bad"])
check("tool 结果未被单独丢弃（naked_tc==0）", st["naked_tc"] == 0, "naked=%d" % st["naked_tc"])

print()
print("  极端压缩：窗口极小，连一个块都放不下")
body2 = {"messages": long_history(6), "max_tokens": 512, "tools": tools_def()}
nm2, nmt2, drop2, trim2 = gw._guard_context(BASE, {"ctx": 900}, body2,
                                            inp_tokens=10 ** 7, cap=900)
st2 = analyze(nm2)
print("  指标:", st2, "max_tokens=", nmt2)
check("极端压缩下仍保留 user", st2["n_user"] >= 1)
check("极端压缩下无孤儿 tool", st2["orphan"] == 0)
check("输出额度守住保底(%d)" % gw._MIN_OUT, int(nmt2 or 0) >= gw._MIN_OUT)

print()
print("  真实窗口场景（cap=131072）：长历史裁剪后仍需保留足量历史且无孤儿")
big = long_history(500)
body_big = {"messages": big, "max_tokens": 8192, "tools": tools_def()}
nb = gw._count_prompt_tokens(BASE, {}, big, tools_def()) or 0
print("  原始：消息数=%d 精确 tokens≈%d" % (len(big), nb))
nmb, nmtb, dropb, trimb = gw._guard_context(BASE, {"ctx": 131072}, body_big, nb, cap=131072)
stb = analyze(nmb)
prob = gw._count_prompt_tokens(BASE, {}, nmb, tools_def())
print("  裁剪后：消息数=%d max_tokens=%s dropped=%d 精确 tokens=%s"
      % (stb["n"], nmtb, dropb, prob))
print("  指标:", stb)
check("真实窗口下触发裁剪", trimb)
check("真实窗口下无孤儿 tool", stb["orphan"] == 0, "orphan=%d" % stb["orphan"])
check("真实窗口下保留足量历史(>=100 条)", stb["n"] >= 100, "n=%d" % stb["n"])
if prob:
    check("裁剪后精确 tokens 落在 94% 预算内", prob <= int(131072 * 0.94),
          "n=%d budget=%d" % (prob, int(131072 * 0.94)))

print()
print("=" * 74)
print("P1-2  _hard_fit 兜底裁剪：同样不得产生孤儿 tool 消息")
print("=" * 74)
body3 = {"messages": long_history(40), "max_tokens": 1024, "tools": tools_def()}
did = gw._hard_fit(BASE, {"ctx": 3000}, body3, 3000)
st3 = analyze(body3["messages"])
n3 = gw._count_prompt_tokens(BASE, {}, body3["messages"], body3.get("tools"))
print("  裁剪发生=%s  消息数=%d  精确 tokens=%s" % (did, st3["n"], n3))
print("  指标:", st3)
check("_hard_fit 触发了裁剪", did)
check("_hard_fit 无孤儿 tool 消息", st3["orphan"] == 0, "orphan=%d" % st3["orphan"])
check("_hard_fit 保留 user", st3["n_user"] >= 1)
if n3 is not None:
    check("_hard_fit 结果落在 72% 窗口预算内", n3 <= int(3000 * 0.72),
          "n=%d budget=%d" % (n3, int(3000 * 0.72)))

print()
print("=" * 74)
print("单元级：_blocks / _drop_orphan_tools")
print("=" * 74)
sample = [
    {"role": "system", "content": "s"},
    {"role": "tool", "content": "孤儿：前面没有 assistant.tool_calls"},
    {"role": "user", "content": "u"},
    {"role": "assistant", "tool_calls": [{"id": "a", "type": "function",
                                          "function": {"name": "f", "arguments": "{}"}}]},
    {"role": "tool", "content": "t1"},
    {"role": "tool", "content": "t2"},
    {"role": "assistant", "content": "done"},
]
blks = gw._blocks(sample)
print("  _blocks ->", blks)
check("_blocks 把 assistant(tool_calls)+2×tool 合成一块", (3, 6) in blks and (1, 2) in blks)
clean = gw._drop_orphan_tools(sample)
print("  清理后 roles ->", [m.get("role") for m in clean])
check("_drop_orphan_tools 去掉孤儿 tool", sum(1 for m in clean if m.get("role") == "tool") == 2)

print()
print("=" * 74)
print("P1-1  客户端配置（models.json）输入上限校验")
print("=" * 74)
mp = os.path.expanduser("~/.workbuddy/models.json")
try:
    cfg = json.load(open(mp, encoding="utf-8"))
    for c in cfg:
        mi = int(c.get("maxInputTokens") or 0)
        mo = int(c.get("maxOutputTokens") or 0)
        print("  %s: maxInput=%d maxOutput=%d 合计=%d" % (c.get("name"), mi, mo, mi + mo))
        check("%s 输入+输出 <= 131072 窗口" % c.get("name"), mi + mo <= 131072,
              "合计=%d" % (mi + mo))
except Exception as e:
    print("  读取失败:", e)

print()
print("=" * 74)
print("结果：PASS=%d  FAIL=%d" % (len(OK), len(BAD)))
if BAD:
    print("失败项：")
    for b in BAD:
        print("   -", b)
print("=" * 74)
sys.exit(1 if BAD else 0)
