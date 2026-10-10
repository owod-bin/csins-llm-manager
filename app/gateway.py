"""OpenAI 兼容网关：鉴权、路由到内部 llama-server、用量统计。

监听 gateway_port (默认 8000)，对外路径 /v1/*，与 llama.cpp 的 OpenAI 接口一致。
- 客户端使用统一入口 http://<主机IP>:8000/v1，Key 为管理系统中创建的 Key。
- 网关根据请求中的 model 字段路由到对应模型的内部端口。
- 非流式与流式(SSE)均支持，并统计 token 用量。
"""
import json
import threading
import time

import requests
from flask import Flask, Response, jsonify, request

from . import server_manager as sm
from .store import (ENGINE_MAP, engine_of, find_key, get_general_settings, get_model,
                    get_models, is_external_engine, record_usage, resolve_model)

app = Flask(__name__)

# 记录每个模型的“正在推理中”状态，供 /api/overview 用于显示 GPU 状态
from .active_reqs import ACTIVE_REQS

# 按需拉起模型：串行锁 + 失败冷却，避免并发请求重复启动同一个模型
_START_LOCK = threading.Lock()
_START_COOLDOWN = {}          # model_id -> 上次启动失败时间
_START_COOLDOWN_SEC = 30      # 启动失败后多久内不再重试
_START_WAIT_SEC = 180         # 拉起后等待就绪的最长秒数（含加载大模型的时间）

# 切换保护：正在被别人使用的模型不允许被顶掉
_MODEL_LAST_ACTIVE = {}       # model_id -> 最后一次请求结束的时间
_SWITCH_GRACE_SEC = 60        # 距上次请求结束不足该秒数，视为“仍在使用”，不切换
                              # （可在系统设置里用 switch_grace_sec 覆盖，设为 0 即严格按在途请求判断）

# “忙”的第二道保险：租约。
# 光靠 ACTIVE_REQS 不够 —— (1) 流式响应在 Flask 的 after_request 里会提前归零，
# (2) 冷启动期间请求还没被计数。租约在这两个窗口内把模型标记为“使用中”。
_BUSY_LEASE_SEC = 300         # 单次占用租约（秒）；流式响应过程中每 10 秒续租一次
_MODEL_BUSY_UNTIL = {}        # model_id -> 忙到该时间戳


def _busy_begin(mid):
    """开始占用：本次请求从此刻起视为“正在使用该模型”。"""
    try:
        _MODEL_BUSY_UNTIL[mid] = time.time() + _BUSY_LEASE_SEC
    except Exception:
        pass


def _busy_touch(mid):
    """续租（流式响应每收到一个数据块时调用），保证长任务期间不被判为空闲。"""
    _busy_begin(mid)


def _busy_cancel(mid):
    """撤销尚未真正开始的占用（请求被拒绝时调用，不写“最后活跃时间”）。"""
    try:
        _MODEL_BUSY_UNTIL[mid] = 0
    except Exception:
        pass


def _release(mid):
    """结束一次占用：计数 -1；归零时记录最后活跃时间并释放租约（改由 grace 接管）。"""
    try:
        ACTIVE_REQS[mid] = max(0, ACTIVE_REQS.get(mid, 0) - 1)
        if ACTIVE_REQS[mid] == 0:
            _MODEL_LAST_ACTIVE[mid] = time.time()
            _MODEL_BUSY_UNTIL[mid] = 0
    except Exception:
        pass


@app.after_request
def _decrement_active(response):
    """请求结束后自动递减推理计数器，避免计数器需被多路径手动清理。

    流式响应例外：其响应体是生成器，after_request 在数据产生之前就执行了，
    此时递减会让长任务被误判为空闲。故流式请求置 g.stream_handled=True，
    改由生成器的 finally 负责释放。
    """
    try:
        from flask import g
        if not getattr(g, "stream_handled", False):
            mid = getattr(g, "active_mid", None)
            if mid:
                _release(mid)
        info = getattr(g, "ctx_guard_info", None)
        if info:
            response.headers["X-Ctx-Guard"] = info
        th = getattr(g, "think_headroom", None)
        if th:
            response.headers["X-Think-Headroom"] = th
        norm = getattr(g, "reasoning_norm", None)
        if norm:
            response.headers["X-Think-Norm"] = norm
    except Exception:
        pass
    return response


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, x-api-key"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Expose-Headers"] = "X-Ctx-Guard, X-Think-Headroom, X-Think-Norm"
    return resp


def _extract_key():
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def _key_allows(key, model):
    ms = key.get("models") or ["*"]
    if "*" in ms:
        return True
    return any(model.get(f) in ms for f in ("name", "display_name", "alias", "id"))


def _target(model):
    if is_external_engine(model):
        return sm._external_base(model)
    host = model.get("host", "127.0.0.1")
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    return "http://%s:%s" % (host, model.get("internal_port"))


def _get_task_id(headers, key_id):
    """优先使用 X-Task-Id 头，否则用 key+5分钟时间窗口自动归并。"""
    tid = headers.get("X-Task-Id") or headers.get("x-task-id")
    if tid:
        return str(tid).strip()[:64]
    # 无显式任务号时按 key+30 秒窗口自动归并：窗口越小越不会把多次独立调用误并成一条；
    # 30s 足以覆盖单个 agent 任务的多轮往返。
    bucket = int(time.time() // 30)
    return "auto-%s-%d" % (str(key_id)[:12], bucket)


def _prompt_meta(messages):
    """提取请求内容的「元信息」：SHA-256 摘要 + 字符数。

    隐私口径：网关不保存、不落盘请求原文；仅保留不可逆摘要与长度，
    以便在需要时做关联/去重，同时不暴露对话内容。
    """
    import hashlib
    text = ""
    if messages:
        for m in messages:
            if not isinstance(m, dict):
                continue
            if m.get("role") != "user":
                continue
            content = m.get("content", "")
            if isinstance(content, list):
                parts = []
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        parts.append(str(c.get("text", "")))
                text = " ".join(parts).strip()
            else:
                text = str(content).strip()
            break
    if not text:
        return "", 0
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16], len(text)


def _ensure_running(model, wait=0):
    """确保模型运行时记录存在：若未记录但端口健康则接管外部实例。

    wait>0 时会在该秒数内轮询等待（用于模型刚启动、尚未就绪的场景）。
    """
    import time as _t
    mid = model.get("id")
    if sm.get_runtime(mid).get("running"):
        return True
    deadline = _t.time() + max(0, wait)
    while True:
        try:
            if sm.attach(model):
                return True
        except Exception:
            pass
        if _t.time() >= deadline:
            return sm.get_runtime(mid).get("running", False)
        _t.sleep(1.5)


