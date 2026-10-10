"""系统监控：CPU / 内存 / 磁盘 / GPU(NVIDIA) / 运行中模型。"""
import shutil
import subprocess

import psutil

from .config import CFG
from . import server_manager as sm

_IS_WIN = __import__("os").name == "nt"
_NO_WINDOW = 0x08000000 if _IS_WIN else 0


def _round(v, n=1):
    try:
        return round(float(v), n)
    except Exception:
        return v


def gpu_info():
    """通过 nvidia-smi 获取 GPU 信息。"""
    # 读取当前进行中的请求数（供状态徽章使用）
    active = 0
    try:
        from .active_reqs import ACTIVE_REQS
        active = sum(int(v) for v in ACTIVE_REQS.values() if int(v) > 0)
    except Exception:
        pass
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw,power.limit",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=8,
            creationflags=_NO_WINDOW,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return {"available": False, "error": (out.stderr or "nvidia-smi 无输出").strip()}
        gpus = []
        for line in out.stdout.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 8:
                continue
            util = int(float(parts[4]))
            # 显存占用 > 500 MB 但 GPU 利用率为 0 -> 模型已加载但当前空闲
            used_mb = int(float(parts[2]))
            if used_mb > 500:
                # 模型已加载驻显存
                if active > 0 or util > 0:
                    state = "busy"; state_label = "推理中"
                else:
                    state = "idle"; state_label = "模型已加载·空闲"
            else:
                state = "free"; state_label = "空闲"
            gpus.append({
                "name": parts[0],
                "mem_total_mb": int(float(parts[1])),
                "mem_used_mb": used_mb,
                "mem_free_mb": int(float(parts[3])),
                "util_percent": util,
                "temp_c": int(float(parts[5])),
                "power_w": _round(parts[6]),
                "power_limit_w": _round(parts[7]),
                "mem_used_gb": _round(used_mb / 1024, 2),
                "mem_total_gb": _round(float(parts[1]) / 1024, 2),
                "mem_percent": _round(used_mb / max(float(parts[1]), 1) * 100, 1),
                "state": state,
                "state_label": state_label,
            })
        return {"available": bool(gpus), "gpus": gpus}
    except FileNotFoundError:
        return {"available": False, "error": "未检测到 nvidia-smi"}
    except Exception as e:
        return {"available": False, "error": str(e)}


def disk_info():
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            if "cdrom" in part.opts or not part.fstype:
                continue
            u = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device": part.device,
                "mount": part.mountpoint,
                "fs": part.fstype,
                "total_gb": _round(u.total / 1024 ** 3, 1),
                "used_gb": _round(u.used / 1024 ** 3, 1),
                "free_gb": _round(u.free / 1024 ** 3, 1),
                "percent": u.percent,
            })
        except (PermissionError, OSError):
            continue
    return disks


def system_info():
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    cpu_freq = psutil.cpu_freq()
    return {
        "cpu": {
            "percent": psutil.cpu_percent(interval=0.3),
            "count_logical": psutil.cpu_count(True),
            "count_physical": psutil.cpu_count(False),
            "freq_mhz": _round(cpu_freq.current, 0) if cpu_freq else None,
        },
        "memory": {
            "total_gb": _round(vm.total / 1024 ** 3, 1),
            "used_gb": _round(vm.used / 1024 ** 3, 1),
            "available_gb": _round(vm.available / 1024 ** 3, 1),
            "percent": vm.percent,
        },
        "swap": {
            "total_gb": _round(sw.total / 1024 ** 3, 1),
            "used_gb": _round(sw.used / 1024 ** 3, 1),
            "percent": sw.percent,
        },
        "disk": disk_info(),
        "gpu": gpu_info(),
        "uptime_hours": _round((__import__("time").time() - psutil.boot_time()) / 3600, 1),
    }


def _model_status_one(m):
    """单个模型的状态（无共享可变状态，可安全并发调用）。"""
    rt = sm.get_runtime(m["id"])
    if not rt.get("running"):
        # 尝试接管外部已运行（未被本进程启动）的实例
        try:
            if sm.attach(m):
                rt = sm.get_runtime(m["id"])
        except Exception:
            pass
    healthy = False
    health_detail = {}
    if rt.get("running"):
        healthy, health_detail = sm.health(m, timeout=1.5)
    return {
        "id": m["id"],
        "name": m.get("name"),
        "display_name": m.get("display_name"),
        "alias": m.get("alias"),
        "port": m.get("internal_port"),
        "ctx": m.get("ctx"),
        "np": m.get("np"),
        "model_path": m.get("model_path"),
        "mmproj_path": m.get("mmproj_path"),
        "running": rt.get("running", False),
        "healthy": healthy,
        "external": rt.get("external", False),
        "pid": rt.get("pid"),
        "started_at": rt.get("started_at"),
        "health": health_detail,
    }


def models_status(models):
    """把模型配置与运行时状态合并。

    并发探测：单个模型的健康检查最坏要等满超时，串行会让状态接口耗时随模型
    数量线性增长（实测 4 个未运行模型串行 ≈ 4.8 秒）。并发后总耗时约等于
    最慢的单个模型，与模型数量基本无关。
    """
    if not models:
        return []
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(models))) as ex:
            return list(ex.map(_model_status_one, models))
    except Exception:
        # 并发不可用时退回串行，保证功能不受影响
        return [_model_status_one(m) for m in models]


def full_status(models):
    """总览：系统信息与模型状态并发采集，总耗时取两者较大者而非相加。"""
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_sys = ex.submit(system_info)
            f_mod = ex.submit(models_status, models)
            return {"system": f_sys.result(), "models": f_mod.result()}
    except Exception:
        return {"system": system_info(), "models": models_status(models)}
