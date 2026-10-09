"""llama.cpp 服务进程管理：构建命令行、启停、健康检查。"""
import os
import shutil
import subprocess
import threading
import time

import psutil
import requests

from .config import CFG
from .store import ENGINE_MAP, engine_of, is_external_engine

IS_WIN = os.name == "nt"
_CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

# model_id -> runtime dict
_RUNTIME = {}
_LOCK = threading.RLock()


def server_exe():
    return CFG["server_exe"]


def model_exe(model=None):
    """模型专属引擎路径（支持每个模型用不同 llama.cpp 构建）；为空则用全局默认。"""
    if model:
        exe = (model.get("server_exe") or "").strip()
        if exe:
            return exe
    return server_exe()


def exe_available(model=None):
    return os.path.isfile(model_exe(model))


def _truthy(v):
    return bool(v)


def build_command(model):
    """按引擎分派，把模型配置转换为启动命令行。外部服务引擎无命令。"""
    eng = engine_of(model)
    if eng == "llamacpp":
        return _build_llamacpp(model)
    if eng == "custom":
        return _build_custom(model)
    if eng == "vllm":
        return _build_vllm(model)
    raise ValueError("引擎「%s」为外部服务，无需启动命令" % eng)


def _resolve_path(p):
    p = p or ""
    if p and not os.path.isabs(p):
        p = os.path.join(CFG["ai_model_dir"], p)
    return p


def _build_custom(model):
    """自定义命令模板：支持 {model_path} {port} {host} {ctx} {alias} 占位符。"""
    tpl = (model.get("command_template") or "").strip()
    if not tpl:
        raise ValueError("自定义引擎需填写启动命令模板")
    vars = {
        "model_path": _resolve_path(model.get("model_path")),
        "port": str(model.get("internal_port", CFG["internal_port_start"])),
        "host": str(model.get("host", "127.0.0.1")),
        "ctx": str(model.get("ctx", 131072)),
        "alias": str(model.get("alias") or model.get("name") or ""),
        "mmproj_path": _resolve_path(model.get("mmproj_path")),
    }
    import shlex
    out = tpl
    for k, v in vars.items():
        out = out.replace("{%s}" % k, v)
    return shlex.split(out, posix=False)


def _build_vllm(model):
    r"""vLLM OpenAI 兼容服务（需 een_dir 指向的 Python 环境已安装 vllm）。"""
    een = CFG.get("een_dir") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "een")
    py = os.path.join(een, "python", "python.exe")
    mp = _resolve_path(model.get("model_path"))
    if not mp:
        raise ValueError("未配置模型文件/目录路径")
    cmd = [py, "-m", "vllm.entrypoints.openai.api_server", "--model", mp]
    alias = model.get("alias") or model.get("name")
    if alias:
        cmd += ["--served-model-name", str(alias)]
    cmd += ["--host", str(model.get("host", "127.0.0.1"))]
    cmd += ["--port", str(model.get("internal_port", CFG["internal_port_start"]))]
    cmd += ["--max-model-len", str(model.get("ctx", 131072))]
    try:
        gm = float(model.get("gpu_mem_util", 0.9) or 0.9)
    except Exception:
        gm = 0.9
    cmd += ["--gpu-memory-utilization", str(gm)]
    dt = (model.get("dtype") or "auto")
    if dt:
        cmd += ["--dtype", str(dt)]
    try:
        tp = int(model.get("tensor_parallel", 1) or 1)
    except Exception:
        tp = 1
    if tp > 1:
        cmd += ["--tensor-parallel-size", str(tp)]
    for a in model.get("extra_args", []) or []:
        cmd += [str(a)]
    return cmd


def _build_llamacpp(model):
    """把模型配置字典转换为 llama-server 命令行列表。"""
    cmd = [model_exe(model)]
    m = model.get("model_path", "")
    if not m:
        raise ValueError("未配置模型文件路径")
    if not os.path.isabs(m):
        m = os.path.join(CFG["ai_model_dir"], m)
    cmd += ["-m", m]

    mmproj = model.get("mmproj_path", "")
    if mmproj:
        if not os.path.isabs(mmproj):
            mmproj = os.path.join(CFG["ai_model_dir"], mmproj)
        cmd += ["--mmproj", mmproj]

    cmd += ["-ngl", str(model.get("ngl", 999))]
    cmd += ["--flash-attn", "on" if model.get("fa", True) else "off"]

    if model.get("jinja", True):
        cmd += ["--jinja"]

    cmd += ["-c", str(model.get("ctx", 131072))]
    # -np：并行 slot 数。在 --kv-unified 下所有 slot 共享同一块 n_ctx 缓冲，多 slot 并发会
    # 互相挤压可用上下文；且异常终止的请求可能留下 "is_processing=True" 的僵尸 slot，
    # 使后续请求随机报 "Context size has been exceeded"（表现为 agent 任务无故中断，
    # 且与输入长度不成比例——实测 56K 失败而 94K 成功）。本机是单用户 agent 场景，
    # 默认单 slot（1）行为确定；确需并发请在模型配置里显式调大 np。
    cmd += ["-np", str(model.get("np", 1))]
    if model.get("kv_unified", True):
        cmd += ["--kv-unified"]

    ctk = model.get("ctk", "q8_0")
    ctv = model.get("ctv", "q8_0")
    if ctk and ctk != "f16":
        cmd += ["--cache-type-k", ctk]
    if ctv and ctv != "f16":
        cmd += ["--cache-type-v", ctv]

    if model.get("cont_batching", True):
        cmd += ["--cont-batching"]
    cmd += ["-b", str(model.get("batch", 2048))]
    cmd += ["-ub", str(model.get("ubatch", 512))]

    t = int(model.get("threads", 0) or 0)
    if t > 0:
        cmd += ["-t", str(t)]

    # 新版 llama.cpp 用 --load-mode 取代了已废弃的 --no-mmap
    if model.get("no_mmap", True):
        cmd += ["--load-mode", "none"]

    alias = model.get("alias") or model.get("name")
    if alias:
        cmd += ["-a", str(alias)]

    cmd += ["--host", str(model.get("host", "127.0.0.1"))]
    cmd += ["--port", str(model.get("internal_port", CFG["internal_port_start"]))]

    cmd += _reasoning_args(model)

    for a in model.get("extra_args", []) or []:
        cmd += [str(a)]
    return cmd


