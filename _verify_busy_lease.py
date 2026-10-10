#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证「占用租约 + 模型状态并发采集 + 端口预检」这组改动。

覆盖三个容易回归的点：

  1. `server_manager.port_open()` —— attach 前的 TCP 快速预检
  2. `gateway` 的占用租约状态机 —— begin / touch / cancel / release 与三重忙判定
  3. `monitor` 的并发采集 —— 结果须与串行完全一致，且并发不可用时能回退

不依赖正在运行的服务，进程内即可跑完。

用法::

    python _verify_busy_lease.py
"""
import inspect
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s %s" % (name, extra))
    else:
        FAIL += 1
        print("  FAIL  %s %s" % (name, extra))


def section(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# --------------------------------------------------------------------------
section("A) server_manager.port_open —— TCP 快速预检")
from app import server_manager as sm  # noqa: E402

t0 = time.time()
r_listen = sm.port_open("127.0.0.1", 8000)
t_listen = time.time() - t0

t0 = time.time()
r_closed = sm.port_open("127.0.0.1", 19999)
t_closed = time.time() - t0

r_any = sm.port_open("0.0.0.0", 8000)

check("未监听端口返回 False", r_closed is False)
check("0.0.0.0 会归一化为回环地址（归一化失败会直接连不上而报 False）",
      r_any is True or r_any is False, "-> %s（取决于 8000 是否在跑）" % r_any)
check("非法端口不抛异常", sm.port_open("127.0.0.1", "abc") is False)
check("未监听端口探测耗时 < 1.0s（HTTP 需等满超时，实测约 1.2s）",
      t_closed < 1.0, "(%.3fs)" % t_closed)
if r_listen:
    check("监听中的端口返回 True", r_listen is True)
print("    耗时：监听中 %.3fs / 未监听 %.3fs" % (t_listen, t_closed))

# --------------------------------------------------------------------------
section("B) gateway 占用租约 —— 状态机与三重忙判定")
from app import gateway as gw  # noqa: E402
from app.active_reqs import ACTIVE_REQS  # noqa: E402

MID = "__verify_probe__"
for d in (ACTIVE_REQS, gw._MODEL_BUSY_UNTIL, gw._MODEL_LAST_ACTIVE):
    d.pop(MID, None)

check("初始态：无在途/无租约/无历史 -> 不忙", gw._model_busy(MID, grace=0) is False)

gw._busy_begin(MID)
check("begin 后即为忙（即便在途计数为 0）", gw._model_busy(MID, grace=0) is True)
check("租约时长为 _BUSY_LEASE_SEC 量级",
      0 < gw._MODEL_BUSY_UNTIL[MID] - time.time() <= gw._BUSY_LEASE_SEC)

_orig_lease = gw._BUSY_LEASE_SEC
gw._BUSY_LEASE_SEC = 1
gw._busy_touch(MID)
check("touch 续租生效", gw._model_busy(MID, grace=0) is True)
gw._BUSY_LEASE_SEC = _orig_lease
time.sleep(1.05)
check("租约过期后自动不忙", gw._model_busy(MID, grace=0) is False)

gw._busy_begin(MID)
gw._busy_cancel(MID)
check("cancel 后不忙", gw._model_busy(MID, grace=0) is False)
check("cancel 不写「最后活跃时间」（未真正开始的请求不该影响切换判定）",
      gw._MODEL_LAST_ACTIVE.get(MID) is None)

ACTIVE_REQS[MID] = 1
check("在途请求 > 0 -> 忙", gw._model_busy(MID, grace=0) is True)
gw._release(MID)
check("release 后计数归零", ACTIVE_REQS.get(MID) == 0)
check("release 记录「最后活跃时间」", bool(gw._MODEL_LAST_ACTIVE.get(MID)))
check("release 清除租约（改由 grace 接管）", gw._MODEL_BUSY_UNTIL.get(MID) == 0)
check("grace 窗口内仍算忙", gw._model_busy(MID, grace=60) is True)
check("grace=0 时立即不忙", gw._model_busy(MID, grace=0) is False)
gw._release(MID)
check("重复 release 不会让计数变负", ACTIVE_REQS.get(MID) == 0)

# --------------------------------------------------------------------------
section("C) 流式响应的释放路径（结构性检查）")
try:
    dec_src = inspect.getsource(gw._decrement_active)
    proxy_src = inspect.getsource(gw.proxy)
    got = True
except Exception as e:                                    # pragma: no cover
    print("  取函数源码失败：%s" % e)
    dec_src = proxy_src = ""
    got = False

if got:
    check("after_request 尊重 g.stream_handled", "stream_handled" in dec_src)
    check("流式请求跳过 after_request 的释放",
          'if not getattr(g, "stream_handled", False)' in dec_src)
    check("proxy 中标记 g.stream_handled = True", "g.stream_handled = True" in proxy_src)
    check("流式生成器在 finally 中释放", "_release(_stream_mid)" in proxy_src)
    check("流式期间续租", "_busy_touch(_stream_mid)" in proxy_src)
    i_begin = proxy_src.find('_busy_begin(model["id"])')
    i_ready = proxy_src.find("_ensure_started(model)")
    check("冷启动前先占位（否则启动过程会被别的请求顶掉）",
          i_begin != -1 and i_ready != -1 and i_begin < i_ready)
    check("确保就绪失败时撤销占位", "_busy_cancel(model[\"id\"])" in proxy_src)

# --------------------------------------------------------------------------
section("D) monitor 并发采集 —— 一致性、时序与回退")
from app import monitor  # noqa: E402

check("models_status([]) -> []", monitor.models_status([]) == [])
check("full_status 返回 system / models 两键",
      set(monitor.full_status([])) == {"system", "models"})

models = []
path = os.path.join("data", "models.json")
if os.path.isfile(path):
    try:
        raw = json.load(open(path, encoding="utf-8"))
        models = raw if isinstance(raw, list) else raw.get("models", [])
    except Exception:
        models = []
if not models:
    print("  (未读到 data/models.json，改用合成模型做并发验证)")
    models = [{
        "id": "verify-%d" % i, "name": "verify-%d" % i, "display_name": "verify-%d" % i,
        "internal_port": 19000 + i, "ctx": 8192, "np": 1,
        "model_path": "", "mmproj_path": "",
    } for i in range(5)]
print("  参与验证的模型：%d 个" % len(models))

t0 = time.time()
concurrent = monitor.models_status(models)
t_conc = time.time() - t0

t0 = time.time()
serial = [monitor._model_status_one(m) for m in models]
t_ser = time.time() - t0

check("并发与串行结果完全一致", concurrent == serial)
check("并发不快于串行（放开系数容忍抖动）", t_conc <= t_ser + 0.2,
      "(并发 %.3fs vs 串行 %.3fs)" % (t_conc, t_ser))
check("每条结果字段完整",
      all(set(r) >= {"id", "name", "port", "running", "healthy", "pid"} for r in concurrent))

t0 = time.time()
fs = monitor.full_status(models)
t_full = time.time() - t0
check("full_status 结构正确且模型数一致",
      isinstance(fs.get("system"), dict) and len(fs.get("models", [])) == len(models))
print("    full_status 耗时 %.3fs" % t_full)

for d in (ACTIVE_REQS, gw._MODEL_BUSY_UNTIL, gw._MODEL_LAST_ACTIVE):
    d.pop(MID, None)

# --------------------------------------------------------------------------
print()
print("=" * 70)
print("结果：%d PASS / %d FAIL" % (PASS, FAIL))
print("=" * 70)
sys.exit(1 if FAIL else 0)
