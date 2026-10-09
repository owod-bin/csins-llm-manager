# CSINS大模型管理运维系统 (CSINS LLM Manager)

基于 **Python + Flask** 的中文 Web 管理后台，用于统一管理本地大模型部署（支持 llama.cpp / PrismML fork / vLLM / 自定义命令，以及 Ollama / LM Studio / OpenAI 兼容等外部服务），并提供 OpenAI 兼容网关与每 Key 用量统计。

## 目录结构

```
llm-manager/
├── run.py                  # 主入口（同时启动管理后台 + OpenAI 网关）
├── seed.py                 # 初始化脚本（写入示例模型配置 + 7 把 API Key）
├── 启动系统.bat            # Windows 一键启动（前台窗口）
├── _start_bg.bat           # Windows 后台启动
├── _restart_service.bat    # 重启管理进程（保留已加载的模型，不重新加载权重）
├── config.json.example     # 配置模板（复制为 config.json 后按需修改）
├── requirements.txt        # Python 依赖
├── app/
│   ├── config.py           # 配置加载（默认值均为项目内相对路径，克隆即可运行）
│   ├── store.py            # 数据持久化（模型/密钥/用量，原子写入）
│   ├── server_manager.py   # 推理引擎进程管理（构建命令行 / 启停 / 健康检查 / 日志）
│   ├── monitor.py          # 系统监控（CPU / 内存 / 磁盘 / GPU）
│   ├── netinfo.py          # 邻居表查询（补齐客户端 IPv4 / IPv6 对偶地址）
│   ├── active_reqs.py      # 进行中请求计数
│   ├── gateway.py          # OpenAI 兼容网关（鉴权 + 路由 + 上下文保护 + 用量统计）
│   └── admin.py            # 管理后台 API
├── static/
│   ├── index.html          # 管理界面（单文件，无构建步骤）
│   ├── user.html           # 普通用户自助页
│   └── autologin.html      # 免登录跳转页
└── data/                   # 运行时数据（首次运行自动生成，已在 .gitignore 中忽略）
```

## 安装

```bash
git clone <本仓库地址>
cd llm-manager
pip install -r requirements.txt
```

（可选）复制配置模板并按实际路径修改；**留空的项会使用项目目录下的默认相对路径**：

```bash
cp config.json.example config.json     # Windows: copy config.json.example config.json
```

```json
{
  "llama_cpp_dir": "C:\\llama.cpp",
  "server_exe": "C:\\llama.cpp\\llama-server.exe",
  "ai_model_dir": "D:\\models"
}
```

## 启动

```bash
python run.py
# 或双击 启动系统.bat（Windows）
```

- 管理后台：http://127.0.0.1:9090 （默认账号 `admin` / `admin`，**登录后请立即修改密码**）
- OpenAI 网关：http://127.0.0.1:8000/v1
- 局域网访问：http://<本机IP>:9090 和 http://<本机IP>:8000/v1
- 可选：运行 `python seed.py` 写入示例模型配置并生成 7 把 API Key（幂等，可重复执行）

## 功能

| 模块 | 说明 |
|------|------|
| 系统监控 | CPU / 内存 / 磁盘 / GPU(显存·利用率·温度·功耗) 实时刷新；模型运行状态 |
| 模型管理 | 新增/编辑/删除模型；按**推理引擎**自适应表单；界面启停/重启/连接；查看启动命令；实时日志 |
| 参数配置 | 模型路径、mmproj、上下文、并发、KV 量化、批处理、采样参数等，**保存即持久化**，运行中修改自动重启生效 |
| API 密钥 | 创建/禁用/删除 Key；可限定可访问模型；每把 Key 独立 |
| 用量统计 | 每把 Key 的请求数、输入/输出/总 Token、按模型明细、调用历史 |
| 对话测试 | 界面直接与已启动模型对话，支持多轮与图像输入 |
- **使用登记**：谁在使用 / 提前申请 / 使用完毕，全员可见当前占用与使用时段

## API 调用（OpenAI 兼容）

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <你的API Key>" \
  -d '{"model":"qwen3.8-27b","messages":[{"role":"user","content":"你好"}]}'
```

Python:
```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="<你的API Key>")
r = client.chat.completions.create(model="qwen3.8-27b",
        messages=[{"role":"user","content":"你好"}])
