"""数据持久化：模型配置、API Key、用量统计。使用原子写入 + 线程锁。"""
import hashlib
import json
import os
import secrets
import threading
import time

from .config import CFG

_LOCK = threading.RLock()

MODELS_FILE = os.path.join(CFG["data_dir"], "models.json")
KEYS_FILE = os.path.join(CFG["data_dir"], "keys.json")
USAGE_FILE = os.path.join(CFG["data_dir"], "usage.json")
SETTINGS_FILE = os.path.join(CFG["data_dir"], "settings.json")

HISTORY_LIMIT = 500


def _ensure_dirs():
    os.makedirs(CFG["data_dir"], exist_ok=True)
    os.makedirs(CFG["logs_dir"], exist_ok=True)


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _load(path, default):
    _ensure_dirs()
    if not os.path.exists(path):
        return default
    # 使用 utf-8-sig 兼容带/不带 BOM 的文件，避免因 BOM 解析失败而误认为空数据
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


# ---------------- 引擎目录 ----------------

ENGINES = [
    {
        "id": "llamacpp", "label": "llama.cpp / PrismML fork", "kind": "managed",
        "desc": "本地 GGUF 推理（llama-server）。支持 Bonsai 2 三值模型（需 PrismML fork）与常规 GGUF。",
        "fields": ["server_exe", "model_path", "mmproj_path", "internal_port", "ctx", "np",
                   "ctk", "ctv", "ngl", "batch", "ubatch", "threads", "temp", "top_p", "top_k",
                   "fa", "jinja", "kv_unified", "no_mmap", "cont_batching"],
    },
    {
        "id": "vllm", "label": "vLLM (OpenAI 兼容)", "kind": "managed",
        "desc": "本地 Safetensors/HF 模型高性能推理（需 een_dir 指向的 Python 环境，含 vllm）。",
        "fields": ["model_path", "internal_port", "ctx", "gpu_mem_util", "dtype", "tensor_parallel"],
    },
    {
        "id": "custom", "label": "自定义命令", "kind": "managed",
        "desc": "自行填写启动命令模板；支持占位符 {model_path} {port} {host} {ctx}。",
        "fields": ["command_template", "internal_port"],
    },
    {
        "id": "ollama", "label": "Ollama (外部服务)", "kind": "external",
        "desc": "连接本机/局域网已运行的 Ollama 服务（默认端口 11434）。",
        "fields": ["host", "internal_port", "model_tag"],
    },
    {
        "id": "lmstudio", "label": "LM Studio (外部服务)", "kind": "external",
        "desc": "连接 LM Studio 已开启的本地服务器（默认端口 1234）。",
        "fields": ["host", "internal_port"],
    },
    {
        "id": "openai", "label": "OpenAI 兼容接口 (外部)", "kind": "external",
        "desc": "接入任意 OpenAI 兼容端点（含云端），填 base_url 与 api_key。",
        "fields": ["base_url", "api_key"],
    },
]

ENGINE_MAP = {e["id"]: e for e in ENGINES}


def engine_of(model):
    eid = (model or {}).get("engine") or "llamacpp"
    return eid if eid in ENGINE_MAP else "llamacpp"


def is_external_engine(model):
    return ENGINE_MAP.get(engine_of(model), {}).get("kind") == "external"


# ---------------- 模型 ----------------