def _reasoning_args(model):
    """思考模式控制（llama-server --reasoning / --reasoning-budget）。

    reasoning_mode: auto(跟随模型默认) / on(强制开启) / off(关闭思考)
    reasoning_budget: 思维链 token 预算，-1=不限，0=立即结束，N=限制为 N
    """
    args = []
    mode = str(model.get("reasoning_mode") or "auto").lower()
    if mode not in ("auto", "on", "off"):
        mode = "auto"
    if mode in ("on", "off"):
        args += ["--reasoning", mode]
    if mode == "off":
        # 双保险：显式关闭 + 预算归零（上游立即结束思考）
        args += ["--reasoning-budget", "0"]
    else:
        try:
            budget = int(model.get("reasoning_budget", -1))
        except Exception:
            budget = -1
        if budget is not None and budget >= 0:
            args += ["--reasoning-budget", str(budget)]
    return args


def _log_path(model):
    os.makedirs(CFG["logs_dir"], exist_ok=True)
    return os.path.join(CFG["logs_dir"], "model-%s.log" % model["id"])


def _pid_alive(pid):
    if not pid:
        return False
    try:
        return psutil.pid_exists(int(pid))
    except Exception:
        return False


def _runtime_alive(rt):
    """运行时是否真存活：受管子进程看 poll()，外部接管的看 pid。"""
    if not rt:
        return False
    proc = rt.get("proc")
    if proc is not None:
        return proc.poll() is None
    return _pid_alive(rt.get("pid"))


def _is_alive(model_id):
    return _runtime_alive(_RUNTIME.get(model_id))


def _find_port_pid(port):
    """查找监听指定端口的进程 PID。"""
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and int(c.laddr.port) == int(port):
                return c.pid
    except Exception:
        pass
    return None


def _build_cmd_safe(model):
    try:
        return build_command(model)
    except Exception:
        return None


def attach(model):
    """接管外部已运行（非本进程启动）的 llama-server。

    若 _RUNTIME 无记录但内部端口健康，则注册一条“外部进程”运行时记录，
    使管理后台能正确显示 running/pid、并支持停止。返回是否已接管。
    """
    mid = model.get("id")
    if not mid:
        return False
    with _LOCK:
        rt = _RUNTIME.get(mid)
        if rt is not None and _runtime_alive(rt):
            return False
    ok, _detail = health(model, timeout=1.2)
    if not ok:
        return False
    if is_external_engine(model):
        with _LOCK:
            _RUNTIME[mid] = {
                "proc": None, "pid": None,
                "started_at": None,
                "port": model.get("internal_port"),
                "log": None, "cmd": None, "file": None,
                "external": True, "remote": True,
            }
        return True
    port = model.get("internal_port", CFG["internal_port_start"])
    pid = _find_port_pid(port)
    with _LOCK:
        _RUNTIME[mid] = {
            "proc": None,
            "pid": pid,
            "started_at": None,
            "port": port,
            "log": _log_path(model),
            "cmd": _build_cmd_safe(model),
            "file": None,
            "external": True,
        }
    return True


def get_runtime(model_id):
    with _LOCK:
        rt = _RUNTIME.get(model_id)
        if not rt:
            return {"running": False}
        alive = _runtime_alive(rt)
        proc = rt.get("proc")
        return {
            "running": bool(alive),
            "pid": rt.get("pid"),
            "started_at": rt.get("started_at"),
            "port": rt.get("port"),
            "log": rt.get("log"),
            "cmd": rt.get("cmd"),
            "external": bool(rt.get("external") or rt.get("remote")),
            "exit_code": None if alive else (proc.poll() if proc is not None else None),
        }


def list_running():
    return [mid for mid in list(_RUNTIME.keys()) if _is_alive(mid)]


