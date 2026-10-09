# -*- coding: utf-8 -*-
"""解析 GGUF 元数据，提取 chat_template，检查是否支持工具调用。"""
import struct, sys, json

PATH = sys.argv[1] if len(sys.argv) > 1 else r"D:\models\Ternary-Bonsai-2-27B\Ternary-Bonsai-2-27B-PQ2_0.gguf"

f = open(PATH, "rb")
magic = f.read(4)
assert magic == b"GGUF", magic
ver = struct.unpack("<I", f.read(4))[0]
n_tensors = struct.unpack("<Q", f.read(8))[0]
n_kv = struct.unpack("<Q", f.read(8))[0]
print("GGUF version=%s tensors=%d kv=%d" % (ver, n_tensors, n_kv))

T_U8, T_I8, T_U16, T_I16, T_U32, T_I32, T_F32, T_BOOL, T_STR, T_ARR, T_U64, T_I64, T_F64 = range(13)


def rstr():
    l = struct.unpack("<Q", f.read(8))[0]
    return f.read(l).decode("utf-8", errors="replace")


def rval(t):
    if t == T_U8:   return struct.unpack("<B", f.read(1))[0]
    if t == T_I8:   return struct.unpack("<b", f.read(1))[0]
    if t == T_U16:  return struct.unpack("<H", f.read(2))[0]
    if t == T_I16:  return struct.unpack("<h", f.read(2))[0]
    if t == T_U32:  return struct.unpack("<I", f.read(4))[0]
    if t == T_I32:  return struct.unpack("<i", f.read(4))[0]
    if t == T_F32:  return struct.unpack("<f", f.read(4))[0]
    if t == T_BOOL: return struct.unpack("<B", f.read(1))[0]
    if t == T_STR:  return rstr()
    if t == T_U64:  return struct.unpack("<Q", f.read(8))[0]
    if t == T_I64:  return struct.unpack("<q", f.read(8))[0]
    if t == T_F64:  return struct.unpack("<d", f.read(8))[0]
    if t == T_ARR:
        et = struct.unpack("<I", f.read(4))[0]
        n = struct.unpack("<Q", f.read(8))[0]
        if et == T_STR:
            return [rstr() for _ in range(n)]
        return [rval(et) for _ in range(n)]
    raise ValueError("t=%s" % t)


meta = {}
for _ in range(n_kv):
    k = rstr()
    t = struct.unpack("<I", f.read(4))[0]
    v = rval(t)
    meta[k] = v

print("KV 总数:", len(meta))
print("关键字段:")
for k in sorted(meta):
    if any(s in k for s in ("template", "name", "reasoning", "architecture", "tokenizer.ggml.model")):
        v = meta[k]
        if isinstance(v, str) and len(v) > 200:
            print("  %s = <str %d chars>" % (k, len(v)))
        else:
            print("  %s = %r" % (k, v))

tpl = meta.get("tokenizer.chat_template") or ""
print("")
print("=" * 70)
print("chat_template 长度:", len(tpl))
print("=" * 70)
for kw in ("tools", "tool_call", "tool_call_id", "tool_choice", "function", "parallel",
           "enable_thinking", "reasoning_effort", "<tool_call>", "tool_response", "role"):
    print("  %-18s -> %d 次" % (kw, tpl.count(kw)))
open("_bonsai_tpl.txt", "w", encoding="utf-8").write(tpl)
print("\n模板已导出到 _bonsai_tpl.txt")
