"""CSINS大模型管理运维系统 —— 主入口。

同时启动：
  * 管理后台 (admin.py)  ——  默认 0.0.0.0:9090
  * OpenAI 兼容网关 (gateway.py)  ——  默认 0.0.0.0:8000

用法：
    python run.py            # 正常启动
    python run.py --admin    # 仅管理后台
    python run.py --gateway  # 仅网关
"""
import argparse
import os
import sys
import threading
import time
import webbrowser

# 允许以脚本方式直接运行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import CFG  # noqa: E402
from app import store, server_manager as sm  # noqa: E402


# ---------------------------------------------------------------------------
# 日志落盘
#   无论用哪种方式启动（双击「启动系统.bat」前台、_start_bg.bat 后台、
#   或会话托管），都把 stdout / stderr 与 logging 同时写入 _mgr.log，
#   避免"前台启动导致日志只留在控制台、出问题无日志可查"。
#   设环境变量 CSINS_NO_FILE_LOG=1 可临时关闭（调试用）。
# ---------------------------------------------------------------------------
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_mgr.log")
_LOG_MAX_BYTES = 20 * 1024 * 1024
_LOG_FH = None


class _Tee:
    """把写入同时转发到多个流；任一目标出错不影响其它目标。"""

    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
            except Exception:
                pass
        return len(data)

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        for s in self._streams:
            try:
                if s.isatty():
                    return True
            except Exception:
                pass
        return False

    @property
    def encoding(self):
        for s in self._streams:
            enc = getattr(s, "encoding", None)
            if enc:
                return enc
        return "utf-8"

    @property
    def errors(self):
        for s in self._streams:
            err = getattr(s, "errors", None)
            if err:
                return err
        return "replace"

    def writable(self):
        return True

    def fileno(self):
        if not self._streams:
            raise OSError("no underlying stream")
        return self._streams[0].fileno()


def _rotate_log(path, max_bytes):
    """轻量轮转：超过上限就把当前日志改名为 .1（覆盖旧 .1），避免无限增长。"""
    try:
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            bak = path + ".1"
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(path, bak)
    except Exception:
        pass


def _setup_file_logging():
    """打开 _mgr.log 并把输出镜像进去；失败则静默降级为原样输出。"""
    global _LOG_FH
    if os.environ.get("CSINS_NO_FILE_LOG") == "1":
        return None
    try:
        _rotate_log(_LOG_PATH, _LOG_MAX_BYTES)
        fh = open(_LOG_PATH, "a", encoding="utf-8", buffering=1)
    except Exception:
        return None
    _LOG_FH = fh
    sys.stdout = _Tee(sys.__stdout__, fh)
    sys.stderr = _Tee(sys.__stderr__, fh)
    try:
        import logging
        root = logging.getLogger()
        target = os.path.abspath(_LOG_PATH)
        dup = any(
            isinstance(h, logging.FileHandler)
            and os.path.abspath(getattr(h, "baseFilename", "") or "") == target
            for h in root.handlers
        )
        if not dup:
            h = logging.StreamHandler(fh)
            h.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
            root.addHandler(h)
            if root.level == logging.NOTSET or root.level > logging.INFO:
                root.setLevel(logging.INFO)
    except Exception:
        pass
    return fh


def _banner():
    print("=" * 66)
    print("  CSINS大模型管理运维系统  CSINS LLM Manager  v1.0")
    print("=" * 66)
    print("  管理后台 :  http://%s:%s" % (CFG["manager_host"], CFG["manager_port"]))
    print("  OpenAI 网关 :  http://%s:%s/v1" % (CFG["gateway_host"], CFG["gateway_port"]))
    print("  模型目录 :  %s" % CFG["ai_model_dir"])
    print("  llama.cpp:  %s" % CFG["server_exe"])
    print("  默认密码 :  admin")
    if _LOG_FH is not None:
        print("  运行日志 :  %s" % _LOG_PATH)
    print("=" * 66)


def _autostart():
    for m in store.get_models():
        if m.get("autostart"):
            print("[自动启动] %s" % (m.get("display_name") or m.get("name")))
            ok, msg = sm.start(m)
            print("   ->", msg)


def _gateway_alive(port):
    """探测本机是否已有本系统的网关在跑（用 /health 的服务标识确认，避免误判）。"""
    import json
    import urllib.request
    try:
        # 显式绕过系统代理，避免本机回环探测被代理劫持而误判"未运行"
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open("http://127.0.0.1:%d/health" % int(port), timeout=2) as r:
            if r.status != 200:
                return False
            d = json.loads(r.read().decode("utf-8", "replace") or "{}")
            return d.get("service") == "llm-gateway"
    except Exception:
        return False


def _single_instance_guard():
    """拒绝重复启动：多个 run.py 同时绑 8000/9090 会导致请求分流、并发的 JSON
    写入互相覆盖（原子写用的是固定 .tmp 名，跨进程会冲突），造成数据损坏。
    返回 True 表示可以继续启动。"""
    port = CFG.get("gateway_port")
    if not port or not _gateway_alive(port):
        return True
    print("!")
    print("! 检测到已有实例在运行（网关端口 %s 已就绪）。" % port)
    print("! 同一时间只允许一个实例，否则请求会随机分流、数据文件可能互相覆盖。")
    print("! 如需重启：先关闭旧实例，或运行 _restart_service.bat。")
    print("! 如确认要强制再起一个（不推荐）：加参数 --force")
    print("!")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--admin", action="store_true")
    ap.add_argument("--gateway", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--force", action="store_true", help="跳过单实例检查，强制启动")
    args = ap.parse_args()

    # 先把输出接到 _mgr.log：这样后续 banner、单实例提示、异常栈都会落盘
    _setup_file_logging()

    if not args.force and not _single_instance_guard():
        sys.exit(3)

    _banner()
    threads = []

    if not args.admin:
        from app.gateway import app as gw
        t = threading.Thread(
            target=lambda: gw.run(host=CFG["gateway_host"], port=CFG["gateway_port"],
                                  threaded=True, use_reloader=False),
            daemon=True, name="gateway")
        t.start()
        threads.append(t)

    if not args.gateway:
        t2 = threading.Thread(target=_autostart, daemon=True, name="autostart")
        t2.start()

    time.sleep(1)
    if not args.no_browser:
        try:
            webbrowser.open("http://127.0.0.1:%s" % CFG["manager_port"])
        except Exception:
            pass

    from app.admin import app as adm
    try:
        adm.run(host=CFG["manager_host"], port=CFG["manager_port"],
                threaded=True, use_reloader=False)
    finally:
        sm.stop_all()


if __name__ == "__main__":
    main()
