"""初始化：写入默认模型配置（Qwen3.8-27B + Bonsai2 128K/262K）+ 7 把 API Key。

幂等，可重复执行；已存在的模型 / Key 会跳过。

模型文件路径由 config.json 的 `ai_model_dir` 推导，如需自定义请先配置好 config.json。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import store  # noqa: E402
from app.config import CFG  # noqa: E402

IS_WIN = os.name == "nt"
_MODEL_DIR = CFG.get("ai_model_dir") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models")
_ENGINE_DIR = os.path.dirname(CFG.get("llama_cpp_dir") or "")

QWEN_DIR = os.path.join(_MODEL_DIR, "Qwen3.8-27B")
QWEN_FILE = os.path.join(QWEN_DIR, "Qwen3.8-27B-UD-Q4_K_M.gguf")
QWEN_MMPROJ = os.path.join(QWEN_DIR, "mmproj-F16.gguf")

BONSAI_DIR = os.path.join(_MODEL_DIR, "Ternary-Bonsai-2-27B")
BONSAI_FILE = os.path.join(BONSAI_DIR, "Ternary-Bonsai-2-27B-PQ2_0.gguf")
BONSAI_MMPROJ = os.path.join(BONSAI_DIR, "mmproj-Q8_0.gguf")
# 三值 GGUF 需由 PrismML fork 加载，与官方 llama.cpp 并列放置
BONSAI_EXE = os.path.join(_ENGINE_DIR, "llama-bonsai",
                          "llama-server.exe" if IS_WIN else "llama-server")

MODELS = [
    dict(name="qwen3.8-27b", display_name="Qwen3.8-27B", alias="qwen3.8-27b",
         description="Qwen3.8-27B，Q4_K_M，原生视觉，128K 上下文",
         engine="llamacpp", server_exe="",
         model_path=QWEN_FILE, mmproj_path=QWEN_MMPROJ,
         internal_port=18080, ctx=131072, np=1, temp=0.7),
    dict(name="bonsai2-27b-128k", display_name="Bonsai2 27B · 128K", alias="bonsai2-128k",
         description="Bonsai2 三值 27B（PQ2_0），128K，日常档",
         engine="llamacpp", server_exe=BONSAI_EXE,
         model_path=BONSAI_FILE, mmproj_path=BONSAI_MMPROJ,
         internal_port=18081, ctx=131072, np=1, temp=1.0),
    dict(name="bonsai2-27b-262k", display_name="Bonsai2 27B · 262K", alias="bonsai2-262k",
         description="Bonsai2 三值 27B（PQ2_0），262K，长文档档",
         engine="llamacpp", server_exe=BONSAI_EXE,
         model_path=BONSAI_FILE, mmproj_path=BONSAI_MMPROJ,
         internal_port=18082, ctx=262144, np=1, temp=1.0),
]

existing = {m.get("name") for m in store.get_models()}
for spec in MODELS:
    if spec["name"] in existing:
        print("已存在，跳过:", spec["name"]); continue
    m = store.default_model()
    m.update(spec)
    m.update({
        "kv_unified": True, "ctk": "q8_0", "ctv": "q8_0", "ngl": 999,
        "fa": True, "jinja": True, "cont_batching": True,
        "batch": 2048, "ubatch": 512, "no_mmap": True,
        "top_p": 0.95, "top_k": 20, "extra_args": [], "autostart": False,
    })
    store.upsert_model(m)
    print("已创建模型配置: %s port=%s ctx=%s np=%s" % (spec["name"], m["internal_port"], m["ctx"], m["np"]))

# 7 把 Key
keys = store.get_keys()
existing_keys = {k.get("name") for k in keys}
added = 0
for i in range(1, 8):
    nm = "user%d" % i
    if nm not in existing_keys:
        keys.append(store.new_api_key(name=nm, models=["*"], note="自动初始化"))
        added += 1
store.save_keys(keys)
print("新增 API Key:", added, "把；当前共", len(keys), "把")
print("（完整 Key 仅保存在 data/keys.json，可登录管理后台查看；此处只显示前缀）")
for k in keys:
    print("  %-8s %s…" % (k["name"], str(k.get("key", ""))[:8]))
