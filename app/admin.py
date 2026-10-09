"""管理后台 API：模型、密钥、用量、监控、设置、文件浏览、对话测试。"""
import base64
import json
import os
import secrets
import time
import uuid

import requests
from functools import wraps

from flask import Flask, Response, g, jsonify, request

from .config import CFG
from . import server_manager as sm
from . import monitor
from . import store

app = Flask(__name__)

# 持久化 token：管理进程重启不丢
TOKENS_FILE = os.path.join(CFG["data_dir"], "tokens.json")

# 验证码：uuid -> (code, expire_ts, attempts)
_CAPTCHAS = {}
_CAPTCHA_TTL = 5 * 60            # 5 分钟
_CAPTCHA_MAX_TRY = 5             # 单 uuid 最多错 5 次（之后失效）

# 账号失败计数：username -> [ts...]
_LOGIN_FAILS = {}
_LOGIN_LOCK_AFTER = 5            # 5 次错误
_LOGIN_LOCK_WINDOW = 10 * 60     # 10 分钟窗口
_LOGIN_LOCK_TTL = 15 * 60        # 锁定 15 分钟

_TOKEN_TTL = 7 * 24 * 3600       # 7 天


def _load_sessions():
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f) or {}
            now = time.time()
            out = {}
            for k, v in d.items():
                if isinstance(v, dict):
                    if float(v.get("exp", 0)) > now:
                        out[k] = v
                else:
                    try:
                        if float(v) > now:
                            out[k] = {"uid": "u-admin", "role": "admin",
                                      "exp": float(v), "created": now}
                    except Exception:
                        pass
            return out
        except Exception:
            return {}
    return {}


def _save_sessions():
    try:
        with open(TOKENS_FILE, "w", encoding="utf-8") as f:
            json.dump(_SESSIONS, f, ensure_ascii=False)
    except Exception:
        pass


_SESSIONS = _load_sessions()


def _extract_token():
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-Auth-Token") or "").strip()


def _issue_token(user):
    t = secrets.token_urlsafe(24)
    _SESSIONS[t] = {
        "uid": user.get("id"),
        "role": user.get("role"),
        "exp": time.time() + _TOKEN_TTL,
        "created": time.time(),
    }
    _save_sessions()
    return t


def _revoke_token(t):
    _SESSIONS.pop(t, None)
    _save_sessions()


def _client_ip():
    try:
        xf = request.headers.get("X-Forwarded-For", "")
        if xf:
            return xf.split(",")[0].strip()[:64]
        return (request.remote_addr or "-")[:64]
    except Exception:
        return "-"


def _cur_user():
    return getattr(g, "user", None) or {}


def _cur_role():
    return _cur_user().get("role") or ""


def _log(action, target="", detail="", result="ok"):
    try:
        u = _cur_user()
        store.append_log(u.get("username"), u.get("role"), action,
                         target=target, detail=detail, ip=_client_ip(), result=result)
    except Exception:
        pass


def _check_auth():
    t = _extract_token()
    sess = _SESSIONS.get(t)
    if not sess or float(sess.get("exp", 0)) <= time.time():
        return False
    u = store.find_user_by_id(sess.get("uid"))
    return bool(u and u.get("enabled", True))


def require_auth(fn):
    def wrapper(*a, **k):
        t = _extract_token()
        sess = _SESSIONS.get(t)
        if not sess or float(sess.get("exp", 0)) <= time.time():
            _SESSIONS.pop(t, None)
            return jsonify({"ok": False, "error": "未登录或登录已过期"}), 401
        u = store.find_user_by_id(sess.get("uid"))
        if not u or not u.get("enabled", True):
            _SESSIONS.pop(t, None)
            _save_sessions()
            return jsonify({"ok": False, "error": "账号不可用或已被禁用"}), 401
        g.user = u
        g.token = t
        if u.get("role") != sess.get("role"):
            sess["role"] = u.get("role")
            _save_sessions()
        return fn(*a, **k)
    wrapper.__name__ = fn.__name__
    return wrapper


def require_admin(fn):
    def wrapper(*a, **k):
        if _cur_role() != "admin":
            return jsonify({"ok": False, "error": "需要管理员权限"}), 403
        return fn(*a, **k)
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------------- 使用日志（操作审计） ----------------

_AUDIT_LABELS = [
    ("/api/users", "POST", "新建用户"),
    ("/api/users", "PUT", "修改用户"),
    ("/api/users", "DELETE", "删除用户"),
    ("/api/bookings", "POST", "登记使用"),
    ("/api/bookings", "PUT", "修改登记"),
    ("/api/bookings", "DELETE", "删除使用记录"),
    ("/api/models", "POST", "新建模型"),
    ("/api/models", "PUT", "编辑模型"),
    ("/api/models", "DELETE", "删除模型"),
    ("/api/keys", "POST", "新建密钥"),
    ("/api/keys", "PUT", "编辑密钥"),
    ("/api/keys", "DELETE", "删除密钥"),
    ("/api/settings/general", "POST", "修改设置"),
    ("/api/usage/reset", "POST", "重置用量"),
    ("/api/change_password", "POST", "修改密码"),
]


def _audit_label(path, method):
    if path.endswith("/start"):
        return "启动模型"
    if path.endswith("/stop"):
        return "停止模型"
    if path.endswith("/restart"):
        return "重启模型"
    if path.endswith("/connect"):
        return "接管外部模型"
    if path.endswith("/finish"):
        return "使用完毕"
    for pref, m, label in _AUDIT_LABELS:
        if path.startswith(pref) and method == m:
            return label
    return None


_AUDIT_SKIP = ("/api/login", "/api/logout", "/api/captcha",
               "/api/auth/verify", "/api/chat", "/api/logs",
               "/api/user/thinking")