def _model_busy(mid, grace):
    """模型是否正被使用。三重判断，任一成立即为“忙”：

      1. 有在途请求（ACTIVE_REQS）
      2. 仍在本模型的占用租约内 —— 覆盖流式响应、冷启动等 ACTIVE_REQS 失真的窗口
      3. 距上次请求结束不足 grace 秒 —— 覆盖客户端刚断开、上游仍在收尾
    """
    if ACTIVE_REQS.get(mid, 0) > 0:
        return True
    if time.time() < _MODEL_BUSY_UNTIL.get(mid, 0):
        return True
    if grace > 0:
        last = _MODEL_LAST_ACTIVE.get(mid, 0)
        if last and (time.time() - last) < grace:
            return True
    return False


def _ensure_started(model):
    """确保目标模型可用：已运行/可接管则直接返回；否则按需拉起。

    单模型模式（exclusive_mode=true）下切换模型时，**绝不打断正在被使用的模型**：
    只要还有别的模型有在途请求（或刚结束不久、仍在保护期内），本次请求直接失败，
    由调用方返回 503，让调用者稍后重试——避免把别人正在用的模型顶掉。
    返回 (ok, code, message)；ok=False 时 code/message 可直接回给客户端。
    """
    mid = model.get("id")
    if not mid:
        return False, "model_not_running", "模型配置缺少 id"
    if sm.get_runtime(mid).get("running"):
        return True, "", ""
    # 端口已有健康实例（例如服务重启后遗留的进程）→ 接管，不必重新加载
    if _ensure_running(model, wait=0):
        return True, "", ""

    name = model.get("display_name") or model.get("name") or mid
    # 上次启动刚失败过：冷却期内直接失败，避免并发请求把启动流程反复触发
    if time.time() - _START_COOLDOWN.get(mid, 0) < _START_COOLDOWN_SEC:
        return (False, "model_not_running",
                "模型 %s 启动失败，%d 秒内不再重试" % (name, _START_COOLDOWN_SEC))

    with _START_LOCK:
        # 双重检查：等锁期间可能已被其它请求拉起
        if sm.get_runtime(mid).get("running") or _ensure_running(model, wait=0):
            return True, "", ""
        if time.time() - _START_COOLDOWN.get(mid, 0) < _START_COOLDOWN_SEC:
            return (False, "model_not_running",
                    "模型 %s 启动失败，%d 秒内不再重试" % (name, _START_COOLDOWN_SEC))

        # ---- 关键：切换前先确认没有别人正在使用其它模型 ----
        try:
            grace = int(get_general_settings().get("switch_grace_sec", _SWITCH_GRACE_SEC))
        except Exception:
            grace = _SWITCH_GRACE_SEC
        busy = []
        for sid in sm.list_running():
            if sid == mid:
                continue
            if _model_busy(sid, grace):
                om = get_model(sid) or {}
                busy.append(om.get("display_name") or om.get("name") or sid)
        if busy:
            return (False, "model_busy",
                    "模型 %s 正在被使用，为避免中断，暂不能切换到 %s；请等其空闲后重试"
                    % ("、".join(busy), name))

        try:
            if get_general_settings().get("exclusive_mode", True):
                sm.stop_all_except(keep_id=mid)
            ok, _msg = sm.start(model)
            if not ok and not sm.get_runtime(mid).get("running"):
                _START_COOLDOWN[mid] = time.time()
                return False, "model_not_running", "模型 %s 启动失败：%s" % (name, _msg)
        except Exception as e:
            _START_COOLDOWN[mid] = time.time()
            return False, "model_not_running", "模型 %s 启动异常：%s" % (name, e)

    # 等待就绪：llama-server 加载完成后才对外提供服务
    deadline = time.time() + _START_WAIT_SEC
    while time.time() < deadline:
        if sm.get_runtime(mid).get("running"):
            try:
                if sm.health(model, timeout=1.5)[0]:
                    return True, "", ""
            except Exception:
                pass
        time.sleep(1.5)
    if sm.get_runtime(mid).get("running"):
        return True, "", ""
    return (False, "model_not_running",
            "模型 %s 未能在 %d 秒内就绪" % (name, _START_WAIT_SEC))


def _client_of(request):
    ua = request.headers.get("User-Agent", "") or ""
    return ua[:80] or (request.remote_addr or "")


def _client_ip(request):
    """调用方来源 IP（优先取反向代理透传的真实 IP）。"""
    for h in ("X-Forwarded-For", "X-Real-IP"):
        v = (request.headers.get(h) or "").strip()
        if v:
            return v.split(",")[0].strip()[:64]
    return (request.remote_addr or "")[:64]


def _client_pair(request):
    """返回 (ipv4, ipv6)，用于「来源」列默认显示 IPv4、悬停显示 IPv6。

    一次 TCP 连接只携带一个地址族；这里用本机邻居表按 MAC 关联，尽力给出
    同一台主机在同一网段内的另一个地址族（例如客户端用 IPv4 连接时补出它的 IPv6）。
    """
    from . import netinfo as _net
    ip = _client_ip(request)
    n = _net.norm(ip)
    if not n:
        return "", ""
    # 本机回环：同时给出 127.0.0.1 / ::1
    if n in ("127.0.0.1", "::1"):
        return "127.0.0.1", "::1"
    if n.startswith("127."):
        return n, "::1"
    v4, v6 = _net.peer_pair(n)
    # 兜底：自身地址族
    if not v4 and not v6:
        return (n, "") if ":" not in n else ("", n)
    return v4, v6


# ---------------- 上下文保护（防任务中断） ----------------
# 本机模型为混合注意力架构，服务端不支持 KV 平移（--context-shift 会被自动禁用），
# 一旦请求超过上下文窗口，llama-server 直接返回 400/截断，导致长任务被中断。
# 这里在请求进入上游前精确计算输入长度：仅在“否则必然溢出”时才裁剪最早历史，
# 保留系统指令与最近对话，使任务得以继续，使用者无感知。

CTX_GUARD_INFLIGHT = {}   # 预留：记录被裁剪的请求数


