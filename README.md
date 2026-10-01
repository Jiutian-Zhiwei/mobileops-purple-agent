# mobileops-agent — tau2-bench telecom purple agent

mobileops-agent 是面向 **tau2-bench telecom 域**（运营商客服：账户管理、账单/套餐变更、设备/连接排障）的参赛 agent。

核心是 **确定性政策合规底座 + LLM 兜底**：

- **Runbook 政策规则**：把 telecom policy 编译成确定性分支（决策前查：该不该做、允许做什么）。
- **EntityStore 状态跟踪**：识别客户 → 查线路 → 查账单 → 查数据用量。
- **双控边界**：assistant（agent 自己调工具）/ user（引导 simulated user 做设备操作）。
- **工具参数校验**：refuel ≤2GB、发支付请求前确认 overdue、合同过期不可 resume。

底层模型是可替换的 LLM（默认 `deepseek/deepseek-chat`），是内部实现细节。

## 目录结构

```
src/
├─ server.py      # A2A server + agent card
├─ executor.py    # A2A request handling（run 模式 + context_id 会话）
└─ agent.py       # 决策逻辑（确定性政策路由 + LLM 兜底）
Dockerfile
pyproject.toml
amber-manifest.json5
```

## 本地运行

```bash
uv sync
export AGENT_LLM=deepseek/deepseek-chat
export DEEPSEEK_API_KEY=sk-...
uv run src/server.py --host 0.0.0.0 --port 9009
```

## 构建 Docker 镜像

```bash
docker build -t mobileops-purple-agent .
docker run -p 9009:9009 -e AGENT_LLM=deepseek/deepseek-chat -e DEEPSEEK_API_KEY=sk-... mobileops-purple-agent
```

## 提交到 AgentBeats 排行榜

完整步骤见仓库外的《Phase5 提交指南》，简版：

1. **发布镜像**：把本仓库推送到你的 GitHub，GitHub Actions 自动发布到
   `ghcr.io/<你的用户名>/mobileops-purple-agent:latest`（见 `.github/workflows/test-and-publish.yml`）。
2. **注册 agent**：登录 [agentbeats.dev](https://agentbeats.dev) → Register Agent → 选 purple 类型，
   填镜像地址 + 仓库 URL，注册后点「Copy agent ID」复制 `agentbeats_id`。
3. **提交评估**（二选一）：
   - **Quick Submit**：在 [tau2-bench green agent 页](https://agentbeats.dev/agentbeater/tau2-benchmark)
     点 Quick Submit，选你的 purple agent，填 `domain=telecom` + API key。
   - **Manual Submit**：fork [tau2-agentbeats-leaderboard](https://github.com/RDI-Foundation/tau2-agentbeats-leaderboard)，
     编辑 `scenario.toml` 填 `agentbeats_id` + `AGENT_LLM`，配 secrets，push 触发 workflow。