def default_model(**over):
    """返回一个带默认值的模型配置字典。"""
    m = {
        "id": secrets.token_hex(8),
        "name": "qwen3.8-27b",
        "display_name": "Qwen3.8-27B",
        "model_path": "",
        "mmproj_path": "",
        "server_exe": "",
        "engine": "llamacpp",
        "model_tag": "",
        "base_url": "",
        "api_key": "",
        "command_template": "",
        "gpu_mem_util": 0.9,
        "dtype": "auto",
        "tensor_parallel": 1,
        "alias": "",
        "description": "",
        "host": "127.0.0.1",
        "internal_port": CFG["internal_port_start"],
        "ctx": 131072,
        "np": 3,
        "kv_unified": True,
        "ctk": "q8_0",
        "ctv": "q8_0",
        "ngl": 999,
        "fa": True,
        "jinja": True,
        "cont_batching": True,
        "batch": 2048,
        "ubatch": 512,
        "threads": 0,
        "no_mmap": True,
        "temp": 0.7,
        "top_p": 0.95,
        "top_k": 20,
        "repeat_penalty": 1.0,
        # 思考模式（Qwen3 / Bonsai2 等支持思维链的模型）：
        #   reasoning_mode: auto=跟随模型默认 / on=强制开启 / off=关闭思考
        #   reasoning_budget: 思维链 token 预算，-1=不限，0=立即结束，N=最多 N 个 token
        "reasoning_mode": "auto",
        "reasoning_budget": -1,
        "extra_args": [],
        "autostart": False,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    m.update(over)
    return m


def get_models():
    with _LOCK:
        return _load(MODELS_FILE, [])


def save_models(models):
    with _LOCK:
        _atomic_write(MODELS_FILE, models)


def get_model(mid):
    for m in get_models():
        if m["id"] == mid:
            return m
    return None


def upsert_model(model):
    with _LOCK:
        models = get_models()
        for i, m in enumerate(models):
            if m["id"] == model["id"]:
                models[i] = model
                break
        else:
            models.append(model)
        save_models(models)
    return model


def delete_model(mid):
    with _LOCK:
        models = [m for m in get_models() if m["id"] != mid]
        save_models(models)


def resolve_model(model_name):
    """根据请求中的 model 字段解析模型（匹配 name / display_name / alias / id）。"""
    if not model_name:
        return None
    for m in get_models():
        for cand in (m.get("name"), m.get("display_name"), m.get("alias"), m.get("id")):
            if cand and cand == model_name:
                return m
    return None


# ---------------- API Key ----------------

def new_api_key(name="user", models=None, note=""):
    raw = "sk-" + secrets.token_urlsafe(32)
    return {
        "id": secrets.token_hex(8),
        "name": name,
        "key": raw,
        "enabled": True,
        "models": models if models else ["*"],
        "note": note,
        # 该用户的思考设置（inherit=跟随管理平台策略）
        "reasoning_mode": "inherit",    # inherit / on / off
        "reasoning_effort": "inherit",  # inherit / low / medium / xhigh
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_keys():
    with _LOCK:
        return _load(KEYS_FILE, [])


def save_keys(keys):
    with _LOCK:
        _atomic_write(KEYS_FILE, keys)


def find_key(token):
    if not token:
        return None
    for k in get_keys():
        if k.get("key") == token and k.get("enabled", True):
            return k
    return None


# ---------------- 用量 ----------------

def _blank_usage():
    return {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "last_used": None,
        "per_model": {},
        "history": [],
    }


def get_usage():
    with _LOCK:
        return _load(USAGE_FILE, {})


def save_usage(u):
    with _LOCK:
        _atomic_write(USAGE_FILE, u)


def _ip_pair(s):
    """把客户端标识规范化为 (ipv4, ipv6) 对。

    含 ':' 视为 IPv6，否则放在 IPv4 位（历史旧记录存的是 User-Agent，也落在 v4 位）。
    本机回环等特殊情况由调用方（gateway._client_pair）补齐对偶地址。
    """
    s = str(s or "").strip()
    if not s:
        return "", ""
    # IPv4-mapped IPv6，如 ::ffff:192.0.2.5（RFC 5737 文档示例地址）
    if s.lower().startswith("::ffff:") and "." in s:
        return s.split("::ffff:")[-1], ""
    if ":" in s:
        return "", s
    return s, ""


def _bucket_of(pt):
    """上下文长度分桶，便于观察长文/短问分布（不涉及内容）。"""
    try:
        pt = int(pt or 0)
    except Exception:
        pt = 0
    if pt < 1024:
        return "<1K"
    if pt < 8192:
        return "1-8K"
    if pt < 32768:
        return "8-32K"
    if pt < 131072:
        return "32-128K"
    return "128K+"


def record_usage(key_id, model_name, usage, latency_ms=None, ok=True, client="",
                 task_id="", status_code=200, error="", prompt_hash="", prompt_chars=0,
                 ttft_ms=None, tps=None, cached_tokens=0, client_ip4="", client_ip6=""):
    """记录一次调用。

    隐私口径：不保存请求内容原文，仅保存 prompt_hash(SHA-256 摘要) 与 prompt_chars(字符数)。
    """
    if not key_id:
        return
    # 来源地址：优先使用显式传入的 v4/v6 对，否则从 client 兜底解析
    if not client_ip4 and not client_ip6:
        client_ip4, client_ip6 = _ip_pair(client)
    pt = int((usage or {}).get("prompt_tokens", 0) or 0)
    ct = int((usage or {}).get("completion_tokens", 0) or 0)
    tt = int((usage or {}).get("total_tokens", pt + ct) or (pt + ct))
    with _LOCK:
        data = get_usage()
        u = data.get(key_id) or _blank_usage()
        u["requests"] += 1
        u["prompt_tokens"] += pt
        u["completion_tokens"] += ct
        u["total_tokens"] += tt
        u["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
        pm = u.setdefault("per_model", {})
        mm = pm.get(model_name) or {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        mm["requests"] += 1
        mm["prompt_tokens"] += pt
        mm["completion_tokens"] += ct
        mm["total_tokens"] += tt
        pm[model_name] = mm
        hist = u.setdefault("history", [])
        hist.insert(0, {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ts": time.time(),
            "task_id": task_id,
            "model": model_name,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": tt,
            "latency_ms": latency_ms,
            "ok": ok,
            "status_code": status_code,
            "error": error,
            # 隐私：不落原文，只落摘要与长度
            "prompt_hash": prompt_hash or "",
            "prompt_chars": int(prompt_chars or 0),
            "ttft_ms": ttft_ms,
            "tps": tps,
            "cached_tokens": int(cached_tokens or 0),
            "bucket": _bucket_of(pt),
            "client": client,
            # 来源地址：v4/v6 分开存，前端默认显示 IPv4、悬停显示 IPv6
            "client_ip4": client_ip4,
            "client_ip6": client_ip6,
        })
        del hist[HISTORY_LIMIT:]
        data[key_id] = u
        save_usage(data)


def aggregate_tasks(hist, page=0, page_size=30):
    """把调用历史按 task_id 聚合为任务列表（每行=一个任务），并分页返回。"""
    from collections import OrderedDict
    tasks = OrderedDict()
    for h in reversed(hist):
        tid = h.get("task_id") or ("orphan-%s-%s-%s" % (h.get("time", ""), h.get("model", ""), h.get("prompt_tokens", 0)))
        t = tasks.get(tid)
        if t is None:
            t = {
                "task_id": tid,
                "first_time": h.get("time", ""),
                "last_time": h.get("time", ""),
                "prompt_hash": h.get("prompt_hash", "") or "",
                "prompt_chars": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "models": {},
                "calls": 0,
                "has_error": False,
                "has_ok": False,
                "_lat": 0.0,
                "_ttft": 0.0,
                "_tps": 0.0,
                "_lat_n": 0,
                "_ttft_n": 0,
                "_tps_n": 0,
                "cached_tokens": 0,
                "client": "",
                "client_ip4": "",
                "client_ip6": "",
            }
            tasks[tid] = t
        t["prompt_tokens"] += int(h.get("prompt_tokens", 0) or 0)
        t["completion_tokens"] += int(h.get("completion_tokens", 0) or 0)
        t["total_tokens"] += int(h.get("total_tokens", 0) or 0)
        t["prompt_chars"] += int(h.get("prompt_chars", 0) or 0)
        t["cached_tokens"] += int(h.get("cached_tokens", 0) or 0)
        if not t["client_ip4"] and not t["client_ip6"]:
            _c4 = h.get("client_ip4", "") or ""
            _c6 = h.get("client_ip6", "") or ""
            if not _c4 and not _c6:
                # 历史旧记录只有 client（可能是 IP 也可能是 UA）
                _c4, _c6 = _ip_pair(h.get("client", ""))
            if _c4:
                t["client_ip4"] = _c4
            if _c6:
                t["client_ip6"] = _c6
            if _c4 or _c6:
                t["client"] = (_c4 or _c6)
        if h.get("latency_ms"):
            t["_lat"] += float(h.get("latency_ms") or 0)
            t["_lat_n"] += 1
        if h.get("ttft_ms"):
            t["_ttft"] += float(h.get("ttft_ms") or 0)
            t["_ttft_n"] += 1
        if h.get("tps"):
            t["_tps"] += float(h.get("tps") or 0)
            t["_tps_n"] += 1
        m = h.get("model", "未知")
        t["models"][m] = t["models"].get(m, 0) + 1
        t["calls"] += 1
        sc = int(h.get("status_code", 200) or 200)
        okflag = h.get("ok", True)
        if okflag and 200 <= sc < 300 and not h.get("error"):
            t["has_ok"] = True
        else:
            t["has_error"] = True
        ht = h.get("time", "") or ""
        if ht and (not t["last_time"] or ht >= t["last_time"]):
            t["last_time"] = ht
        if not t["prompt_hash"] and h.get("prompt_hash"):
            t["prompt_hash"] = h.get("prompt_hash", "")
    task_list = []
    for t in tasks.values():
        if t["has_error"] and t["has_ok"]:
            t["status"] = "partial"
        elif t["has_error"]:
            t["status"] = "failed"
        else:
            t["status"] = "success"
        t["model"] = max(t["models"].items(), key=lambda x: x[1])[0] if t["models"] else "未知"
        t["model_count"] = len(t["models"])
        t["latency_ms"] = int(t["_lat"] / t["_lat_n"]) if t["_lat_n"] else 0
        t["ttft_ms"] = int(t["_ttft"] / t["_ttft_n"]) if t["_ttft_n"] else 0
        t["tps"] = round(t["_tps"] / t["_tps_n"], 1) if t["_tps_n"] else 0
        # 展示用主地址：默认 IPv4，没有则用 IPv6
        t["client"] = (t.get("client_ip4") or t.get("client_ip6") or "")
        t.pop("models", None)
        t.pop("has_error", None)
        t.pop("has_ok", None)
        for _k in ("_lat", "_ttft", "_tps", "_lat_n", "_ttft_n", "_tps_n"):
            t.pop(_k, None)
        task_list.append(t)
    task_list.sort(key=lambda x: x["first_time"], reverse=True)
    total = len(task_list)
    start = max(0, page) * page_size
    return task_list[start:start + page_size], total


def reset_usage(key_id=None):
    with _LOCK:
        if key_id is None:
            save_usage({})
        else:
            data = get_usage()
            data.pop(key_id, None)
            save_usage(data)


# ---------------- 全局设置 ----------------

_GENERAL_DEFAULTS = {
    "exclusive_mode": True,
    "ctx_guard": True,
    # ---- 思考控制（全局默认，作为"模型级未明确指定"时的兜底） ----
    "reasoning_default_mode": "inherit",    # inherit=跟随模型 / on=强制开启 / off=关闭思考
    "reasoning_default_effort": "inherit",  # inherit / low / medium / xhigh（思考强度）
    "reasoning_allow_user_override": True,  # 是否允许用户（API Key）与客户端自行覆盖
    # ---- 思考预留（防"思维链/长回答吃光输出额度"导致前端 MAX_TOKENS 截断） ----
    # 开启思考时，网关把上游 max_tokens 抬高这么多，保证正式回答始终有空间；0=关闭该保护
    # 实测：客户端申请 8192 时，Bonsai2 单轮自然输出可达 ~12.8k，故取 8192 作为默认。
    "thinking_headroom": 8192,
    # ---- 模型切换保护（按需拉起时，不打断正在被别人使用的模型）----
    # 距某模型上次请求结束不足该秒数，视为"仍在使用"，不允许被切换到别的模型；
    # 0 = 严格只按在途请求判断（请求一结束就允许切换）。
    "switch_grace_sec": 60,
}

# 思考模式与强度的合法取值（供接口层校验复用）
REASONING_MODES = ("inherit", "on", "off")
REASONING_EFFORTS = ("inherit", "low", "medium", "xhigh")


def get_general_settings():
    """运行模式等全局设置。"""
    with _LOCK:
        raw = _load(SETTINGS_FILE, {})
        g = dict(_GENERAL_DEFAULTS)
        for k, v in (raw.get("general") or {}).items():
            g[k] = v
        return g


def set_general_settings(updates):
    with _LOCK:
        raw = _load(SETTINGS_FILE, {})
        g = dict(_GENERAL_DEFAULTS)
        g.update(raw.get("general") or {})
        g.update(updates or {})
        raw["general"] = g
        _atomic_write(SETTINGS_FILE, raw)
        return g


def get_settings():
    with _LOCK:
        return _load(SETTINGS_FILE, {"admin_username": "admin", "admin_password": "A123654789"})


def save_settings(s):
    with _LOCK:
        _atomic_write(SETTINGS_FILE, s)

# ---------------- 使用登记（谁在用 / 预约 / 记录） ----------------

BOOKINGS_FILE = os.path.join(CFG["data_dir"], "bookings.json")
BOOKING_LIMIT = 500
_BOOKING_STATES = ("using", "reserved", "done")


def _now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def get_bookings():
    """读取使用登记原始数据。"""
    with _LOCK:
        d = _load(BOOKINGS_FILE, {"items": []})
        if not isinstance(d, dict):
            d = {"items": []}
        if not isinstance(d.get("items"), list):
            d["items"] = []
        return d


def view_bookings():
    """返回分类视图：active(使用中) / reserved(预约申请) / done(历史记录)。"""
    d = get_bookings()
    items = list(d.get("items") or [])
    active, reserved, done = [], [], []
    for it in items:
        st = it.get("status") or "using"
        if st == "using":
            active.append(it)
        elif st == "reserved":
            reserved.append(it)
        else:
            done.append(it)
    active.sort(key=lambda x: x.get("started_at") or "")
    reserved.sort(key=lambda x: x.get("created_at") or "")
    done.sort(key=lambda x: x.get("ended_at") or x.get("updated_at") or x.get("created_at") or "",
              reverse=True)
    return {
        "items": items,
        "active": active,
        "reserved": reserved,
        "done": done[:50],
        "now": _now_str(),
        "counts": {"active": len(active), "reserved": len(reserved), "done": len(done)},
    }


def add_booking(user, status="using", time_text="", note="", operator=""):
    """新增一条使用登记。status: using(正在使用) / reserved(提前申请) / done(使用完毕)。"""
    if status not in _BOOKING_STATES:
        status = "using"
    now = _now_str()
    with _LOCK:
        d = get_bookings()
        rec = {
            "id": secrets.token_hex(6),
            "user": (user or "").strip()[:40],
            "status": status,
            "time_text": (time_text or "").strip()[:120],
            "note": (note or "").strip()[:200],
            "operator": (operator or "")[:40],
            "created_at": now,
            "updated_at": now,
            "started_at": now if status == "using" else "",
            "ended_at": now if status == "done" else "",
        }
        items = list(d.get("items") or [])
        items.insert(0, rec)
        d["items"] = items[:BOOKING_LIMIT]
        _atomic_write(BOOKINGS_FILE, d)
        return rec


def update_booking(bid, fields):
    """更新指定登记；状态切换时自动补齐 started_at / ended_at。"""
    with _LOCK:
        d = get_bookings()
        items = list(d.get("items") or [])
        for it in items:
            if it.get("id") != bid:
                continue
            for k in ("user", "time_text", "note", "status"):
                if k in fields and fields[k] is not None:
                    it[k] = fields[k]
            if it.get("status") not in _BOOKING_STATES:
                it["status"] = "using"
            now = _now_str()
            if it["status"] == "using" and not it.get("started_at"):
                it["started_at"] = now
            if it["status"] == "done":
                if not it.get("started_at"):
                    it["started_at"] = it.get("created_at") or now
                it["ended_at"] = now
            it["updated_at"] = now
            _atomic_write(BOOKINGS_FILE, d)
            return it
        return None


def delete_booking(bid):
    with _LOCK:
        d = get_bookings()
        items = list(d.get("items") or [])
        new = [x for x in items if x.get("id") != bid]
        if len(new) == len(items):
            return False
        d["items"] = new
        _atomic_write(BOOKINGS_FILE, d)
        return True

# ---------------- 用户账户（账户管理 / RBAC） ----------------

USERS_FILE = os.path.join(CFG["data_dir"], "users.json")
LOGS_FILE = os.path.join(CFG["data_dir"], "logs.json")
LOG_LIMIT = 3000
ROLES = ("admin", "user")


def _hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.sha256((salt + (pw or "")).encode("utf-8")).hexdigest()
    return salt + "$" + h


def _verify_pw(pw, stored):
    if not stored:
        return False
    if "$" not in stored:          # 兼容历史明文
        return pw == stored
    salt, h = stored.split("$", 1)
    return hashlib.sha256((salt + (pw or "")).encode("utf-8")).hexdigest() == h


def _bootstrap_users():
    st = _load(SETTINGS_FILE, {}) or {}
    uname = (st.get("admin_username") or "admin").strip() or "admin"
    pw = st.get("admin_password") or "A123654789"
    now = _now_str()
    return {"users": [{
        "id": "u-admin", "username": uname, "display_name": "管理员",
        "password": _hash_pw(pw), "role": "admin", "enabled": True,
        "created_at": now, "last_login": "", "note": "系统内置管理员",
    }]}


def get_users():
    with _LOCK:
        d = _load(USERS_FILE, None)
        if (not isinstance(d, dict) or not isinstance(d.get("users"), list)
                or not d.get("users")):
            d = _bootstrap_users()
            _atomic_write(USERS_FILE, d)
        return d


def save_users(d):
    with _LOCK:
        _atomic_write(USERS_FILE, d)


def find_user(username):
    uname = (username or "").strip().lower()
    if not uname:
        return None
    for u in get_users().get("users", []):
        if (u.get("username") or "").strip().lower() == uname:
            return u
    return None


def find_user_by_id(uid):
    for u in get_users().get("users", []):
        if u.get("id") == uid:
            return u
    return None


def public_user(u):
    return {k: v for k, v in (u or {}).items() if k != "password"}


def verify_user(username, pw):
    u = find_user(username)
    if not u or not u.get("enabled", True):
        return None
    if _verify_pw(pw, u.get("password")):
        return u
    return None


def count_admins(exclude_uid=None):
    return sum(1 for u in get_users().get("users", [])
               if u.get("role") == "admin" and u.get("enabled", True)
               and u.get("id") != exclude_uid)


def create_user(username, password, role="user", display_name="", note=""):
    username = (username or "").strip()
    if not username:
        raise ValueError("用户名不能为空")
    if len(username) > 32:
        raise ValueError("用户名过长（≤32）")
    if find_user(username):
        raise ValueError("用户名已存在")
    if role not in ROLES:
        role = "user"
    u = {
        "id": "u-" + secrets.token_hex(5),
        "username": username,
        "display_name": (display_name or username).strip()[:32],
        "password": _hash_pw(password or "123456"),
        "role": role,
        "enabled": True,
        "created_at": _now_str(),
        "last_login": "",
        "note": (note or "")[:120],
    }
    with _LOCK:
        d = get_users()
        d["users"].append(u)
        _atomic_write(USERS_FILE, d)
    return u


def update_user(uid, fields):
    with _LOCK:
        d = get_users()
        for u in d.get("users", []):
            if u.get("id") != uid:
                continue
            if "username" in fields and fields["username"]:
                newn = fields["username"].strip()
                for o in d["users"]:
                    if o is not u and (o.get("username") or "").lower() == newn.lower():
                        raise ValueError("用户名已存在")
                u["username"] = newn[:32]
            if "display_name" in fields:
                u["display_name"] = (fields.get("display_name") or "").strip()[:32]
            if "note" in fields:
                u["note"] = (fields.get("note") or "")[:120]
            if "role" in fields:
                r = fields["role"]
                if r not in ROLES:
                    raise ValueError("角色不合法")
                if r != "admin" and u.get("role") == "admin" and count_admins(exclude_uid=uid) == 0:
                    raise ValueError("至少需保留一名管理员")
                u["role"] = r
            if "enabled" in fields:
                en = bool(fields["enabled"])
                if not en and u.get("role") == "admin" and count_admins(exclude_uid=uid) == 0:
                    raise ValueError("至少需保留一名启用的管理员")
                u["enabled"] = en
            if fields.get("password"):
                u["password"] = _hash_pw(fields["password"])
            _atomic_write(USERS_FILE, d)
            return u
        return None


def delete_user(uid):
    with _LOCK:
        d = get_users()
        target = None
        for u in d.get("users", []):
            if u.get("id") == uid:
                target = u
                break
        if not target:
            return False
        if target.get("role") == "admin" and count_admins(exclude_uid=uid) == 0:
            raise ValueError("至少需保留一名管理员，无法删除")
        d["users"] = [u for u in d["users"] if u.get("id") != uid]
        _atomic_write(USERS_FILE, d)
        return True


def touch_login(uid):
    with _LOCK:
        d = get_users()
        for u in d.get("users", []):
            if u.get("id") == uid:
                u["last_login"] = _now_str()
                _atomic_write(USERS_FILE, d)
                return


# ---------------- 使用日志（审计） ----------------

def append_log(user, role, action, target="", detail="", ip="", result="ok"):
    rec = {
        "id": secrets.token_hex(6),
        "time": _now_str(),
        "user": (user or "-")[:40],
        "role": (role or "-")[:20],
        "action": (action or "-")[:60],
        "target": (target or "")[:120],
        "detail": (detail or "")[:300],
        "ip": (ip or "-")[:64],
        "result": result or "ok",
    }
    with _LOCK:
        d = _load(LOGS_FILE, {"items": []})
        if not isinstance(d, dict):
            d = {"items": []}
        items = list(d.get("items") or [])
        items.insert(0, rec)
        d["items"] = items[:LOG_LIMIT]
        _atomic_write(LOGS_FILE, d)
    return rec


def get_logs(limit=200, offset=0, user="", action="", result="", q="", role=""):
    d = _load(LOGS_FILE, {"items": []})
    if not isinstance(d, dict):
        d = {"items": []}
    items = list(d.get("items") or [])
    user = (user or "").strip().lower()
    action = (action or "").strip().lower()
    result = (result or "").strip().lower()
    role = (role or "").strip().lower()
    q = (q or "").strip().lower()

    def match(it):
        if user and user not in (it.get("user") or "").lower():
            return False
        if action and action not in (it.get("action") or "").lower():
            return False
        if result and result != (it.get("result") or "").lower():
            return False
        if role and role != (it.get("role") or "").lower():
            return False
        if q:
            blob = " ".join(str(it.get(k) or "") for k in
                            ("user", "action", "target", "detail", "ip")).lower()
            if q not in blob:
                return False
        return True

    filtered = [it for it in items if match(it)]
    total = len(filtered)
    try:
        offset = max(0, int(offset))
    except Exception:
        offset = 0
    try:
        limit = max(1, min(1000, int(limit)))
    except Exception:
        limit = 200
    page = filtered[offset:offset + limit]
    by_role = {}
    for it in items:
        k = (it.get("role") or "-")
        by_role[k] = by_role.get(k, 0) + 1
    return {
        "items": page,
        "total": total,
        "all": len(items),
        "by_role": by_role,
        "limit": limit,
        "offset": offset,
        "actions": sorted({(it.get("action") or "") for it in items if it.get("action")}),
    }


def delete_log(lid):
    with _LOCK:
        d = _load(LOGS_FILE, {"items": []})
        items = list((d or {}).get("items") or [])
        new = [x for x in items if x.get("id") != lid]
        if len(new) == len(items):
            return False
        d["items"] = new
        _atomic_write(LOGS_FILE, d)
        return True


def clear_logs():
    with _LOCK:
        _atomic_write(LOGS_FILE, {"items": []})
        return True