def _msg_body_text(m):
    """只取消息的正文文本（不含 tool_calls），可用于安全地回写 content。"""
    if not isinstance(m, dict):
        return "" if m is None else str(m)
    c = m.get("content", "")
    if isinstance(c, list):
        out = []
        for p in c:
            if isinstance(p, dict):
                if p.get("type") == "text":
                    out.append(str(p.get("text", "")))
                elif p.get("type") == "image_url":
                    out.append("[图片]")
        return " ".join(out)
    return "" if c is None else str(c)


def _msg_text(m):
    """把一条消息展平为「全部可计费文本」，用于 token 估算。

    注意：这里必须把 assistant.tool_calls（工具名 + JSON 参数）和 tool 结果一并算上。
    旧实现只算 content，导致 agent 场景下系统性低估输入长度——一次带大参数的工具调用
    可能只被算成几个 token，估算"够用"而实际超窗。
    仅用于估算，切勿把返回值写回 content（会破坏 tool_calls 结构，见 _msg_body_text）。
    """
    if not isinstance(m, dict):
        return "" if m is None else str(m)
    parts = []
    body = _msg_body_text(m)
    if body:
        parts.append(body)
    tcs = m.get("tool_calls")
    if isinstance(tcs, list) and tcs:
        try:
            parts.append(json.dumps(tcs, ensure_ascii=False))
        except Exception:
            parts.append(str(tcs))
    nm = m.get("name")
    if nm:
        parts.append(str(nm))
    return " ".join(parts)


def _est_tokens(text):
    """粗略估算 token 数：CJK≈1/字，其他≈1/4字节。偏大以保证安全。"""
    if not text:
        return 0
    cjk = 0
    other_bytes = 0
    for ch in text:
        o = ord(ch)
        if o >= 0x2E80:          # CJK 及全角
            cjk += 1
        elif o < 128:
            other_bytes += 1
        else:
            other_bytes += 3
    return cjk + (other_bytes + 3) // 4 + 4


def _tools_est_tokens(tools):
    """工具定义的粗略 token 量（用于给输入预算扣减工具占用）。"""
    if not tools:
        return 0
    try:
        return _est_tokens(json.dumps(tools, ensure_ascii=False))
    except Exception:
        return 0


def _tools_rendered(prompt, tools):
    """判断上游渲染结果里是否真的体现了工具定义。

    少数旧版 llama.cpp 会静默忽略 /apply-template 的 tools 字段，此时计数会漏掉
    整段工具定义；用一个"工具名是否出现在渲染结果里"的特征检测兜住。
    """
    try:
        names = []
        for t in tools or []:
            fn = t.get("function") if isinstance(t, dict) else None
            if isinstance(fn, dict) and fn.get("name"):
                names.append(str(fn["name"]))
        if not names:
            return True          # 无法判定，按"已渲染"处理，避免重复叠加
        return any(nm in prompt for nm in names)
    except Exception:
        return True


def _count_prompt_tokens(base, model, messages, tools):
    """向 llama-server 请求精确 token 数（/apply-template + /tokenize）。
    任何失败都返回 None，调用方回退到估算值。外部引擎（OpenAI/Ollama）不适用。

    tools 一并交给 /apply-template：让上游按真实对话模板把工具定义渲染进 prompt 再计数。
    工具定义（JSON Schema）在 agent 场景里占比可观，漏算会让"估算够、实际超窗"。
    """
    try:
        payload = {"messages": messages}
        if tools:
            payload["tools"] = tools
        tmpl = requests.post(base + "/apply-template", json=payload,
                             timeout=(3, 20)).json()
        prompt = tmpl.get("prompt") if isinstance(tmpl, dict) else None
        if not prompt:
            return None
        tk = requests.post(base + "/tokenize", json={"content": prompt},
                           timeout=(3, 20)).json()
        toks = tk.get("tokens")
        if isinstance(toks, list):
            n = len(toks)
            # 兜底：上游若忽略了 tools（渲染结果里找不到任何工具名），按粗估补回
            if tools and not _tools_rendered(prompt, tools):
                n += _tools_est_tokens(tools)
            return n
    except Exception:
        return None
    return None


def _blocks(msgs):
    """把消息序列切成「不可拆分」的原子块，返回 [(start, end), ...]（半开区间）。

    agent 场景下一次工具调用由「assistant(tool_calls) + 紧随其后的若干 role=tool 结果」
    共同构成；qwen 系模板不使用 tool_call_id，完全依赖消息顺序配对。裁剪时若只保留
    其中一半，上游轻则语义错乱，重则直接 500。故绑定成块，整体保留或整体丢弃。
    """
    out = []
    i, n = 0, len(msgs or [])
    while i < n:
        m = msgs[i] if isinstance(msgs[i], dict) else {}
        if m.get("role") == "assistant" and m.get("tool_calls"):
            j = i + 1
            while j < n and isinstance(msgs[j], dict) and msgs[j].get("role") == "tool":
                j += 1
            out.append((i, j))
            i = j
        else:
            out.append((i, i + 1))
            i += 1
    return out


def _drop_orphan_tools(msgs):
    """丢弃「无主」的 tool 消息：其前面没有声明 tool_calls 的 assistant。

    客户端自身的历史裁剪、或裁剪块边界判断失误，都可能留下孤儿 tool 消息；
    上游模板对它们没有兜底，会直接报错。
    """
    out = []
    armed = False          # 上一条已保留的 assistant 是否声明了 tool_calls
    for m in msgs or []:
        if not isinstance(m, dict):
            out.append(m)
            continue
        r = m.get("role")
        if r == "assistant":
            armed = bool(m.get("tool_calls"))
            out.append(m)
        elif r == "tool":
            if armed:
                out.append(m)
            # 否则丢弃（孤儿 tool 结果）
        else:
            armed = False
            out.append(m)
    return out


def _truncate_msg(text, keep_tokens):
    """单条消息过长时，保留首尾、省略中段，并插入省略标记。"""
    keep_chars = max(200, int(keep_tokens * 0.8))   # 预留 20% 安全余量
    head = keep_chars * 2 // 3
    tail = keep_chars - head
    if len(text) <= keep_chars:
        return text
    return text[:head] + "\n\n…（为继续任务，较早内容已自动省略）…\n\n" + text[-tail:]


# 上游（llama.cpp）报“上下文超限”时的经典错误文案片段，用于判定是否需要强制裁剪后重试。
_OVERFLOW_HINTS = (
    "exceeds the available context size",
    "exceeds context",
    "exceed_context_size",
    "context size",
    "out of context",
    "too large for the context",
    "maximum context length",
)