def stop_all_except(keep_id=None, timeout=20):
    """停止除 keep_id 外的所有在运行的模型；返回被停止的 model_id 列表。"""
    stopped = []
    for mid in list_running():
        if keep_id and mid == keep_id:
            continue
        ok, _msg = stop(mid, timeout=timeout)
        if ok:
            stopped.append(mid)
    return stopped


def start(model):
    """启动 llama-server。返回 (ok, message)。"""
    mid = model["id"]
    with _LOCK:
        if _is_alive(mid):
            return False, "该模型已在运行（pid=%s）" % (_RUNTIME.get(mid, {}).get("pid"),)
        if is_external_engine(model):
            return False, "该模型为外部服务引擎，请在对应软件中启动后于此处「连接/刷新」"
        if engine_of(model) in ("llamacpp", "custom"):
            _exe = model_exe(model)
            if engine_of(model) == "llamacpp" and not os.path.isfile(_exe):
                return False, "未找到 llama-server.exe：%s" % _exe
        else:
            _exe = None
        try:
            cmd = build_command(model)
        except Exception as e:
            return False, str(e)

        port = model.get("internal_port", CFG["internal_port_start"])
        log = _log_path(model)
        f = open(log, "a", encoding="utf-8", errors="replace")
        f.write("\n\n===== START %s =====\n%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(cmd)))
        f.flush()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=(os.path.dirname(_exe) if _exe else None) or None,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception as e:
            f.write("启动失败: %s\n" % e)
            f.close()
            return False, "启动失败: %s" % e

        _RUNTIME[mid] = {
            "proc": proc,
            "pid": proc.pid,
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "port": port,
            "log": log,
            "cmd": cmd,
            "file": f,
        }
    return True, "已启动 (pid=%s, port=%s)" % (proc.pid, port)


def stop(model_id, timeout=15):
    with _LOCK:
        rt = _RUNTIME.get(model_id)
        if not rt:
            return False, "该模型未在运行"
        proc = rt.get("proc")
        ext_pid = rt.get("pid") if rt.get("external") else None
    if proc is None:
        if rt.get("remote"):
            with _LOCK:
                _RUNTIME.pop(model_id, None)
            return True, "已断开外部服务连接"
        # 外部接管的进程：按 PID 结束
        if ext_pid and _pid_alive(ext_pid):
            try:
                pp = psutil.Process(int(ext_pid))
                pp.terminate()
                try:
                    pp.wait(timeout=timeout)
                except psutil.TimeoutExpired:
                    pp.kill()
            except Exception as e:
                return False, "停止失败: %s" % e
        with _LOCK:
            _RUNTIME.pop(model_id, None)
        return True, "已停止"
    if proc.poll() is not None:
        with _LOCK:
            _RUNTIME.pop(model_id, None)
        return True, "进程已结束"
    try:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    except Exception as e:
        return False, "停止失败: %s" % e
    finally:
        with _LOCK:
            f = rt.get("file")
            if f:
                try:
                    f.close()
                except Exception:
                    pass
            _RUNTIME.pop(model_id, None)
    return True, "已停止"


def _external_base(model):
    """外部引擎的服务基址。"""
    b = (model.get("base_url") or "").strip()
    if b:
        return b.rstrip("/")
    host = model.get("host", "127.0.0.1")
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    default_port = 11434 if engine_of(model) == "ollama" else (1234 if engine_of(model) == "lmstudio" else CFG["internal_port_start"])
    port = model.get("internal_port") or default_port
    return "http://%s:%s" % (host, port)


def _external_headers(model):
    key = (model.get("api_key") or "").strip()
    if not key:
        return {}
    return {"Authorization": "Bearer " + key}


def health(model, timeout=3):
    """探测服务健康状态（受管引擎查 /health；外部引擎查 /v1/models）。"""
    if is_external_engine(model):
        base = _external_base(model)
        url = base + "/v1/models"
        try:
            r = requests.get(url, headers=_external_headers(model), timeout=timeout)
            if r.status_code == 200:
                try:
                    return True, {"external": True, "models": r.json()}
                except Exception:
                    return True, {"external": True}
            return False, "HTTP %s" % r.status_code
        except Exception as e:
            return False, str(e)
    host = model.get("host", "127.0.0.1")
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    port = model.get("internal_port")
    url = "http://%s:%s/health" % (host, port)
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return True, r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return False, "HTTP %s" % r.status_code
    except Exception as e:
        return False, str(e)


def wait_ready(model, max_wait=240):
    """等待模型加载完成（健康检查通过）。"""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        ok, _ = health(model, timeout=2)
        if ok:
            return True
        if not get_runtime(model["id"]).get("running"):
            return False
        time.sleep(2)
    return False


def tail_log(model_id, lines=80):
    rt = _RUNTIME.get(model_id)
    path = rt.get("log") if rt else None
    if not path:
        from .store import get_model
        m = get_model(model_id)
        path = _log_path(m) if m else None
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = f.readlines()
        return [ln.rstrip("\n") for ln in data[-lines:]]
    except Exception:
        return []


def stop_all():
    for mid in list(_RUNTIME.keys()):
        stop(mid)