print(r.choices[0].message.content)
```

网关按请求中的 `model` 字段自动路由到对应模型的内部端口，并记录用量。

## 推理引擎接入方式（多引擎）

「新增 / 编辑模型」弹窗顶部提供 **推理引擎 / 接入方式** 下拉，选择后表单自动切换为对应引擎的参数项：

| 引擎 id | 名称 | 类型 | 说明 |
|--------|------|------|------|
| `llamacpp` | llama.cpp / PrismML fork | 受管进程 | 默认引擎；支持每模型独立的 `server_exe`（可用于三值 Bonsai2 的 PrismML fork） |
| `vllm` | vLLM | 受管进程 | 需 `een_dir` 指向的 Python 环境安装 vllm；参数：模型目录、dtype、显存比、张量并行 |
| `custom` | 自定义命令 | 受管进程 | 自定义命令模板，占位符 `{model_path} {mmproj_path} {port} {host} {ctx} {alias}` |
| `ollama` | Ollama | 外部服务 | 指向已运行的 Ollama（默认端口 11434），界面「连接 / 断开」 |
| `lmstudio` | LM Studio | 外部服务 | 指向已运行的 LM Studio（默认端口 1234） |
| `openai` | OpenAI 兼容接口 | 外部服务 | 任意 OpenAI 兼容服务（`base_url` + `api_key`），可接远程/云端 |

- **受管进程**：由本系统启动/停止，显示 启动 / 停止 / 重启 按钮。
- **外部服务**：本系统不启动进程，仅探测 `/v1/models` 后接管；显示 连接 / 断开 按钮。外部服务同样支持网关路由、7 把 Key、用量 Dashboard。
- **单模型互斥**：默认开启（`exclusive_mode`，系统设置页可切换）。启动一个模型前会自动停止其它正在运行的模型，避免 24GB 显存冲突。
- **外部进程接管**：若模型由外部命令启动（绕过本系统），管理后台会按监听端口反查 PID 自动接管，重启管理进程后依然可识别。

## 示例部署（供参考）

以下配置在 24 GB 显存环境下实测稳定，可作为 `seed.py` 之外的参考模板。

### 1) Qwen3.8-27B（日常主用）
- **模型**：Qwen3.8-27B (UD-Q4_K_M, 15.33 GB) + mmproj-F16 (884 MB 视觉)
- **上下文**：131072 (128K)
- **KV 量化**：`q8_0` / `q8_0`
- **引擎**：llama.cpp（CUDA）
- **端口**：模型内部 18080

### 2) Bonsai2 27B（三值量化）
- **模型**：Ternary-Bonsai-2-27B-PQ2_0 (7.21 GB) + mmproj-Q8_0 (0.63 GB 视觉)
- 两条条目：`bonsai2-27b-128k`（131072 / 端口 18081）、`bonsai2-27b-262k`（262144 / 端口 18082）
- **KV 量化**：`q8_0` / `q8_0`（262K 必须量化 KV，否则显存不足）
- **引擎**：PrismML fork
  （**注意**：三值 GGUF 只能由 PrismML fork 加载，官方 llama.cpp 无法识别）
- **端口**：管理后台 9090 / 网关 8000

> ⚠️ **并发 slot 数 `-np` 建议保持 1。**
> 在 `--kv-unified` 下所有 slot 共享同一块 KV 缓冲，`-np > 1` 时多个 slot 会互相挤压可用上下文；
> 异常终止的请求还可能留下无法回收的 slot，导致**与输入长度完全不成比例**的随机
> `Context size has been exceeded`（实测 5.6 万 tokens 报错、11 万 tokens 反而成功）。
> 单机单用户场景请使用 `-np 1`。

## 数据文件说明

- `data/models.json` — 模型配置（永久保存）
- `data/keys.json` — API Key 列表（**含明文 Key，已在 `.gitignore` 中忽略，切勿提交**）
- `data/usage.json` — 用量统计（每 Key 最多保留 500 条历史）
- `data/settings.json` — 全局设置
- `data/users.json` / `data/logs.json` / `data/bookings.json` — 账户、使用日志、使用登记
- `<logs_dir>/model-<id>.log` — 各模型运行日志（默认 `logs/`，可在 `config.json` 指定）

## 每 Key 用量 Dashboard

> **统计口径**：用量统计**仅统计「通过该 API Key 调用网关 `/v1`（含 `/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`）所产生的 token 消耗**。系统内「对话测试」页面属于内部自测功能，**不计入**任何 Key 的用量统计。

在「API 密钥与用量」页面，点击任意 Key 的 **查看用量** 按钮，打开该 Key 的独立用量 Dashboard（深色主题）：

- **时间范围**：今日 / 近 7 天 / 近 30 天 / 全部（右上角分段按钮，切换即刷新）
- **核心指标卡**：请求次数、输入 Token、输出 Token、总 Token（范围值 + 累计值）
- **每日调用量图表**：按天双柱 —— 紫色=请求次数，青色=Token（相对量，各自独立归一化到自身最大值，便于观察趋势）
- **按模型拆分**：每个模型的请求数、Token 明细与 sparkline 趋势
- **性能与可靠度**：成功率、平均延迟（含 P95）、平均速度 tok/s（含 TTFT 首 token 延迟）、缓存命中率与命中 Token 数；并列出**错误码分布**与**上下文长度分布**（<1K / 1-8K / 8-32K / 32-128K / 128K+）
- **调用任务表**：按「任务」聚合的调用记录，列为 时间 / 任务 ID / 来源 (IP) / Token（输入↓ 输出↑）/ 使用模型 / 耗时·速度 / 状态
  - **来源 (IP)**：**默认显示 IPv4，鼠标悬停显示该客户端的 IPv6**。
    - 取值为发起调用的客户端 IP（优先取 `X-Forwarded-For` / `X-Real-IP`，否则取直连地址）。
    - 一次 TCP 连接只携带一个地址族；系统会用**本机邻居表按 MAC 关联**，尽力补齐同一台主机在同一网段内的另一地址族（Windows `GetIpNetTable2`，<1ms，失败静默降级）。
    - 本机调用显示 `127.0.0.1`，悬停显示 `::1`。
    - 跨网段/经 NAT 的客户端无法关联出另一族；历史旧记录可能显示 User-Agent（如 `hertz`、`WorkBuddy/...`）；此类非 IP 值悬停显示为「来源：<值>」。
  - 状态徽章：绿色「✓ 成功」、橙色「⚠ 部分失败」、红色「✗ 失败」
  - 同一任务多次调用显示 `×N` 标记；同一任务内按最早一次调用的时间归并

### 隐私口径（重要）

> 用量统计**只记录「量」与元数据，不记录对话内容**。
>
> - 网关**不保存、不落盘**任何请求或回复原文；
> - 每条记录仅保存请求内容的 **SHA-256 摘要（前 16 位）** 与**字符数**，用于关联/去重，无法还原内容；
> - 任务表展示「任务 ID」，不再展示请求内容；
> - 若需要内容级留痕（审计用途），应另建独立审计模块（默认关闭、加密存储、单独权限、可设保留期），与用量页分离。

### 任务（Task）聚合规则

网关在代理每次请求时按以下规则归并为一个「任务」：

1. 若请求头带 `X-Task-Id`，以其作为任务 ID（推荐 agent/客户端显式传入）；
2. 否则按 `Key + 时间窗口（默认 30 秒，见 `gateway.py` 的 `_get_task_id`）` 自动归并为 `auto-<key片段>-<时间桶>`；
3. 同一任务内的调用若全部 2xx 且无错误 → 成功；全部失败 → 失败；混合 → 部分失败。

### 新增采集字段（用于性能分析）

每次调用额外记录（均不含内容）：

| 字段 | 说明 |
| --- | --- |
| `prompt_hash` | 请求内容 SHA-256 前 16 位（不可逆） |
| `prompt_chars` | 请求内容字符数 |
| `ttft_ms` | 首 token 延迟（流式；兼容 content / reasoning_content / text） |
| `tps` | 输出速度 tok/s |
| `cached_tokens` | 命中前缀缓存的 Token 数（`prompt_tokens_details.cached_tokens`） |
| `bucket` | 输入 Token 分桶（<1K / 1-8K / 8-32K / 32-128K / 128K+） |
| `client_ip4` / `client_ip6` | 来源地址对（默认展示 IPv4、悬停展示 IPv6） |


> 说明：历史遗留记录（无任务信息）会各自独立成一个任务，状态视为成功，不影响既有统计聚合。

### 导出与重置

- Dashboard 底部的「重置该 Key 用量」可清空该 Key 的统计（不可撤销）。
- 用量数据持久化于 `data/usage.json`，每 Key 最多保留 500 条任务历史。
- `GET /api/keys/<kid>/usage?days=&page=&page_size=` 返回 `analytics`（性能与可靠度）字段。

## 上下文保护（防任务中断）

> 目标：调用大模型持续完成长任务时，不因"上下文超限"报错中断，且使用者无感知。

### 背景
本机模型为**混合注意力架构**，llama.cpp 的 `--context-shift`（KV 平移）会被自动禁用
（启动日志：`KV cache shifting is not supported for this context`），因此
llama-server 在输入超出窗口时会直接返回 `400 exceed_context_size_error`。
实测：输入 148191 tokens > 窗口 131072 → 直连上游必报 400。

### 做法
网关（`gateway.py`）在转发前把好关，仅在"否则必然溢出"时才裁剪：
1. 粗略估算输入长度（CJK≈1/字、其他≈1/4 字节），**低于窗口 60% 直接放行**（零额外开销）；
2. 接近上限时，用上游 `/apply-template` + `/tokenize` **精确计数**；
   工具定义（JSON Schema）占用一并计入 —— `/apply-template` 支持 `tools` 参数，会按真实对话模板
   渲染后再计数，避免"估算够、实际超窗"；
3. 若"输入 + max_tokens"会溢出 → 静默下调 `max_tokens` 到剩余窗口；
4. 若输入本身超窗 → **保留 system 指令 + 尽可能多的最新消息**，丢弃最早的历史；
   单条消息过长则保留首尾、中段以中文省略标记替代；裁剪后保证首条为 user；
5. **工具调用按「块」裁剪**：一次工具调用由「`assistant(tool_calls)` + 紧随的若干 `role=tool` 结果」共同构成，
   二者被绑定为一个**不可拆分的原子块**，整块保留或整块丢弃 —— Qwen 系模板不使用 `tool_call_id`，
   完全依赖消息顺序配对，拆开会导致语义错乱甚至上游 500；另有孤儿 tool 消息兜底清理；
6. 保证裁剪结果落进窗口 → 请求必定成功，任务继续跑。

### 结果（实测）
| 场景 | 结果 |
| --- | --- |
| 输入 148191 tokens（> 131072）直连上游 | `400 exceed_context_size_error` |
| 同一请求经网关 | **200 成功**，响应头 `X-Ctx-Guard: trimmed; dropped=46; in=148191` |
| 同一任务再次对话 | 仍 200，可持续对话不中断 |
| 正常小请求 | 不触发、不加开销（无 `X-Ctx-Guard` 头） |

### 开关
- 系统设置页「🛡️ 上下文保护（防任务中断）」开关，默认**开启**；
- 全局设置键 `ctx_guard`（`data/settings.json` → `general.ctx_guard`），
  接口 `POST /api/settings/general {"ctx_guard": true|false}`；
- 仅对本地 llama.cpp 类引擎生效（外部引擎如 OpenAI/Ollama 不裁剪）。

### 语义取舍
只丢弃最早的对话、始终保留系统指令与最新上下文，是业界通行做法；
仅在"否则必然 400 死掉"时才裁剪，属严格更优。若需内容级完整留痕，
应另建独立审计模块（与用量页分离），而非在网关内保留全文。

## 使用登记（谁在用 / 预约 / 记录）

为解决"多人共用一套本地模型、互相不知道谁在用"的问题，新增「👥 使用登记」页面。

### 功能
- **登记使用**：填写「使用人 + 使用时间范围（自由文本，如 `09-28 14:00 ~ 18:00`
  或 `今晚 19 点开始`）+ 备注」，可一键「立即开始使用」或「提前申请」。
- **正在使用**：绿色状态徽章 + 使用人 + 时间范围 + 开始时间 + 备注；可「标记使用完毕」「删除」。
- **使用申请（预约）**：提前占位登记；到点后点「开始使用」转为使用中。
- **使用记录**：使用完毕的历史（使用人 / 时间范围 / 开始 / 结束），保留最近 50 条。
- **系统监控页**联动：顶部「👥 当前使用情况」卡片实时显示"谁正在使用"或
  "当前无人使用，可自由使用"，方便全员一眼看清占用情况。

### 交互
- 前端每 **8 秒**自动刷新登记状态（切到其它页也会后台刷新）；
- 使用时间范围为**纯文本**，由填写人自定义，适配各种排班/口语化表达。

### 数据与接口
- 数据文件：`data/bookings.json`（`items[]`，单条字段：`id / user / status /
  time_text / note / operator / created_at / updated_at / started_at / ended_at`）；
- `status` 取值：`using`（正在使用）/ `reserved`（预约申请）/ `done`（使用完毕）；
- 接口（均需登录态）：
  - `GET    /api/bookings`            读取视图（active / reserved / done / counts）
  - `POST   /api/bookings`            新增（`user` 必填；`status` 默认 using）
  - `PUT    /api/bookings/<id>`       更新（可改 user / time_text / note / status）
  - `POST   /api/bookings/<id>/finish` 直接标记使用完毕
  - `DELETE /api/bookings/<id>`       删除
- 单条记录最多保留 500 条（超出丢弃最旧）。


## 角色与权限（RBAC）

系统内置两类角色，登录后按角色展示菜单与操作入口；后端接口同样做守卫（前端隐藏 ≠ 唯一防线）。

### 账户模型
- 数据文件 `data/users.json`，字段：`id / username / display_name / role / enabled / password(哈希) / note / created_at / last_login`。
- 首次启动自动写入内置管理员 `admin`（初始密码 `admin`，**请登录后立即修改**），
  兼容旧配置：若 `settings.json` 里存在 `admin_password`，会平滑迁移为管理员账号。
- 密码使用 `salt$sha256(salt+pw)` 形式存储，**不保存明文**。

### 权限矩阵
| 能力 | 管理员 | 普通用户 |
| --- | --- | --- |
| 系统监控查看 | ✅ | ✅ |
| 模型管理：启停 / 查看日志 | ✅ | ✅ |
| 模型管理：新建 / 编辑 / 删除 | ✅ | ❌ |
| API 密钥：查看全部与用量 | ✅ | ✅ |
| API 密钥：启用/禁用、编辑 | ✅ 全部 | 仅自己创建的 |
| API 密钥：新建 / 删除 | ✅ | ❌ |
| 对话测试 | ✅ | ✅ |
| 使用登记：登记 / 结束 / 申请 | ✅ | ✅ |
| 使用登记：删除记录 | ✅ | ❌ |
| 账户管理页 | ✅ | 不可见 |
| 使用日志页 | ✅ | 不可见 |
| 系统设置 / 重置用量 / 清空日志 | ✅ | ❌ |
| 修改密码 | 改自己 | 改自己 |

### 使用日志
- 记录关键操作：登录 / 登出 / 修改密码 / 新建-编辑-删除密钥 / 新建-编辑-删除模型 /
  模型启停 / 新建-编辑-删除账户 / 删除使用记录 / 清空日志 / 重置用量 / 修改系统设置。
- 每条含：时间、用户、角色、操作、详情、**来源 IP**、结果（成功/失败）。
- **管理员操作同样记录**（管理员与普通用户一视同仁，均可追溯到人、到 IP）；
  支持按角色筛选「仅管理员 / 仅普通用户」，页脚显示「管理员 N / 普通用户 M」分角色计数。
- 保留上限 3000 条（超出丢弃最早）；支持按 用户 / 操作 / 结果 / 关键字 筛选；
  管理员可逐条删除或一键清空。

### 接口
- `POST   /api/login`               返回 `token / username / role / display_name / ttl`
- `GET    /api/me`                  当前会话身份（前端据此渲染角色菜单）
- `GET    /api/users`               账户列表（管理员）
- `POST   /api/users`               新建账户（管理员）
- `PUT    /api/users/<id>`          更新账户（管理员）
- `DELETE /api/users/<id>`          删除账户（管理员；不能删自己）
- `GET    /api/logs`                使用日志（管理员；支持 user/action/result/q/limit）
- `DELETE /api/logs/<id>`           删除单条日志（管理员）
- `DELETE /api/logs`                清空日志（管理员）

## 开源许可

本项目采用 [MIT License](LICENSE) 开源。

## 贡献

欢迎提交 Issue 与 Pull Request。

## ⚠️ 安全提示

本系统会在 `data/` 目录下保存 **API Key 明文、账户密码哈希、调用记录（含客户端 IP）**。

- 仓库已通过 `.gitignore` 排除 `data/`、`config.json`、日志与备份文件；
- **请勿使用 `git add -f` 强制添加**这些内容，也不要把它们上传到任何公开位置；
- 若曾误提交过密钥，请立即在管理后台**删除并重建对应 API Key**（删除旧 Key 即可使其失效）。