def _looks_overflow(status, text):
    """判断上游响应是否为“输入超过上下文窗口”。"""
    try:
        if int(status or 0) not in (400, 413, 500):
            return False
    except Exception:
        return False
    t = (text or "").lower()
    return any(h in t for h in _OVERFLOW_HINTS)


_CTX_CACHE = {}

# ---- 输出额度保底 ----------------------------------------------------------
# 上下文保护在裁剪输入时，必须为「模型输出」预留足够空间。
# 若把输出额度压到 200~500 token，开启思考的模型光思维链就会占满额度，
# 返回 finish_reason=length（前端表现为“响应因达到最大 token 限制而被截断”并中断任务）。
_MIN_OUT = 4096      # 输出额度保底（client 要得再少也至少留这么多）
_DEF_OUT = 8192      # client 未指定 max_tokens 时的默认预留
_MAX_OUT = 16384     # 单次输出预留上限
_THINK_HEADROOM = 4096   # 开启思考时的额外输出预留（可在系统设置 thinking_headroom 调整）

# ---- 思考强度归一化 ----
# 背景：llama.cpp 会把请求里的 reasoning_effort 原样交给模型 chat template，而各模型模板
# 接受的取值集合不同。例如 Bonsai2 系模板只认 xhigh/medium/low，遇到 high/minimal/max
# 会直接 raise_exception → HTTP 500。而 WorkBuddy 等 agent 客户端在"自定义模型"（无
# thinkingLevelMap）上会把 xhigh/max 折叠成 high 下发，因此一旦开启思考就必然 500、任务中断。
# 这里在网关统一收敛到主流模板都安全接受的集合，客户端无需改动。
_RE_EFFORT_MAP = {
    "xhigh": "xhigh", "max": "xhigh", "high": "xhigh", "ultra": "xhigh",
    "medium": "medium", "mid": "medium", "default": "medium",
    "low": "low", "minimal": "low",
}
_RE_OFF = {"off", "none", "disabled", "disable", "false", "0", "no"}


def _normalize_reasoning(body):
    """把客户端下发的思考强度收敛为模板安全值，避免"开思考即 500"。
    兼容顶层 reasoning_effort 与 reasoning.effort 两种写法。
    返回 (是否改动, 说明)。
    """
    val = body.get("reasoning_effort")
    if not isinstance(val, str) or not val.strip():
        r = body.get("reasoning")
        if isinstance(r, dict):
            val = r.get("effort") or r.get("level")
    if not isinstance(val, str) or not val.strip():
        return False, None
    key = val.strip().lower()
    robj = body.get("reasoning")

    if key in _RE_OFF:
        # 显式关闭思考：删掉强度字段并关闭思考开关，模板就不会做取值校验
        body.pop("reasoning_effort", None)
        ctk = body.setdefault("chat_template_kwargs", {})
        if isinstance(ctk, dict):
            ctk["enable_thinking"] = False
        if isinstance(robj, dict):
            robj.pop("effort", None)
            robj.pop("level", None)
            if not robj:
                body.pop("reasoning", None)
        return True, "off->enable_thinking=false"

    target = _RE_EFFORT_MAP.get(key, "medium")
    changed = False
    if body.get("reasoning_effort") != target:
        body["reasoning_effort"] = target
        changed = True
    # 对象写法（reasoning.effort）也同步，避免个别后端读到旧值
    if isinstance(robj, dict):
        for _f in ("effort", "level"):
            if _f in robj and robj[_f] != target:
                robj[_f] = target
                changed = True
    if changed:
        return True, "%s->%s" % (key, target)
    return False, None


# ---- 思考策略分层解析 ----
# 优先级：请求级 > API Key 级 > 模型级 > 全局默认。
# 「允许用户自定义」关闭时忽略前两层，一律使用平台策略（便于统一管控、节省算力）。
def _req_reasoning(body):
    """解析客户端请求里显式指定的思考设置，返回 (mode, effort)。"""
    mode = effort = None
    ctk = body.get("chat_template_kwargs")
    if isinstance(ctk, dict) and "enable_thinking" in ctk:
        mode = "on" if ctk.get("enable_thinking") else "off"
    val = body.get("reasoning_effort")
    if not isinstance(val, str) or not val.strip():
        r = body.get("reasoning")
        if isinstance(r, dict):
            val = r.get("effort") or r.get("level")
    if isinstance(val, str) and val.strip():
        k = val.strip().lower()
        if k in _RE_OFF:
            mode = "off"
        else:
            mode = mode or "on"
            effort = _RE_EFFORT_MAP.get(k, "medium")
    return mode, effort


def _pick_reasoning(model, key, body, gen):
    """按优先级选出有效思考策略，返回 (mode, effort, 来源)；均为 None 表示不干预。"""
    allow = bool(gen.get("reasoning_allow_user_override", True))

    # 1) 请求级（客户端 /effort、enable_thinking）
    if allow:
        rm, rf = _req_reasoning(body)
        if rm or rf:
            return rm, rf, "request"

    # 2) API Key 级（用户自助设置）
    if allow and isinstance(key, dict):
        km = str(key.get("reasoning_mode") or "inherit").lower()
        if km in ("on", "off"):
            ke = str(key.get("reasoning_effort") or "inherit").lower()
            return km, (ke if ke in ("low", "medium", "xhigh") else None), "key"

    # 3) 模型级（管理平台按模型设置）
    mm = str((model or {}).get("reasoning_mode") or "auto").lower()
    if mm in ("on", "off"):
        return mm, None, "model"

    # 4) 全局默认（系统设置）
    gm = str(gen.get("reasoning_default_mode") or "inherit").lower()
    if gm in ("on", "off"):
        ge = str(gen.get("reasoning_default_effort") or "inherit").lower()
        return gm, (ge if ge in ("low", "medium", "xhigh") else None), "global"

    return None, None, "none"


def _apply_reasoning(body, mode, effort):
    """把选定的思考策略写入请求体，返回是否发生改动。"""
    if mode == "off":
        body.pop("reasoning_effort", None)
        ctk = body.setdefault("chat_template_kwargs", {})
        if isinstance(ctk, dict):
            ctk["enable_thinking"] = False
        r = body.get("reasoning")
        if isinstance(r, dict):
            r.pop("effort", None)
            r.pop("level", None)
            if not r:
                body.pop("reasoning", None)
        return True
    if mode == "on":
        ctk = body.setdefault("chat_template_kwargs", {})
        if isinstance(ctk, dict):
            ctk["enable_thinking"] = True
        if effort:
            body["reasoning_effort"] = effort
        return True
    return False