@app.after_request
def _audit_hook(response):
    try:
        if request.method in ("POST", "PUT", "DELETE") and request.path.startswith("/api/"):
            p = request.path
            if not any(p.startswith(x) for x in _AUDIT_SKIP):
                label = _audit_label(p, request.method)
                u = _cur_user()
                if label and u:
                    store.append_log(
                        u.get("username"), u.get("role"), label,
                        target=p, detail="%s %s" % (request.method, p),
                        ip=_client_ip(),
                        result=("ok" if response.status_code < 400 else "fail"),
                    )
    except Exception:
        pass
    return response





def _ok(data=None, **kw):
    if data is None:
        data = {}
    if kw:
        if not isinstance(data, dict):
            data = {"value": data}
        data.update(kw)
    return jsonify({"ok": True, "data": data})


def _err(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


# ---------------- 登录 ----------------

# ---------------- 验证码（SVG，纯标准库，无 PIL 依赖） ----------------
_CAPTCHA_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 去掉易混淆的 0/O/1/I/l
_CAPTCHA_LEN = 4


def _gen_captcha_code():
    return "".join(secrets.choice(_CAPTCHA_CHARS) for _ in range(_CAPTCHA_LEN))


def _make_captcha_svg(code):
    import random as _r
    W, H = 140, 44
    # 随机颜色
    def c():
        return "#%02x%02x%02x" % (_r.randint(40, 200), _r.randint(40, 200), _r.randint(40, 200))
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" viewBox="0 0 %d %d">' % (W, H, W, H)]
    # 背景渐变
    parts.append('<rect width="100%" height="100%" fill="#0d1117"/>')
    parts.append('<defs><linearGradient id="g" x1="0" x2="1" y1="0" y2="1">'
                '<stop offset="0" stop-color="#161b22"/>'
                '<stop offset="1" stop-color="#1f2630"/></linearGradient></defs>')
    parts.append('<rect width="100%" height="100%" fill="url(#g)"/>')
    # 干扰线
    for _ in range(4):
        x1, y1 = _r.randint(0, W), _r.randint(0, H)
        x2, y2 = _r.randint(0, W), _r.randint(0, H)
        parts.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s" stroke-width="1" opacity="0.6"/>' % (x1, y1, x2, y2, c()))
    # 干扰点
    for _ in range(40):
        x, y = _r.randint(0, W), _r.randint(0, H)
        parts.append('<circle cx="%d" cy="%d" r="1" fill="%s" opacity="0.7"/>' % (x, y, c()))
    # 字符
    for i, ch in enumerate(code):
        x = 18 + i * 28 + _r.randint(-3, 3)
        y = 30 + _r.randint(-3, 3)
        rot = _r.randint(-18, 18)
        fs = 24 + _r.randint(0, 4)
        parts.append('<text x="%d" y="%d" font-family="Consolas,monospace" font-size="%d" font-weight="700" '
                    'fill="%s" transform="rotate(%d %d %d)">%s</text>' % (x, y, fs, c(), rot, x, y, ch))
    # 边框
    parts.append('<rect x="0.5" y="0.5" width="%d" height="%d" fill="none" stroke="#2d3748"/>' % (W - 1, H - 1))
    parts.append("</svg>")
    return "".join(parts)


@app.route("/api/captcha", methods=["GET"])
def api_captcha():
    cid = uuid.uuid4().hex
    code = _gen_captcha_code()
    _CAPTCHAS[cid] = [code, time.time() + _CAPTCHA_TTL, 0]
    svg = _make_captcha_svg(code)
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return _ok({"uuid": cid, "image": "data:image/svg+xml;base64," + b64, "ttl": _CAPTCHA_TTL})


def _check_captcha(uuid_str, code):
    """校验并消费验证码。返回 (ok, err_msg)。"""
    if not uuid_str or not code:
        return False, "请输入验证码"
    rec = _CAPTCHAS.get(uuid_str)
    if not rec:
        return False, "验证码已失效，请刷新"
    expected, expire, attempts = rec
    if expire < time.time():
        _CAPTCHAS.pop(uuid_str, None)
        return False, "验证码已过期，请刷新"
    if attempts >= _CAPTCHA_MAX_TRY:
        _CAPTCHAS.pop(uuid_str, None)
        return False, "验证码尝试次数过多，请刷新"
    if code.strip().upper() != expected:
        rec[2] += 1
        return False, "验证码错误"
    # 成功：消费
    _CAPTCHAS.pop(uuid_str, None)
    return True, ""


def _check_login_lock(username):
    """账号级失败锁定。"""
    arr = _LOGIN_FAILS.get(username, [])
    now = time.time()
    arr = [t for t in arr if now - t < _LOGIN_LOCK_WINDOW]
    if len(arr) >= _LOGIN_LOCK_AFTER:
        _LOGIN_FAILS[username] = arr
        return True, _LOGIN_LOCK_TTL - (now - arr[0])
    _LOGIN_FAILS[username] = arr
    return False, 0


def _record_login_fail(username):
    arr = _LOGIN_FAILS.setdefault(username, [])
    now = time.time()
    arr.append(now)
    arr[:] = [t for t in arr if now - t < _LOGIN_LOCK_WINDOW]


def _clear_login_fail(username):
    _LOGIN_FAILS.pop(username, None)


@app.route("/api/auth/verify", methods=["GET"])
def api_auth_verify():
    t = _extract_token()
    sess = _SESSIONS.get(t)
    if not sess or float(sess.get("exp", 0)) <= time.time():
        return _ok({"valid": False})
    u = store.find_user_by_id(sess.get("uid"))
    if not u or not u.get("enabled", True):
        return _ok({"valid": False})
    return _ok({"valid": True, "username": u.get("username"),
                "role": u.get("role"),
                "display_name": u.get("display_name") or u.get("username")})


@app.route("/api/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    pwd = body.get("password") or ""
    cid = (body.get("captcha_uuid") or "").strip()
    ccode = (body.get("captcha_code") or "").strip()
    ip = _client_ip()
    if not username:
        return _err("请输入账号")
    locked, wait = _check_login_lock(username)
    if locked:
        return _err("账号已锁定，请 %d 秒后再试" % int(wait), 429)
    ok, err = _check_captcha(cid, ccode)
    if not ok:
        _record_login_fail(username)
        return _err(err, 400)
    u = store.verify_user(username, pwd)
    if not u:
        _record_login_fail(username)
        locked, wait = _check_login_lock(username)
        msg = "账号或密码错误"
        if locked:
            msg += "，已临时锁定 %d 秒" % int(wait)
        store.append_log(username or "-", "-", "登录失败",
                         detail="账号或密码错误", ip=ip, result="fail")
        return _err(msg, 401)
    _clear_login_fail(username)
    store.touch_login(u["id"])
    tok = _issue_token(u)
    store.append_log(u.get("username"), u.get("role"), "登录",
                     detail="登录系统", ip=ip, result="ok")
    return _ok({"token": tok, "username": u.get("username"),
                "role": u.get("role"),
                "display_name": u.get("display_name") or u.get("username"),
                "ttl": _TOKEN_TTL})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    u = _cur_user()
    t = _extract_token()
    if u:
        store.append_log(u.get("username"), u.get("role"), "退出登录", ip=_client_ip())
    _revoke_token(t)
    return _ok()


@app.route("/api/change_password", methods=["POST"])
@require_auth
def change_password():
    body = request.get_json(silent=True) or {}
    u = _cur_user()
    if not store._verify_pw(body.get("old"), u.get("password")):
        return _err("原密码错误")
    new = (body.get("new") or "").strip()
    if not new:
        return _err("新密码不能为空")
    if len(new) < 6:
        return _err("新密码至少 6 位")
    store.update_user(u["id"], {"password": new})
    return _ok()


@app.route("/api/engines", methods=["GET"])
@require_auth
def list_engines():
    """返回支持的推理引擎目录（供“新增/编辑模型”表单适配）。"""
    return _ok(engines=store.ENGINES)


@app.route("/api/overview", methods=["GET"])
@require_auth
def overview():
    models = store.get_models()
    keys = store.get_keys()
    usage = store.get_usage()
    total_tokens = sum(u.get("total_tokens", 0) for u in usage.values())
    return _ok({
        "status": monitor.full_status(models),
        "counts": {"models": len(models), "keys": len(keys), "running": len(sm.list_running())},
        "total_tokens": total_tokens,
        "server_exe": CFG["server_exe"],
        "exe_available": sm.exe_available(),
        "exclusive_mode": store.get_general_settings().get("exclusive_mode", True),
        "gateway": "%s:%s" % (CFG["gateway_host"], CFG["gateway_port"]),
    })


# ---------------- 模型管理 ----------------

@app.route("/api/models", methods=["GET"])
@require_auth
def list_models():
    return _ok(models=store.get_models(), status=monitor.models_status(store.get_models()))


@app.route("/api/models", methods=["POST"])
@require_auth
@require_admin
def create_model():
    body = request.get_json(silent=True) or {}
    m = store.default_model()
    for k, v in body.items():
        if k in m:
            m[k] = v
    # 自动分配内部端口
    used = {x.get("internal_port") for x in store.get_models()}
    port = CFG["internal_port_start"]
    while port in used:
        port += 1
    if not body.get("internal_port"):
        m["internal_port"] = port
    store.upsert_model(m)
    return _ok(model=m)


@app.route("/api/models/<mid>", methods=["GET"])
@require_auth
def get_model(mid):
    m = store.get_model(mid)
    if not m:
        return _err("模型不存在", 404)
    return _ok(model=m, runtime=sm.get_runtime(mid))


@app.route("/api/models/<mid>", methods=["PUT"])
@require_auth
@require_admin
def update_model(mid):
    m = store.get_model(mid)
    if not m:
        return _err("模型不存在", 404)
    body = request.get_json(silent=True) or {}
    was_running = sm.get_runtime(mid).get("running")
    for k, v in body.items():
        if k in m:
            m[k] = v
    store.upsert_model(m)
    # 运行中则自动重启以使配置生效
    restarted = False
    if was_running:
        sm.stop(mid)
        ok, _ = sm.start(m)
        restarted = ok
    return _ok(model=m, restarted=restarted)


@app.route("/api/models/<mid>/thinking", methods=["POST"])
@require_auth
@require_admin
def set_model_thinking(mid):
    """思考模式开关：快速切换 reasoning_mode / reasoning_budget。

    仅改这两项配置；若模型运行中则自动重启使其生效。
    """
    m = store.get_model(mid)
    if not m:
        return _err("模型不存在", 404)
    body = request.get_json(silent=True) or {}
    if "reasoning_mode" in body:
        mode = str(body.get("reasoning_mode") or "auto").lower()
        if mode not in ("auto", "on", "off"):
            return _err("reasoning_mode 只能为 auto / on / off")
        m["reasoning_mode"] = mode
    if "reasoning_budget" in body:
        try:
            m["reasoning_budget"] = int(body.get("reasoning_budget"))
        except Exception:
            return _err("reasoning_budget 需为整数（-1 表示不限）")
    store.upsert_model(m)
    restarted = False
    if sm.get_runtime(mid).get("running"):
        sm.stop(mid)
        ok, _ = sm.start(m)
        restarted = ok
    return _ok(model=m, restarted=restarted)


@app.route("/api/models/<mid>", methods=["DELETE"])
@require_auth
@require_admin
def remove_model(mid):
    rt = sm.get_runtime(mid)
    if rt.get("running"):
        sm.stop(mid)
    store.delete_model(mid)
    return _ok()


@app.route("/api/models/<mid>/start", methods=["POST"])
@require_auth
def start_model(mid):
    m = store.get_model(mid)
    if not m:
        return _err("模型不存在", 404)
    if sm.get_runtime(mid).get("running"):
        return _ok(message="该模型已在运行", stopped=[])
    # 单模型模式：先停掉其它正在运行的模型，避免显存冲突
    stopped_names = []
    if store.get_general_settings().get("exclusive_mode", True):
        for sid in sm.stop_all_except(keep_id=mid):
            om = store.get_model(sid)
            if om:
                stopped_names.append(om.get("display_name") or om.get("name"))
    ok, msg = sm.start(m)
    if not ok:
        return _err(msg)
    note = ("已停止 " + "、".join(stopped_names) + "；") if stopped_names else ""
    return _ok(message=note + msg, stopped=stopped_names)


@app.route("/api/models/<mid>/connect", methods=["POST"])
@require_auth
def connect_model(mid):
    """连接/刷新外部服务引擎（Ollama / LM Studio / OpenAI 兼容）。"""
    m = store.get_model(mid)
    if not m:
        return _err("模型不存在", 404)
    if sm.get_runtime(mid).get("running"):
        ok, detail = sm.health(m, timeout=3)
        return _ok(message="已连接" if ok else "连接异常", health=detail if ok else None, running=bool(ok))
    if sm.attach(m):
        return _ok(message="连接成功", running=True)
    ok, detail = sm.health(m, timeout=3)
    return _err("无法连接：%s" % (detail if isinstance(detail, str) else "服务未响应"))


@app.route("/api/models/<mid>/stop", methods=["POST"])
@require_auth
def stop_model(mid):
    ok, msg = sm.stop(mid)
    return _ok(message=msg) if ok else _err(msg)


@app.route("/api/models/<mid>/restart", methods=["POST"])
@require_auth
def restart_model(mid):
    m = store.get_model(mid)
    if not m:
        return _err("模型不存在", 404)
    sm.stop(mid)
    time.sleep(1)
    stopped_names = []
    if store.get_general_settings().get("exclusive_mode", True):
        for sid in sm.stop_all_except(keep_id=mid):
            om = store.get_model(sid)
            if om:
                stopped_names.append(om.get("display_name") or om.get("name"))
    ok, msg = sm.start(m)
    if not ok:
        return _err(msg)
    note = ("已停止 " + "、".join(stopped_names) + "；") if stopped_names else ""
    return _ok(message=note + msg, stopped=stopped_names)


@app.route("/api/models/<mid>/log", methods=["GET"])
@require_auth
def model_log(mid):
    lines = int(request.args.get("lines", 120))
    return _ok(lines=sm.tail_log(mid, lines))


@app.route("/api/models/<mid>/preview", methods=["GET"])
@require_auth
def model_preview(mid):
    m = store.get_model(mid)
    if not m:
        return _err("模型不存在", 404)
    try:
        return _ok(cmd=sm.build_command(m))
    except Exception as e:
        return _err(str(e))


# ---------------- 文件浏览 ----------------

@app.route("/api/files", methods=["GET"])
@require_auth
def list_files():
    base = CFG["ai_model_dir"]
    out = []
    if os.path.isdir(base):
        for root, dirs, files in os.walk(base):
            for fn in files:
                if fn.lower().endswith((".gguf", ".safetensors", ".bin")):
                    fp = os.path.join(root, fn)
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        sz = 0
                    out.append({
                        "name": fn,
                        "path": fp,
                        "rel": os.path.relpath(fp, base),
                        "size_gb": round(sz / 1024 ** 3, 2),
                        "dir": os.path.relpath(root, base) if root != base else "",
                    })
    out.sort(key=lambda x: x["rel"])
    return _ok(base=base, files=out)


# ---------------- API Key ----------------

@app.route("/api/keys", methods=["GET"])
@require_auth
def list_keys():
    keys = store.get_keys()
    usage = store.get_usage()
    for k in keys:
        u = usage.get(k["id"]) or {}
        k["usage"] = {
            "requests": u.get("requests", 0),
            "prompt_tokens": u.get("prompt_tokens", 0),
            "completion_tokens": u.get("completion_tokens", 0),
            "total_tokens": u.get("total_tokens", 0),
            "last_used": u.get("last_used"),
        }
    return _ok(keys=keys)


@app.route("/api/keys", methods=["POST"])
@require_auth
@require_admin
def create_key():
    body = request.get_json(silent=True) or {}
    k = store.new_api_key(
        name=body.get("name") or "user",
        models=body.get("models") or ["*"],
        note=body.get("note") or "",
    )
    k["created_by"] = _cur_user().get("id")
    k["created_by_username"] = _cur_user().get("username")
    keys = store.get_keys()
    keys.append(k)
    store.save_keys(keys)
    return _ok(key=k)


@app.route("/api/keys/<kid>", methods=["PUT"])
@require_auth
def update_key(kid):
    body = request.get_json(silent=True) or {}
    me = _cur_user()
    keys = store.get_keys()
    for k in keys:
        if k["id"] == kid:
            if _cur_role() != "admin" and k.get("created_by_username") != me.get("username"):
                return _err("只能编辑自己创建的密钥", 403)
            for f in ("name", "enabled", "models", "note"):
                if f in body:
                    k[f] = body[f]
            if "reasoning_mode" in body:
                v = str(body["reasoning_mode"]).strip().lower()
                if v not in store.REASONING_MODES:
                    return _err("reasoning_mode 取值不合法（可选：%s）" % "/".join(store.REASONING_MODES))
                k["reasoning_mode"] = v
            if "reasoning_effort" in body:
                v = str(body["reasoning_effort"]).strip().lower()
                if v not in store.REASONING_EFFORTS:
                    return _err("reasoning_effort 取值不合法（可选：%s）" % "/".join(store.REASONING_EFFORTS))
                k["reasoning_effort"] = v
            store.save_keys(keys)
            return _ok(key=k)
    return _err("密钥不存在", 404)


@app.route("/api/keys/<kid>", methods=["DELETE"])
@require_auth
@require_admin
def delete_key(kid):
    keys = [k for k in store.get_keys() if k["id"] != kid]
    store.save_keys(keys)
    store.reset_usage(kid)
    return _ok()


@app.route("/api/keys/<kid>/usage", methods=["GET"])
@require_auth
def key_usage(kid):
    """按 API Key 汇总用量。支持时间范围过滤 + 按天 + 按模型聚合。"""
    try:
        days = int(request.args.get("days", 0))
    except Exception:
        days = 0
    from_day = (request.args.get("from") or "").strip()
    to_day = (request.args.get("to") or "").strip()
    try:
        page = int(request.args.get("page", 0))
    except Exception:
        page = 0
    page_size = int(request.args.get("page_size", 50))

    usage_all = store.get_usage()
    u = usage_all.get(kid) or {}
    hist = list(u.get("history", []) or [])
    per_model_total = u.get("per_model", {}) or {}

    # 时间过滤（days=0 代表全部；from/to 为自定义 YYYY-MM-DD）
    now = time.time()
    from_ts = None
    to_ts = None
    if from_day:
        try:
            from_ts = time.mktime(time.strptime(from_day + " 00:00:00", "%Y-%m-%d %H:%M:%S"))
        except Exception:
            from_ts = None
    if to_day:
        try:
            to_ts = time.mktime(time.strptime(to_day + " 23:59:59", "%Y-%m-%d %H:%M:%S"))
        except Exception:
            to_ts = None

    def _h_ts(h):
        try:
            return time.mktime(time.strptime(h.get("time", ""), "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return 0

    if from_ts is not None or to_ts is not None:
        f0 = from_ts if from_ts is not None else 0
        t0 = to_ts if to_ts is not None else float("inf")
        hist_f = [h for h in hist if f0 <= _h_ts(h) <= t0]
    elif days and days > 0:
        cutoff = now - days * 86400
        hist_f = [h for h in hist if _h_ts(h) >= cutoff]
    else:
        hist_f = hist

    # 按天聚合
    daily = {}
    for h in hist_f:
        day = (h.get("time", "") or "")[:10]
        if not day:
            continue
        d = daily.setdefault(day, {"day": day, "requests": 0, "prompt_tokens": 0,
                                   "completion_tokens": 0, "total_tokens": 0,
                                   "models": {}})
        d["requests"] += 1
        pt = int(h.get("prompt_tokens", 0) or 0)
        ct = int(h.get("completion_tokens", 0) or 0)
        tt = int(h.get("total_tokens", pt + ct) or (pt + ct))
        d["prompt_tokens"] += pt
        d["completion_tokens"] += ct
        d["total_tokens"] += tt
        m = h.get("model", "未知")
        mm = d["models"].setdefault(m, {"requests": 0, "prompt_tokens": 0,
                                       "completion_tokens": 0, "total_tokens": 0})
        mm["requests"] += 1
        mm["prompt_tokens"] += pt
        mm["completion_tokens"] += ct
        mm["total_tokens"] += tt

    daily_list = sorted(daily.values(), key=lambda x: x["day"])

    # 范围内总量
    in_total = sum(int(h.get("prompt_tokens", 0) or 0) for h in hist_f)
    out_total = sum(int(h.get("completion_tokens", 0) or 0) for h in hist_f)
    req_total = len(hist_f)

    # 按模型聚合（仅范围内）
    per_model = {}
    for d in daily_list:
        for mname, mv in d["models"].items():
            mm = per_model.setdefault(mname, {"requests": 0, "prompt_tokens": 0,
                                              "completion_tokens": 0, "total_tokens": 0,
                                              "first_used": d["day"], "last_used": d["day"]})
            mm["requests"] += mv["requests"]
            mm["prompt_tokens"] += mv["prompt_tokens"]
            mm["completion_tokens"] += mv["completion_tokens"]
            mm["total_tokens"] += mv["total_tokens"]
            if d["day"] < mm["first_used"]:
                mm["first_used"] = d["day"]
            if d["day"] > mm["last_used"]:
                mm["last_used"] = d["day"]

    # 性能与可靠度分析（仅当前过滤范围）
    lat_list = sorted(int(h.get("latency_ms") or 0) for h in hist_f if h.get("latency_ms"))

    def _pct(arr, p):
        if not arr:
            return 0
        k = int(round((len(arr) - 1) * p))
        k = min(len(arr) - 1, max(0, k))
        return arr[k]

    ok_cnt = sum(1 for h in hist_f
                 if h.get("ok") and 200 <= int(h.get("status_code", 200) or 200) < 300 and not h.get("error"))
    err_dist = {}
    for h in hist_f:
        sc = int(h.get("status_code", 200) or 200)
        if not (200 <= sc < 300):
            err_dist[str(sc)] = err_dist.get(str(sc), 0) + 1
    bucket_dist = {}
    for h in hist_f:
        b = h.get("bucket") or "<1K"
        bucket_dist[b] = bucket_dist.get(b, 0) + 1
    ttft_list = sorted(int(h.get("ttft_ms") or 0) for h in hist_f if h.get("ttft_ms"))
    tps_list = [float(h.get("tps") or 0) for h in hist_f if h.get("tps")]
    cached_total = sum(int(h.get("cached_tokens", 0) or 0) for h in hist_f)
    analytics = {
        "success_rate": round(ok_cnt / len(hist_f) * 100, 1) if hist_f else 0,
        "latency": {
            "avg": int(sum(lat_list) / len(lat_list)) if lat_list else 0,
            "p50": _pct(lat_list, 0.50),
            "p95": _pct(lat_list, 0.95),
            "p99": _pct(lat_list, 0.99),
        },
        "ttft_avg": int(sum(ttft_list) / len(ttft_list)) if ttft_list else 0,
        "tps_avg": round(sum(tps_list) / len(tps_list), 1) if tps_list else 0,
        "cached_tokens": cached_total,
        "cache_hit_rate": round(cached_total / in_total * 100, 1) if in_total else 0,
        "errors": err_dist,
        "buckets": bucket_dist,
    }

    # 历史记录：按任务(task)聚合后分页（每行=一个任务）
    if page < 0: page = 0
    history_page, total_history = store.aggregate_tasks(hist_f, page=page, page_size=page_size)

    # 来源地址：尽力补齐另一地址族（默认显示 IPv4、悬停显示 IPv6）。
    # 兼容历史旧记录：旧记录可能只存了 client（UA 或单一 IP）。
    try:
        from . import netinfo as _net
        for _t in history_page:
            _v4 = str(_t.get("client_ip4") or "").strip()
            _v6 = str(_t.get("client_ip6") or "").strip()
            if not _v4 and not _v6:
                # 历史旧记录：client 可能是 UA/主机名，此时 norm() 为空，原样保留在 v4 位
                _c = str(_t.get("client") or "").strip()
                if _c and not _net.norm(_c):
                    _t["client_ip4"] = _c
                    _t["client"] = _c
                    continue
                _v4, _v6 = _net.peer_pair(_c)
            elif not _v6 and _v4:
                _v4b, _v6 = _net.peer_pair(_v4)
            elif not _v4 and _v6:
                _v4, _v6b = _net.peer_pair(_v6)
            _t["client_ip4"], _t["client_ip6"] = _v4, _v6
            _t["client"] = _v4 or _v6
    except Exception:
        pass

    # 总体口径（不受过滤影响）
    grand_total = {
        "requests": int(u.get("requests", 0) or 0),
        "prompt_tokens": int(u.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(u.get("completion_tokens", 0) or 0),
        "total_tokens": int(u.get("total_tokens", 0) or 0),
        "last_used": u.get("last_used"),
    }

    return _ok(usage={
        "filter": {"days": days, "from": from_day or None, "to": to_day or None,
                   "from_first": hist_f[-1]["time"][:10] if hist_f else None,
                   "to_last": hist_f[0]["time"][:10] if hist_f else None},
        "range_total": {"requests": req_total, "prompt_tokens": in_total,
                        "completion_tokens": out_total,
                        "total_tokens": in_total + out_total},
        "grand_total": grand_total,
        "daily": daily_list,
        "analytics": analytics,
        "per_model": per_model,
        "per_model_total": per_model_total,
        "history_page": history_page,
        "history_total": total_history,
        "page": page, "page_size": page_size,
    }, all=usage_all)


@app.route("/api/usage/reset", methods=["POST"])
@require_auth
@require_admin
def usage_reset():
    body = request.get_json(silent=True) or {}
    kid = (body.get("key_id") or "").strip()
    # 安全保护：必须指定具体 Key，避免前端传空导致全量用量被清空
    if not kid:
        return _err("缺少 key_id，已拒绝执行（防止误清空全部用量）", 400)
    if not any(k.get("id") == kid for k in store.get_keys()):
        return _err("密钥不存在", 404)
    store.reset_usage(kid)
    return _ok()


# ---------------- 对话测试 ----------------

@app.route("/api/chat", methods=["POST"])
@require_auth
def chat():
    """对话测试。支持 stream=true（前端实时显示 token 速度）。
    非流式：返回完整 response + usage + latency_ms + 总速度；
    流式：返回 SSE：event=meta 首包，event=token 中间包，event=done 收尾（含 usage + tokens/s）。"""
    body = request.get_json(silent=True) or {}
    mid = body.get("model_id")
    messages = body.get("messages") or []
    stream = bool(body.get("stream"))
    m = store.get_model(mid)
    if not m:
        return _err("模型不存在", 404)
    rt = sm.get_runtime(mid)
    if not rt.get("running"):
        return _err("模型未运行，请先启动")

    # 说明：对话测试是系统内部自测功能，不计入任何 API Key 的用量统计。
    # 用量统计仅统计“通过某个 API Key 调用网关(/v1)”所产生的 token 消耗。
    host = m.get("host", "127.0.0.1")
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    url = "http://%s:%s/v1/chat/completions" % (host, m.get("internal_port"))
    payload = {
        "model": m.get("alias") or m.get("name"),
        "messages": messages,
        "temperature": body.get("temperature", m.get("temp", 0.7)),
        "top_p": body.get("top_p", m.get("top_p", 0.95)),
        "top_k": body.get("top_k", m.get("top_k", 20)),
        "max_tokens": body.get("max_tokens", 1024),
        "stream": stream,
    }
    # 思考控制（Qwen3 jinja）：默认开，可由前端传 enable_thinking=false 关闭
    enable_thinking = bool(body.get("enable_thinking", True))
    if not enable_thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    if stream:
        payload["stream_options"] = {"include_usage": True}
    if body.get("image"):
        payload["messages"] = messages[:-1] + [{
            "role": "user",
            "content": [
                {"type": "text", "text": messages[-1].get("content", "") if messages else ""},
                {"type": "image_url", "image_url": {"url": body["image"]}},
            ],
        }]

    started = time.time()
    try:
        if not stream:
            r = requests.post(url, json=payload, timeout=(10, 600))
            lat = int((time.time() - started) * 1000)
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
            usage = data.get("usage") or {}
            pt = int(usage.get("prompt_tokens", 0))
            ct = int(usage.get("completion_tokens", 0))
            total_tok = pt + ct
            tps = round(total_tok / (lat / 1000.0), 2) if lat > 0 else 0.0
            return _ok(response=data, latency_ms=lat,
                       usage=usage,
                       prompt_tokens=pt, completion_tokens=ct, total_tokens=total_tok,
                       total_tps=tps)

        # 流式：SSE 直接转发（前端按事件名解析）
        def gen():
            upstream = requests.post(url, json=payload, stream=True, timeout=(10, 600))
            upstream.raise_for_status()
            emitted_text = ""
            t_first = None
            for line in upstream.iter_lines(decode_unicode=True):
                if not line:
                    continue
                if line.startswith("data:"):
                    payload_s = line[5:].strip()
                    if payload_s == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload_s)
                    except Exception:
                        continue
                    # Qwen3 jinja: 可能同时有 reasoning_content（思考）和 content（正文）
                    delta_text = ""
                    delta_reason = ""
                    if obj.get("choices"):
                        d = obj["choices"][0].get("delta", {}) or {}
                        delta_text = d.get("content") or ""
                        delta_reason = d.get("reasoning_content") or ""
                    if delta_text or delta_reason:
                        if t_first is None:
                            t_first = time.time()
                        if delta_reason:
                            emitted_text += delta_reason
                            yield "event: reason\ndata: " + json.dumps({"text": delta_reason}, ensure_ascii=False) + "\n\n"
                        if delta_text:
                            emitted_text += delta_text
                            yield "event: token\ndata: " + json.dumps({"text": delta_text}, ensure_ascii=False) + "\n\n"
                    # usage 通常在最后一帧
                    if obj.get("usage"):
                        lat = int((time.time() - started) * 1000)
                        u = obj["usage"]
                        pt = int(u.get("prompt_tokens", 0))
                        ct = int(u.get("completion_tokens", 0))
                        total_tok = pt + ct
                        tps = round(total_tok / (lat / 1000.0), 2) if lat > 0 else 0.0
                        prefill_ms = int((t_first - started) * 1000) if t_first else lat
                        decode_ms = int((time.time() - (t_first or started)) * 1000) if t_first else lat
                        prefill_tps = round(pt / (prefill_ms / 1000.0), 2) if prefill_ms > 0 and pt > 0 else 0.0
                        decode_tps = round(ct / (decode_ms / 1000.0), 2) if decode_ms > 0 and ct > 0 else 0.0
                        yield "event: done\ndata: " + json.dumps({
                            "usage": u, "latency_ms": lat,
                            "prompt_tokens": pt, "completion_tokens": ct, "total_tokens": total_tok,
                            "total_tps": tps, "prefill_tps": prefill_tps, "decode_tps": decode_tps,
                            "prefill_ms": prefill_ms, "decode_ms": decode_ms,
                        }, ensure_ascii=False) + "\n\n"
            # 兜底：未收到 usage 时补一次
            lat = int((time.time() - started) * 1000)
            yield "event: done\ndata: " + json.dumps({
                "latency_ms": lat, "total_tps": 0, "text_chars": len(emitted_text),
            }, ensure_ascii=False) + "\n\n"
        return Response(gen(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive",
        })
    except Exception as e:
        if stream:
            return Response("event: error\ndata: " + json.dumps({"error": str(e)}, ensure_ascii=False) + "\n\n",
                            mimetype="text/event-stream")
        return _err("调用失败: %s" % e, 502)


# ---------------- 前端页面 ----------------

_STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


# ---------------- 使用登记 ----------------

def _booking_operator():
    try:
        return store.get_settings().get("admin_username") or "admin"
    except Exception:
        return "admin"


@app.route("/api/bookings", methods=["GET"])
@require_auth
def list_bookings():
    return _ok(store.view_bookings())


@app.route("/api/bookings", methods=["POST"])
@require_auth
def create_booking():
    body = request.get_json(silent=True) or {}
    user = (body.get("user") or "").strip()
    if not user:
        return _err("请填写使用人")
    status = body.get("status") or "using"
    if status not in ("using", "reserved", "done"):
        return _err("状态不合法")
    rec = store.add_booking(
        user=user, status=status,
        time_text=body.get("time_text") or "",
        note=body.get("note") or "",
        operator=_booking_operator(),
    )
    return _ok(booking=rec, view=store.view_bookings())


@app.route("/api/bookings/<bid>", methods=["PUT"])
@require_auth
def update_booking(bid):
    body = request.get_json(silent=True) or {}
    fields = {}
    if "user" in body:
        u = (body.get("user") or "").strip()[:40]
        if not u:
            return _err("使用人不能为空")
        fields["user"] = u
    if "time_text" in body:
        fields["time_text"] = (body.get("time_text") or "").strip()[:120]
    if "note" in body:
        fields["note"] = (body.get("note") or "").strip()[:200]
    if "status" in body:
        st = body.get("status")
        if st not in ("using", "reserved", "done"):
            return _err("状态不合法")
        fields["status"] = st
    if not fields:
        return _err("没有可更新的字段")
    rec = store.update_booking(bid, fields)
    if not rec:
        return _err("记录不存在", 404)
    return _ok(booking=rec, view=store.view_bookings())


@app.route("/api/bookings/<bid>/finish", methods=["POST"])
@require_auth
def finish_booking(bid):
    rec = store.update_booking(bid, {"status": "done"})
    if not rec:
        return _err("记录不存在", 404)
    return _ok(booking=rec, view=store.view_bookings())


@app.route("/api/bookings/<bid>", methods=["DELETE"])
@require_auth
@require_admin
def remove_booking(bid):
    if not store.delete_booking(bid):
        return _err("记录不存在", 404)
    return _ok(view=store.view_bookings())


@app.route("/", methods=["GET"])
def index():
    p = os.path.join(_STATIC, "index.html")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")
    return "CSINS-LLM-Manager: 未找到前端页面", 404


@app.route("/user", methods=["GET"])
def user_console():
    """用户自助页：凭自己的 API Key 设置思考开关与强度。"""
    p = os.path.join(_STATIC, "user.html")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html")
    return "CSINS-LLM-Manager: 未找到用户自助页", 404


@app.route("/api/settings/general", methods=["POST"])
@require_auth
@require_admin
def set_general_settings():
    body = request.get_json(silent=True) or {}
    bool_keys = {"exclusive_mode", "ctx_guard", "reasoning_allow_user_override"}
    enum_keys = {
        "reasoning_default_mode": store.REASONING_MODES,
        "reasoning_default_effort": store.REASONING_EFFORTS,
    }
    int_keys = {"thinking_headroom", "switch_grace_sec"}
    upd = {}
    for k, v in body.items():
        if k in bool_keys:
            upd[k] = bool(v)
        elif k in enum_keys:
            sv = str(v).strip().lower()
            if sv not in enum_keys[k]:
                return _err("参数 %s 取值不合法（可选：%s）" % (k, "/".join(enum_keys[k])))
            upd[k] = sv
        elif k in int_keys:
            try:
                iv = int(v)
            except Exception:
                return _err("参数 %s 需为整数" % k)
            if iv < 0 or iv > 32768:
                return _err("参数 %s 需在 0~32768 之间（0 表示关闭该保护）" % k)
            upd[k] = iv
    g = store.set_general_settings(upd)
    return _ok(general=g)


# ---------------- 用户自助：思考设置 ----------------

def _key_from_request():
    """从请求中提取 API Key：Bearer / x-api-key / ?key= / body.key 四种方式。"""
    auth = request.headers.get("Authorization") or ""
    tok = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if not tok:
        tok = (request.headers.get("x-api-key") or "").strip()
    if not tok:
        tok = (request.args.get("key") or "").strip()
    if not tok:
        body = request.get_json(silent=True) or {}
        tok = str(body.get("key") or "").strip()
    return tok


@app.route("/api/user/thinking", methods=["GET", "POST"])
def user_thinking():
    """用户凭自己的 API Key 读取/保存思考设置（无需管理端登录）。"""
    k = store.find_key(_key_from_request())
    if not k:
        return _err("API Key 无效或已停用", 401)
    gen = store.get_general_settings()
    allow = bool(gen.get("reasoning_allow_user_override", True))

    if request.method == "GET":
        return _ok(
            name=k.get("name"),
            mode=k.get("reasoning_mode", "inherit"),
            effort=k.get("reasoning_effort", "inherit"),
            allow_override=allow,
            platform_mode=gen.get("reasoning_default_mode", "inherit"),
            platform_effort=gen.get("reasoning_default_effort", "inherit"),
        )

    if not allow:
        return _err("管理员已关闭「用户自定义思考」，当前为统一策略", 403)
    body = request.get_json(silent=True) or {}
    upd = {}
    if "mode" in body:
        v = str(body["mode"]).strip().lower()
        if v not in store.REASONING_MODES:
            return _err("mode 取值不合法（可选：%s）" % "/".join(store.REASONING_MODES))
        upd["reasoning_mode"] = v
    if "effort" in body:
        v = str(body["effort"]).strip().lower()
        if v not in store.REASONING_EFFORTS:
            return _err("effort 取值不合法（可选：%s）" % "/".join(store.REASONING_EFFORTS))
        upd["reasoning_effort"] = v
    if not upd:
        return _err("没有需要保存的改动")
    keys = store.get_keys()
    for kk in keys:
        if kk.get("id") == k.get("id"):
            kk.update(upd)
            store.save_keys(keys)
            return _ok(mode=kk.get("reasoning_mode", "inherit"),
                       effort=kk.get("reasoning_effort", "inherit"))
    return _err("保存失败：未找到该密钥", 500)


# ---------------- 账户管理 ----------------

@app.route("/api/users", methods=["GET"])
@require_auth
@require_admin
def list_users():
    users = [store.public_user(u) for u in store.get_users().get("users", [])]
    return _ok(users=users)


@app.route("/api/users", methods=["POST"])
@require_auth
@require_admin
def create_user():
    body = request.get_json(silent=True) or {}
    try:
        u = store.create_user(
            username=body.get("username"),
            password=body.get("password") or "123456",
            role=body.get("role") or "user",
            display_name=body.get("display_name") or "",
            note=body.get("note") or "",
        )
    except ValueError as e:
        return _err(str(e))
    return _ok(user=store.public_user(u))


@app.route("/api/users/<uid>", methods=["PUT"])
@require_auth
def update_user(uid):
    body = request.get_json(silent=True) or {}
    me = _cur_user()
    is_self = (me.get("id") == uid)
    if _cur_role() != "admin" and not is_self:
        return _err("无权修改其他账号", 403)
    if is_self and _cur_role() != "admin":
        fields = {k: body[k] for k in ("password", "display_name") if k in body}
        if not fields:
            return _err("无权修改这些字段", 403)
    else:
        fields = {k: body[k] for k in
                  ("username", "display_name", "role", "enabled", "password", "note")
                  if k in body}
    try:
        u = store.update_user(uid, fields)
    except ValueError as e:
        return _err(str(e))
    if not u:
        return _err("账号不存在", 404)
    return _ok(user=store.public_user(u))


@app.route("/api/users/<uid>", methods=["DELETE"])
@require_auth
@require_admin
def delete_user(uid):
    me = _cur_user()
    if me.get("id") == uid:
        return _err("不能删除当前登录的账号")
    try:
        if not store.delete_user(uid):
            return _err("账号不存在", 404)
    except ValueError as e:
        return _err(str(e))
    return _ok()


# ---------------- 使用日志 ----------------

@app.route("/api/logs", methods=["GET"])
@require_auth
@require_admin
def list_logs():
    q = request.args
    return _ok(store.get_logs(
        limit=q.get("limit", 200), offset=q.get("offset", 0),
        user=q.get("user", ""), action=q.get("action", ""),
        result=q.get("result", ""), q=q.get("q", ""),
        role=q.get("role", "")))


@app.route("/api/logs/<lid>", methods=["DELETE"])
@require_auth
@require_admin
def remove_log(lid):
    if not store.delete_log(lid):
        return _err("日志不存在", 404)
    return _ok()


@app.route("/api/logs", methods=["DELETE"])
@require_auth
@require_admin
def clear_logs():
    store.clear_logs()
    return _ok()


# ---------------- 当前登录信息 ----------------

@app.route("/api/me", methods=["GET"])
@require_auth
def api_me():
    u = _cur_user()
    pu = store.public_user(u)
    pu["is_admin"] = (_cur_role() == "admin")
    return _ok(user=pu)


@app.route("/api/settings", methods=["GET"])
@require_auth
def get_settings():
    s = dict(store.get_settings())
    s.pop("admin_password", None)
    s["general"] = store.get_general_settings()
    return _ok(settings=s, paths={
        "llama_cpp_dir": CFG["llama_cpp_dir"],
        "ai_model_dir": CFG["ai_model_dir"],
        "logs_dir": CFG["logs_dir"],
        "gateway_port": CFG["gateway_port"],
        "manager_port": CFG["manager_port"],
    })
