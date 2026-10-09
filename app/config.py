"""配置加载模块。

内置默认值全部基于**项目目录下的相对路径**，克隆后无需任何配置即可启动；
若引擎或模型放在别的磁盘，复制 config.json.example 为 config.json 后按需修改。
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

_EXE = "llama-server.exe" if os.name == "nt" else "llama-server"

_DEFAULTS = {
    "llama_cpp_dir": os.path.join(BASE_DIR, "llama.cpp"),
    "server_exe": os.path.join(BASE_DIR, "llama.cpp", _EXE),
    "ai_model_dir": os.path.join(BASE_DIR, "models"),
    "een_dir": os.path.join(BASE_DIR, "een"),
    "data_dir": os.path.join(BASE_DIR, "data"),
    "logs_dir": os.path.join(BASE_DIR, "logs"),
    "gateway_host": "0.0.0.0",
    "gateway_port": 8000,
    "internal_port_start": 18080,
    "manager_host": "0.0.0.0",
    "manager_port": 9090,
}


def load_config():
    cfg = dict(_DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            # 空值以及 "_" 开头的注释字段不覆盖默认值，
            # 这样 config.json 可以只写"需要改的那几项"。
            for k, v in data.items():
                if k.startswith("_") or v in ("", None):
                    continue
                cfg[k] = v
        except Exception:
            pass
    return cfg


CFG = load_config()