# ---- 思考预留：防"思维链吃光输出额度"导致前端 MAX_TOKENS 截断 ----------------
# 背景（2026-09-30 实测）：
#   Bonsai2 系模板在「未传 enable_thinking」时默认开启思考，且 reasoning_effort 默认
#   xhigh（最啰嗦档）。此时若客户端 max_tokens 只有 8192，思维链会把整个输出额度吃掉，
#   模型还没写出正式回答就 finish_reason=length，前端表现为
#   “响应因达到最大 token 限制而被截断”（Error Code: MAX_TOKENS）并中断任务。
# 对策：判定本次请求上游会启用思考时，把上游 max_tokens 抬高一个「思考预留」，
#   保证正式回答始终有空间；同时受「窗口剩余」与 _MAX_OUT 约束，不会撑破上下文。
def _thinking_on(base, model, body):
    """判断本次请求上游是否会启用思考（不看模板细节，只看最终生效意图）。"""
    ctk = body.get("chat_template_kwargs")
    if isinstance(ctk, dict):
        et = ctk.get("enable_thinking")
        if et is False:
            return False
        if et is True:
            return True
    v = body.get("reasoning_effort")
    if not (isinstance(v, str) and v.strip()):
        r = body.get("reasoning")
        if isinstance(r, dict):
            v = r.get("effort") or r.get("level")
    if isinstance(v, str) and v.strip():
        return v.strip().lower() not in _RE_OFF
    mode = str((model or {}).get("reasoning_mode") or "auto").lower()
    if mode == "off":
        return False
    if mode == "on":
        return True
    # auto：由模型模板决定。模板里出现 enable_thinking / reasoning_effort 即视为默认会思考。
    tpl = _server_props(base).get("chat_template") or ""
    return ("enable_thinking" in tpl) or ("reasoning_effort" in tpl)


def _apply_think_headroom(base, model, body, gen, inp_tokens=None):
    """思考开启时给输出额度加预留。返回说明字符串；未改动返回 None。"""
    try:
        if not _thinking_on(base, model, body):
            return None
        mt = int(body.get("max_tokens") or 0)
        if mt <= 0:
            return None      # 客户端未限输出上限，交给上游按窗口自然收敛
        head = int(gen.get("thinking_headroom", _THINK_HEADROOM))
        if head <= 0:
            return None
        cap = _server_ctx(base, model)
        if inp_tokens is None:
            inp_tokens = sum(_est_tokens(_msg_text(m))
                             for m in (body.get("messages") or []) if isinstance(m, dict))
        room = max(_MIN_OUT, int(cap * 0.94) - int(inp_tokens))
        want = min(mt + head, _MAX_OUT, room)
        if want <= mt:
            return None
        body["max_tokens"] = int(want)
        return "%d->%d" % (mt, want)
    except Exception:
        return None


def _server_props(base):
    """取上游 /props（30s 缓存，失败缓存空字典）。上下文窗口与模板能力都从这里读。"""
    now = time.time()
    hit = _CTX_CACHE.get(base)
    if hit and now - hit[0] < 30:
        return hit[1]
    try:
        pr = requests.get(base + "/props", timeout=(2, 5)).json()
        if not isinstance(pr, dict):
            pr = {}
    except Exception:
        pr = {}
    _CTX_CACHE[base] = (now, pr)
    return pr


def _server_ctx(base, model):
    """上游真实可用上下文窗口；失败回退到配置值。"""
    pr = _server_props(base)
    try:
        dgs = pr.get("default_generation_settings") or {}
        cap = int(dgs.get("n_ctx") or pr.get("n_ctx") or 0) or None
    except Exception:
        cap = None
    if not cap:
        cap = int(model.get("ctx") or 131072)
    return cap


