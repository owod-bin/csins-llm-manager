# -*- coding: utf-8 -*-
"""验证 run.py 的日志落盘改造（不启动服务、不占端口）。"""
import os
import sys
import logging

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import run  # noqa: E402

LOG = run._LOG_PATH
ok = True


def check(name, cond, extra=""):
    global ok
    print(("  [PASS] " if cond else "  [FAIL] ") + name + ("  " + extra if extra else ""))
    if not cond:
        ok = False


print("== 1. 基本属性 ==")
print("  日志路径 :", LOG)
check("日志文件存在", os.path.exists(LOG))

print("== 2. _setup_file_logging 生效 ==")
before = os.path.getsize(LOG)
fh = run._setup_file_logging()
check("返回文件对象", fh is not None)
check("sys.stdout 已被 Tee", isinstance(sys.stdout, run._Tee))
check("sys.stderr 已被 Tee", isinstance(sys.stderr, run._Tee))
check("Tee.encoding 可读", isinstance(sys.stdout.encoding, str), sys.stdout.encoding)
check("Tee.isatty() 可调用", isinstance(sys.stdout.isatty(), bool))
check("Tee.fileno() 可用", isinstance(sys.stdout.fileno(), int))

MARK = "TEE-PROBE-9F3C 中文落盘测试"
print(MARK)
print(MARK, file=sys.stderr)
logging.getLogger("werkzeug").warning("WERKZEUG-PROBE-9F3C 请求日志测试")
sys.stdout.flush()
sys.stderr.flush()
if fh:
    fh.flush()

after = os.path.getsize(LOG)
check("文件增大", after > before, "delta=%d" % (after - before))

with open(LOG, "r", encoding="utf-8", errors="replace") as f:
    tail = f.read()[-4000:]
check("stdout 内容已入文件", MARK in tail)
check("logging 内容已入文件", "WERKZEUG-PROBE-9F3C" in tail)

print("== 3. 日志轮转 ==")
import tempfile
tmpdir = tempfile.mkdtemp()
tp = os.path.join(tmpdir, "_mgr.log")
with open(tp, "w", encoding="utf-8") as f:
    f.write("x" * 100)
run._rotate_log(tp, 50)
check("超限已轮转", (not os.path.exists(tp)) and os.path.exists(tp + ".1"))
with open(tp, "w", encoding="utf-8") as f:
    f.write("y" * 10)
run._rotate_log(tp, 50)
check("未超限不轮转", os.path.exists(tp) and os.path.getsize(tp) == 10)

print("== 4. CSINS_NO_FILE_LOG=1 可关闭 ==")
os.environ["CSINS_NO_FILE_LOG"] = "1"
r = run._setup_file_logging()
check("关闭时返回 None", r is None)
os.environ.pop("CSINS_NO_FILE_LOG", None)

print()
print("=== 结论:", "全部通过" if ok else "存在失败项", "===")
sys.exit(0 if ok else 1)
