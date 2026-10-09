# -*- coding: utf-8 -*-
"""诊断：确认「思考链占满输出额度」导致 MAX_TOKENS 截断。"""
import json
import time

import requests

GW = "http://127.0.0.1:8000/v1/chat/completions"
KEY = "sk-REPLACE_ME"
S = requests.Session()
S.trust_env = False  # 绕过本机代理环境变量

PROMPT = "一个农夫有17只羊，除了9只以外都死了，还剩几只？请一步步解释你的推理过程。"


def run(tag, max_tokens, thinking=None, budget_note=""):
    body = {
        "model": "bonsai2-128k",
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": max_tokens,
        "stream": False,
    }
    if thinking is False:
        body["chat_template_kwargs"] = {"enable_thinking": False}
    elif thinking is True:
        body["chat_template_kwargs"] = {"enable_thinking": True}
    t0 = time.time()
    try:
        r = S.post(GW, json=body, headers={"Authorization": "Bearer " + KEY,
                                           "Content-Type": "application/json"},
                   timeout=900)
    except Exception as e:
        print("[%s] 请求异常: %s" % (tag, e))
        return
    dt = time.time() - t0
    try:
        d = r.json()
    except Exception:
        print("[%s] 非 JSON 响应 %s: %s" % (tag, r.status_code, r.text[:300]))
        return
    ch = (d.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    u = d.get("usage") or {}
    rc = msg.get("reasoning_content") or ""
    ct = msg.get("content") or ""
    print("=" * 70)
    print("[%s] %s  (thinking=%s %s)" % (tag, "OK" if r.status_code == 200 else r.status_code,
                                         thinking, budget_note))
    print("  finish_reason : %s" % ch.get("finish_reason"))
    print("  usage         : prompt=%s completion=%s" % (u.get("prompt_tokens"), u.get("completion_tokens")))
    print("  思维链长度     : %d 字" % len(rc))
    print("  正式回答长度   : %d 字" % len(ct))
    print("  耗时          : %.1fs" % dt)
    print("  思维链尾部     : %s" % repr(rc[-120:]))
    print("  回答开头       : %s" % repr(ct[:120]))


if __name__ == "__main__":
    run("A 思考开启 max=1024", 1024, True)
    run("B 思考关闭 max=1024", 1024, False)