def _guard_context(base, model, body, inp_tokens, force=False, frac=None, cap=None):
    """按需裁剪消息与输出额度。返回 (messages, max_tokens, dropped_count, trimmed)。

    force=True 时无条件把输入压到窗口 frac 比例以内（用于上游仍报超限后的兜底重试）。
    frac 缺省：常规 0.94、强制 0.72。
    """
    msgs = body.get("messages") or []
    if not isinstance(msgs, list) or not msgs:
        return msgs, body.get("max_tokens"), 0, False
    cap = int(cap or model.get("ctx") or 131072)
    if frac is None:
        frac = 0.72 if force else 0.94
    total = max(1024, int(cap * frac))
    # 先为「输出」预留位置，剩下的才作为输入预算；这样长上下文时是「多裁输入」，
    # 而不是把输出额度榨到几百 token 导致必然截断（MAX_TOKENS）。
    try:
        _want = int(body.get("max_tokens") or 0)
    except Exception:
        _want = 0
    if _want <= 0:
        _want = _DEF_OUT
    _want = max(_MIN_OUT, min(_want, _MAX_OUT, max(512, total // 3)))
    budget = max(512, total - _want)
    # _est_tokens 为粗估，与真实分词器存在偏差。用「精确总数 / 粗估总数」校准，
    # 使裁剪结果对真实 tokenizer 也一定落在预算内，避免“裁了却仍超窗”。
    # 工具定义算作输入的一部分：inp_tokens 含它，粗估基线也要含它，否则 scale 会被放大而过度裁剪。
    _tools_tok = _tools_est_tokens(body.get("tools"))
    _raw_est = sum(_est_tokens(_msg_text(m)) for m in msgs if isinstance(m, dict)) + _tools_tok
    scale = 1.0
    if inp_tokens and _raw_est > 0:
        scale = min(4.0, max(1.0, float(inp_tokens) / float(_raw_est)))

    def _et(m, _s=scale):
        return int(_est_tokens(_msg_text(m)) * _s) + 2

    mt = body.get("max_tokens")
    if not force and (inp_tokens is None or inp_tokens <= budget):
        if inp_tokens is None:
            return msgs, mt, 0, False
        room = total - inp_tokens          # 留给输出的真实剩余窗口
        if mt and int(mt) > room:
            # 要得比剩余窗口还多：压到剩余窗口，但守住输出保底
            return msgs, max(_MIN_OUT, min(int(mt), room)), 0, True
        if not mt and room < _DEF_OUT:
            # 未指定输出上限且剩余窗口偏小：主动封顶，避免输出撞满窗口后被截断
            return msgs, max(_MIN_OUT, min(room, _MAX_OUT)), 0, True
        return msgs, mt, 0, False
    # 输入超预算：保留 system + 尽量多的最新消息
    sys_msgs = [m for m in msgs if isinstance(m, dict) and m.get("role") == "system"]
    _sys_ids = set(id(m) for m in sys_msgs)
    rest = [m for m in msgs
            if not (isinstance(m, dict) and id(m) in _sys_ids)]
    sys_tokens = sum(_et(m) for m in sys_msgs)
    # 工具定义先占位：留给对话消息的空间要把工具开销扣掉，否则"裁完仍超窗"。
    room = budget - sys_tokens - _tools_tok
    if room <= 256:
        sys_msgs = sys_msgs[-1:]
        sys_tokens = _et(sys_msgs[0]) if sys_msgs else 0
        room = max(256, budget - sys_tokens - _tools_tok)
    # ---- 块化挑选：整块保留/丢弃，避免把工具调用与其结果拆散 ----
    blks = _blocks(rest)
    _kept_idx = set()
    _trunc = {}
    used = 0
    for (s, e) in reversed(blks):
        bt = sum(_et(rest[k]) for k in range(s, e))
        if used + bt <= room:
            _kept_idx.update(range(s, e))
            used += bt
        else:
            if not _kept_idx:
                # 连最近的一块都放不下：逐条压缩正文（保留 tool_calls 结构本身）
                per = max(200, int(room / max(1, e - s)) - 8)
                for k in range(s, e):
                    mk = rest[k]
                    if isinstance(mk, dict) and not (mk.get("role") == "assistant"
                                                     and mk.get("tool_calls")):
                        mm = dict(mk)
                        mm["content"] = _truncate_msg(_msg_body_text(mm), per)
                        _trunc[k] = mm
                    _kept_idx.add(k)
                used = room
            break

    def _at(k):
        return _trunc.get(k, rest[k])

    picked = [_at(k) for k in range(len(rest)) if k in _kept_idx]

    # 修正 1：裁剪结果必须至少保留一条 user。
    # 长任务的典型形态是「user 在前、其后一长串 assistant」；只从后往前挑会把 user 全部丢弃，
    # 上游模板（Bonsai2）随即抛 "No user query found in messages" 直接 500。
    if picked and not any(isinstance(m, dict) and m.get("role") == "user" for m in picked):
        _kept = set(id(x) for x in picked)
        _cand = [m for m in rest
                 if isinstance(m, dict) and id(m) not in _kept and m.get("role") == "user"]
        if _cand:
            _u = _cand[-1]                          # 最近的一条 user
            _tu = _et(_u)
            while picked and used + _tu > room:     # 从最老的一端腾出空间
                used -= _et(picked.pop(0))
            picked.insert(0, _u)
            used += _tu

    # 修正 2：避免以 assistant 开头，但绝不能删到没有 user。
    # 旧实现「只要是 assistant 就删」在 picked 恰好全是 assistant 时，会把它一路删到只剩 1 条，
    # 白白浪费 80% 以上的可用窗口（模型看到的历史过少，任务容易跑偏）。
    # 注意：带 tool_calls 的 assistant 不在此列——删掉它会让紧随的 tool 结果变成孤儿消息。
    while (len(picked) > 1 and isinstance(picked[0], dict)
           and picked[0].get("role") == "assistant" and not picked[0].get("tool_calls")
           and any(isinstance(m, dict) and m.get("role") == "user" for m in picked[1:])):
        used -= _et(picked.pop(0))

    # 收尾：丢弃孤儿 tool 消息（块化挑选已基本杜绝，这里作为最后一道保险）
    picked = _drop_orphan_tools(picked)
    _kept = set(id(x) for x in picked)
    dropped = sum(1 for m in rest if isinstance(m, dict) and id(m) not in _kept)
    new_msgs = sys_msgs + picked
    # 裁剪后剩余窗口全部给输出，但不少于保底、不多于 client 期望；工具定义占用也算进输入
    room = max(_MIN_OUT, total - (sys_tokens + used + _tools_tok))
    if mt:
        new_mt = max(_MIN_OUT, min(int(mt), room))
    else:
        new_mt = max(_MIN_OUT, min(_DEF_OUT, room))
    return new_msgs, new_mt, dropped, True


def _hard_fit(base, model, body, cap):
    """对上次上游返回“仍超窗”的请求做最终兜底：把总输入压到窗口 72% 以内。

    用上游 /tokenize 精确校验；若仍超预算则反复减半截断正文，保证一定放得下。
    返回是否发生了有效裁剪。
    """
    msgs = body.get("messages") or []
    if not isinstance(msgs, list) or not msgs:
        return False
    budget = max(512, int(cap * 0.72) - _MIN_OUT)
    mt = body.get("max_tokens")

    def _total():
        n = _count_prompt_tokens(base, model, msgs, body.get("tools"))
        return n

    n = _total()
    if n is not None and n <= budget:
        return False
    # 精确不可得时用校准估算。工具定义同样计入基线（n 已含它），并先占掉一部分预算，
    # 否则"把消息压进预算"后加上工具定义仍是超窗。
    _tools_tok = _tools_est_tokens(body.get("tools"))
    raw = sum(_est_tokens(_msg_text(m)) for m in msgs if isinstance(m, dict)) + _tools_tok
    scale = min(4.0, max(1.0, (float(n) / raw) if (n and raw) else 1.0))
    room_chars = int(max(256, budget - _tools_tok) / max(scale, 1e-6)) * 0.9
    blks = _blocks(msgs)
    _kept_idx = set()
    _trunc = {}
    used = 0
    for (s, e) in reversed(blks):
        bt = sum(_est_tokens(_msg_text(msgs[k])) * scale for k in range(s, e))
        if used + bt <= room_chars:
            _kept_idx.update(range(s, e))
            used += bt
        else:
            if not _kept_idx:
                # 连最近一块都放不下：逐条压缩正文（保留 tool_calls 结构本身）
                for k in range(s, e):
                    mk = msgs[k]
                    if isinstance(mk, dict) and not (mk.get("role") == "assistant"
                                                     and mk.get("tool_calls")):
                        mm = dict(mk)
                        mm["content"] = _truncate_msg(
                            _msg_body_text(mm), max(200, int(room_chars)))
                        _trunc[k] = mm
                    _kept_idx.add(k)
                used = room_chars
            break
    picked = [_trunc.get(k, msgs[k]) for k in range(len(msgs)) if k in _kept_idx]
    # 同 _guard_context：必须至少保留一条 user，否则上游模板直接 500
    if picked and not any(isinstance(m, dict) and m.get("role") == "user" for m in picked):
        _kept = set(id(x) for x in picked)
        _cand = [m for m in msgs if isinstance(m, dict)
                 and id(m) not in _kept and m.get("role") == "user"]
        if _cand:
            _u = _cand[-1]                          # 最近的一条 user
            _tu = _est_tokens(_msg_text(_u)) * scale
            while picked and used + _tu > room_chars:
                used -= _est_tokens(_msg_text(picked.pop(0))) * scale
            picked.insert(0, _u)
    picked = _drop_orphan_tools(picked)
    if not picked and msgs:
        picked = [msgs[-1]]
    body["messages"] = picked
    if mt:
        body["max_tokens"] = int(min(int(mt), max(_MIN_OUT, budget // 4)))
    return True



@app.route("/v1/<path:sub>", methods=["POST", "GET", "OPTIONS"])
@app.route("/v1", methods=["POST", "GET", "OPTIONS"])
def proxy(sub=None):
    if request.method == "OPTIONS":
        return _cors(Response("", 204))

    token = _extract_key()
    key = find_key(token)
    if not key:
        return _cors(jsonify({"error": {"message": "无效或已禁用的 API Key", "type": "invalid_request_error", "code": "invalid_api_key"}})), 401

    # 不走模型路由的端点直接在网关上处理
    path = "/v1" + (("/" + sub) if sub else "")

    if path == "/v1/models" and request.method == "GET":
        # 列出全部已配置模型：配合按需拉起，请求任一 alias 都会被自动加载，
        # 因此对客户端而言它们都是可用的（agent 侧做模型发现时才看得到目标模型）。
        data = []
        for m in get_models():
            if not sm.get_runtime(m["id"]).get("running"):
                _ensure_running(m, wait=0)      # 仅接管已在跑的实例，不主动加载
            data.append({
                "id": m.get("alias") or m.get("name"),
                "object": "model",
                "created": 0,
                "owned_by": engine_of(m),
                "permission": [],
            })
        return _cors(jsonify({"object": "list", "data": data}))

    if not path.startswith("/v1/chat") and not path.startswith("/v1/completions") and not path.startswith("/v1/embeddings"):
        # 其它端点（如 /v1/responses）也一并转发
        pass

    body = request.get_json(silent=True) or {}
    model_name = body.get("model") or request.args.get("model")
    model = resolve_model(model_name) if model_name else None

    if model is None:
        # 若只有一个模型在运行，则回退到该模型
        running = []
        for m in get_models():
            if sm.get_runtime(m["id"]).get("running") or _ensure_running(m, wait=0):
                running.append(m)
        if len(running) == 1:
            model = running[0]
        elif not model_name:
            return _cors(jsonify({"error": {"message": "缺少 model 参数", "type": "invalid_request_error"}})), 400
        else:
            return _cors(jsonify({"error": {"message": "未知模型: %s" % model_name, "type": "invalid_request_error", "code": "model_not_found"}})), 404

    if not _key_allows(key, model):
        return _cors(jsonify({"error": {"message": "该 API Key 无权访问模型 %s" % model_name, "type": "invalid_request_error", "code": "permission_denied"}})), 403

    # 先占位、再确保就绪：冷启动可能耗时 10~180 秒，这段时间也必须让别的请求
    # 看到“本模型正在被使用”，否则漫长的启动过程会被另一个客户端的请求顶掉。
    _busy_begin(model["id"])
    _ok_start, _start_code, _start_msg = _ensure_started(model)
    if not _ok_start:
        _busy_cancel(model["id"])
        return _cors(jsonify({"error": {"message": _start_msg or "模型暂时不可用",
                                        "type": "service_unavailable",
                                        "code": _start_code or "model_not_running"}})), 503

    from flask import g
    g.active_mid = model["id"]
    ACTIVE_REQS[model["id"]] = ACTIVE_REQS.get(model["id"], 0) + 1

    task_id = _get_task_id(request.headers, key["id"])
    prompt_hash, prompt_chars = _prompt_meta(body.get("messages"))
    stream = bool(body.get("stream"))

    # ---- 上下文保护：在转发前预估/精算输入长度，必要时裁剪最早历史，防任务中途报错 ----
    _guard_on = get_general_settings().get("ctx_guard", True) and not is_external_engine(model)
    _inp_used = None      # 精算到的输入 token 数（供"思考预留"复用，避免重复请求上游）
    if _guard_on:
        try:
            _base = _target(model)
            _cap = _server_ctx(_base, model)
            _msgs = body.get("messages") or []
            _used_p = sum(_est_tokens(_msg_text(m)) for m in _msgs if isinstance(m, dict))
            _mt0 = body.get("max_tokens")
            # 仅在“粗略估算”接近/超过窗口时才做精确计数（避免给小请求增加开销）
            if _used_p > _cap * 0.6 or (_mt0 and _used_p + int(_mt0) > _cap * 0.9):
                _inp = _count_prompt_tokens(_base, model, _msgs, body.get("tools")) or _used_p
                _inp_used = _inp
                _nm, _nmt, _drop, _trim = _guard_context(_base, model, body, _inp, cap=_cap)
                if _trim:
                    body["messages"] = _nm
                    if _nmt:
                        body["max_tokens"] = int(_nmt)
                    g.ctx_guard_info = "trimmed; dropped=%d; in=%s" % (_drop, _inp)
        except Exception:
            pass

    def _hard_trim():
        """上游仍报“上下文超限”时的兜底：强制把输入压到窗口 72% 以内，供重试一次。"""
        if not _guard_on:
            return False
        try:
            _b = _target(model)
            _c = _server_ctx(_b, model)
            if _hard_fit(_b, model, body, _c):
                g.ctx_guard_info = "hard-trim; cap=%s" % (_c,)
                return True
        except Exception:
            pass
        return False


    if stream:
        body.setdefault("stream_options", {})["include_usage"] = True
    # 用真实模型名发送给上游
    if model_name:
        body["model"] = model.get("alias") or model.get("name")

    # ---- 思考策略：请求级 > API Key 级 > 模型级 > 全局默认 ----
    _gen_r = get_general_settings()
    try:
        _rm, _re, _rsrc = _pick_reasoning(model, key, body, _gen_r)
        if _rm or _re:
            _apply_reasoning(body, _rm, _re)
            g.reasoning_src = _rsrc
            g.reasoning_mode = _rm
    except Exception:
        pass

    # ---- 思考强度归一化：防"开启思考后模板取值校验报 500" ----
    if _gen_r.get("reasoning_normalize", True):
        try:
            _chg, _info = _normalize_reasoning(body)
            if _chg:
                g.reasoning_norm = _info
        except Exception:
            pass

    # ---- 思考预留：思考开启时为输出额度留出思维链空间，防前端 MAX_TOKENS 截断 ----
    try:
        _th = _apply_think_headroom(_target(model), model, body, _gen_r, _inp_used)
        if _th:
            g.think_headroom = _th
    except Exception:
        pass

    url = _target(model) + path
    headers = {"Content-Type": "application/json"}
    if is_external_engine(model):
        _k = (model.get("api_key") or "").strip()
        if _k:
            headers["Authorization"] = "Bearer " + _k
    started = time.time()
    # 必须在请求上下文内取出 client：流式生成器在请求上下文销毁后才被消费，
    # 生成器内再访问 request 会抛 RuntimeError，导致 SSE 中断 + 用量不记录。
    client_str = _client_of(request)
    client_ip = _client_ip(request)
    cip4, cip6 = _client_pair(request)

    try:
        if stream:
            upstream = requests.post(url, json=body, headers=headers, stream=True, timeout=(30, 1800))
            if upstream.status_code != 200:
                txt = upstream.text
                # 上游若仍报上下文超限，强制裁剪后重试一次，避免 agent 任务被硬中断
                if _looks_overflow(upstream.status_code, txt) and _hard_trim():
                    upstream = requests.post(url, json=body, headers=headers, stream=True, timeout=(30, 1800))
                    if upstream.status_code != 200:
                        txt = upstream.text
                if upstream.status_code != 200:
                    return _cors(Response(txt, status=upstream.status_code, mimetype="application/json"))

            _stream_mid = model["id"]
            g.stream_handled = True   # 流式响应：计数与租约改由下面的 gen() 释放

            def _stream_body():
                usage = None
                first_tok_at = None
                _last_touch = 0.0
                for raw in upstream.iter_lines(chunk_size=None):
                    _now = time.time()
                    if _now - _last_touch > 10:
                        _busy_touch(_stream_mid)   # 续租：长任务期间保持“忙”
                        _last_touch = _now
                    if raw is None:
                        continue
                    line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                    if not line:
                        continue
                    if line.startswith("data: "):
                        payload = line[6:]
                        if payload.strip() == "[DONE]":
                            yield "data: [DONE]\n\n"
                            continue
                        try:
                            obj = json.loads(payload)
                            if obj.get("usage"):
                                usage = obj["usage"]
                            if first_tok_at is None and obj.get("choices"):
                                _d = (obj["choices"][0] or {}).get("delta") or {}
                                # 兼容 content / reasoning_content(思维链) / 纯 text，任一增量即视为首个输出 token
                                if (_d.get("content") or _d.get("reasoning_content")
                                        or obj["choices"][0].get("text")):
                                    first_tok_at = time.time()
                        except Exception:
                            pass
                    yield (line + "\n\n").encode("utf-8")
                lat = int((time.time() - started) * 1000)
                ct = int((usage or {}).get("completion_tokens", 0) or 0)
                cached = int(((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
                ttft = int((first_tok_at - started) * 1000) if first_tok_at else None
                _dt = (time.time() - first_tok_at) if first_tok_at else None
                tps = round(ct / _dt, 1) if (_dt and _dt > 0.05 and ct) else None
                try:
                    record_usage(key["id"], model.get("display_name") or model.get("name"), usage,
                                 latency_ms=lat, ok=True, client=client_ip,
                                 task_id=task_id, prompt_hash=prompt_hash, prompt_chars=prompt_chars,
                                 status_code=200, error="", ttft_ms=ttft, tps=tps, cached_tokens=cached,
                                 client_ip4=cip4, client_ip6=cip6)
                except Exception:
                    pass  # 统计失败不得影响响应

            def gen():
                """包一层：无论正常结束、客户端断开还是上游异常，都释放占用。"""
                try:
                    for _chunk in _stream_body():
                        yield _chunk
                finally:
                    _release(_stream_mid)

            resp = Response(gen(), mimetype="text/event-stream")
            resp.headers["Cache-Control"] = "no-cache"
            resp.headers["X-Accel-Buffering"] = "no"
            return _cors(resp)

        else:
            upstream = requests.post(url, json=body, headers=headers, timeout=(30, 1800))
            if _looks_overflow(upstream.status_code, upstream.text) and _hard_trim():
                upstream = requests.post(url, json=body, headers=headers, timeout=(30, 1800))

            lat = int((time.time() - started) * 1000)
            try:
                data = upstream.json()
            except Exception:
                return _cors(Response(upstream.content, status=upstream.status_code, mimetype="application/json"))
            usage = data.get("usage") if isinstance(data, dict) else None
            stat_code = int(upstream.status_code or 200)
            err = ""
            if stat_code < 200 or stat_code >= 300:
                err = (data.get("error", {}) or {}).get("message", "") if isinstance(data, dict) else ""
                if not err:
                    err = ("HTTP %d" % stat_code)
            _ct = int((usage or {}).get("completion_tokens", 0) or 0)
            _cached = int(((usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
            _tps = round(_ct / (lat / 1000.0), 1) if (lat and lat > 50 and _ct) else None
            record_usage(key["id"], model.get("display_name") or model.get("name"), usage,
                         latency_ms=lat, ok=(stat_code == 200), client=client_ip,
                         task_id=task_id, prompt_hash=prompt_hash, prompt_chars=prompt_chars,
                         status_code=stat_code, error=err[:200], tps=_tps, cached_tokens=_cached,
                         client_ip4=cip4, client_ip6=cip6)
            return _cors(Response(upstream.content, status=upstream.status_code, mimetype="application/json"))
    except requests.exceptions.RequestException as e:
        lat = int((time.time() - started) * 1000)
        record_usage(key["id"], model.get("display_name") or model.get("name"), None,
                     latency_ms=lat, ok=False, client=client_ip,
                     task_id=task_id, prompt_hash=prompt_hash, prompt_chars=prompt_chars,
                     status_code=502, error=str(e)[:200],
                     client_ip4=cip4, client_ip6=cip6)
        return _cors(jsonify({"error": {"message": "上游模型服务不可达: %s" % e, "type": "upstream_error"}})), 502


@app.route("/health", methods=["GET"])
def health():
    return _cors(jsonify({"status": "ok", "service": "llm-gateway"}))
