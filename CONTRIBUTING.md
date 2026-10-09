# 贡献指南

感谢你愿意为 CSINS LLM Manager 出一份力。本文档说明本地开发、提交规范与几条**必须遵守的红线**。

## 开发环境

```bash
git clone https://github.com/owod-bin/csins-llm-manager.git
cd csins-llm-manager

python -m venv .venv
# Windows:  .venv\Scripts\activate
source .venv/bin/activate

pip install -r requirements.txt
python seed.py          # 可选：写入示例模型配置与若干 API Key
python run.py           # 管理后台 http://127.0.0.1:9090 ，网关 http://127.0.0.1:8000/v1
```

无需前端构建——`static/*.html` 是单文件页面，改完刷新即可。默认管理员账号为 `admin`，
首次登录后请立即改密码。

路径与端口都可通过 `config.json` 覆盖（先复制模板）：

```bash
cp config.json.example config.json
```

## 提交前自检

```bash
python -m compileall -q .        # 语法检查
python scripts/check_secrets.py  # 敏感信息扫描（CI 门禁，必须通过）
```

涉及网关、上下文裁剪、工具调用的改动，建议跑一遍仓库内附带的验证脚本：

| 脚本 | 覆盖内容 |
|---|---|
| `_verify_agent_p1.py` | 上下文裁剪：不丢 `user`、不产生孤儿 `tool` 消息、工具定义计入 token |
| `_verify_agent_e2e.py` | 端到端：工具调用、多轮 ReAct、超窗请求必须 200 |
| `_verify_ctxguard.py` / `_verify_ctxguard2.py` | 上下文保护开关与边界 |
| `_probe_ctx.py` | 直压上游探测真实可用窗口（结果**非单调**即为 slot 占用异常） |
| `_verify_filelog.py` | 日志落盘与轮转 |

## 🔴 红线（违反会被 CI 直接拦下）

1. **绝不提交运行时数据**：`data/`（含 API Key 明文、密码哈希、客户端 IP、调用记录）、
   `config.json`、`logs/`、`_mgr.log`。
   这些已在 `.gitignore` 中排除，**不要用 `git add -f` 绕过**。
2. **绝不提交真实凭据**：任何 API Key、Token、口令、私钥。
   文档示例请用 `sk-REPLACE_ME`、`<your-token>` 之类占位符。
3. **绝不提交内网 IP 与本机绝对路径**：示例中用 `192.168.x.x`、
   `C:\llama.cpp\llama-server.exe` 这类中性写法代替。

若不小心提交了密钥，**第一件事是去管理后台删除并重建该 Key**（删除即失效），
再考虑改写历史。

## 代码风格

- Python 3.10+，仅依赖 `flask` / `requests` / `psutil`，**不引入新的重量级依赖**；
  确有必要请在 PR 里说明理由。
- 注释与提交信息用中文，注释解释**为什么**这么做，而不是复述代码在做什么。
- 保持 `app/` 下模块职责单一：`store` 只管持久化、`server_manager` 只管进程、
  `gateway` 只管协议转换与鉴权。
- 前端沿用单文件风格，不引入构建工具链。

## 提交信息

使用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/) 前缀：

```
feat: 网关支持 embeddings 端点
fix: 裁剪时保留最近一条 user 消息
docs: README 补充 Ollama 接入示例
refactor: 抽出 _msg_body_text 避免回写冲掉 tool_calls
```

## 提交 Pull Request

1. 从 `main` 切出分支：`git checkout -b fix/ctx-trim-orphan-tool`
2. 完成改动并跑通上面的自检
3. 按模板填写 PR 描述，**在「敏感信息检查」小节如实勾选**
4. 保持 PR 聚焦单一主题，便于审阅

## 待办方向

欢迎从这些方向入手（也欢迎提出更好的想法）：

- 内置 `embeddings` / `reranking` 支持（当前 `/v1/embeddings` 返回 501）
- 受管模型的开机自启开关
- 多 slot 并发下的稳定性（在 `--kv-unified` 语义下给出确定性方案）

## 许可证

贡献即表示你同意以 [MIT License](LICENSE) 授权你的代码。
