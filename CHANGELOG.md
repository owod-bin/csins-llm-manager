# 更新日志

本项目遵循 [语义化版本 2.0.0](https://semver.org/lang/zh-CN/) 与
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 约定。

## [Unreleased]

### 计划中

- 内置 embeddings / reranking 支持（当前 `/v1/embeddings` 返回 501，需在模型 `extra_args` 手写
  `--embeddings` 或改造受管引擎参数生成逻辑）。
- 模型开机自启开关（当前受管模型默认 `autostart=false`，管理进程重启后需手动 Start）。

## [1.0.0] - 2026-10-09

首个开源版本。

### 新增 · 管理后台

- **统一 Web 管理界面**（Flask，默认 `:9090`）：中文单文件前端，无构建步骤，克隆即可运行。
- **多引擎接入**：受管进程（llama.cpp、PrismML fork、vLLM、自定义命令）与外部服务
  （Ollama、LM Studio、任意 OpenAI 兼容端点）统一编排。
- **模型生命周期管理**：一键启动 / 停止 / 重启、排他模式（同一时间只保留一个受管引擎进程）、
  内部端口自动分配、健康检查、按模型落盘运行日志。
- **系统监控**：CPU / 内存 / 磁盘 / GPU 占用。
- **角色与权限（RBAC）**：管理员与普通用户两档，权限矩阵覆盖各接口；关键操作全部落使用日志。
- **使用登记**：谁在用 / 预约 / 使用记录，含客户端 IPv4 与 IPv6 对偶地址补齐。

### 新增 · OpenAI 兼容网关

- 默认 `:8000`，提供 `/v1/chat/completions`、`/v1/models` 等标准端点。
- **API Key 分发与鉴权**：多 Key 管理、启用 / 禁用、备注。
- **SSE 流式纯透传**：不重组分片，完整保留 `tool_calls` 增量。
- **每 Key 用量统计**：请求数 / Token 数 / 任务（Task）聚合，支持导出与重置；
  隐私口径明确（默认不落盘对话正文）。
- **思考策略四级覆盖**：请求级 > API Key 级 > 模型级 > 全局默认。
- **思考强度归一化**：`high / max / ultra → xhigh`、`minimal → low`、`off / none → 关闭思考`
  （显式传 `enable_thinking=false`），规避不同模型模板的取值白名单校验失败。
- **思考预留（`thinking_headroom`）**：思考开启时自动抬高上游 `max_tokens`，
  避免思维链吃光输出额度、正式回答被截断（`finish_reason=length`）。

### 新增 · Agent 场景支持

- **工具调用全链路**：非流式与流式的 `tool_calls`、多轮回传工具结果、ReAct 多轮循环。
- **KV 缓存复用**：多轮循环中 `cached_tokens` 递增。
- **多模态输入**：图片（`image_url`）。
- **上下文自动保护（`ctx_guard`）**：见下方「修复」。

### 修复

- **上下文裁剪丢失 `user` 消息**：长任务的典型形态是「user 在前、其后一长串 assistant」，
  自后向前裁剪会把 `user` 全部丢弃，上游模板随即抛 `No user query found in messages` 直接 500。
  现强制保留最近一条 `user`，并修正开头修剪策略（实测保留历史比例由约 16% 提升到 80% 以上）。
- **裁剪产生孤儿 `tool` 消息**：qwen 系模板**不使用 `tool_call_id`**，工具结果靠**消息顺序**
  与 `assistant.tool_calls` 配对，孤立的结果消息会造成语义损坏。现把「`assistant`(tool_calls)
  + 紧随的若干条工具结果」绑定为**原子块**，整块保留或整块丢弃，并加孤儿清理兜底。
- **工具调用 token 未计入**：`_msg_text()` 忽略 `assistant.tool_calls`，且精确计数调用上游
  `/apply-template` 时未传 `tools`，导致输入长度**双重低估**、裁剪介入过晚。现把 `tools`
  一并交给模板渲染计入 prompt，并单独提取正文用于回写，避免覆盖 `tool_calls` 结构。
- **随机超窗导致 Agent 任务无故中断**：受管引擎默认 `-np 3` 配合 `--kv-unified`，多个 slot
  共享同一块 `n_ctx` 缓冲，异常路径还会留下无法清理的僵尸 slot，表现为**与输入长度不成比例**
  的随机 500（实测 53K tokens 失败而 110K tokens 成功）。单用户场景默认改为 `-np 1`。
- **日志不落盘**：改为在进程层同时捕获 stdout / stderr 与 `logging` 输出并落盘，
  与启动方式无关；单文件超过 20MB 自动轮转为 `_mgr.log.1`。
- **客户端上下文配置超窗**：示例配置的输入上限不再等于模型窗口全量，为输出额度与思考预留留出空间。

### 文档

- README 覆盖安装 / 启动 / 功能 / API / 多引擎接入 / 数据文件 / 用量 Dashboard /
  上下文保护 / 使用登记 / RBAC / 安全提示。
- 附带 16 个可独立复跑的验证与诊断脚本（工具调用、ReAct 循环、上下文裁剪、窗口探测、日志落盘等）。
- 新增 `CONTRIBUTING.md` 贡献指南与 Issue / Pull Request 模板。

### 安全

- `data/`（API Key 明文、账户密码哈希、客户端 IP、调用记录）与本地 `config.json`
  已由 `.gitignore` 排除，不随仓库分发。
- 内置敏感信息扫描脚本 `scripts/check_secrets.py`，CI 自动拦截误提交的密钥 / 内网 IP / 本机绝对路径。

[Unreleased]: https://github.com/owod-bin/csins-llm-manager/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/owod-bin/csins-llm-manager/releases/tag/v1.0.0
