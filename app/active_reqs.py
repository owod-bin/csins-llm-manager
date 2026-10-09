"""进程内的共享状态（推理计数器等）。

两个 Flask 应用（gateway / admin）都通过此模块访问，保证计数一致。
"""

# model_id -> 当前正在进行的请求数
ACTIVE_REQS = {}