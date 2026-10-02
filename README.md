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

## AgentBeats 注册信息

在 [AgentBeats](https://agentbeats.dev/) 注册本 Purple Agent 时使用以下公开信息：

- **Agent 类型**：Purple Agent
- **Container image**：`ghcr.io/jiutian-zhiwei/mobileops-purple-agent:latest`
- **A2A 端口**：`9009`
- **平台**：`linux/amd64`
- **Amber manifest（注册用 Raw URL）**：

  ```text
  https://raw.githubusercontent.com/Jiutian-Zhiwei/mobileops-purple-agent/main/amber-manifest.json5
  ```

- **配置项**：`agent_llm`（默认 `deepseek/deepseek-chat`）+ `DEEPSEEK_API_KEY`

## 提交到 AgentBeats 排行榜

> 本仓库不包含 GitHub Actions 工作流（发布 PAT 缺少 `workflow` scope），镜像为手动
> `docker build` + `docker push` 发布，已有固定 digest。

**方式 A：Quick Submit（推荐）**

1. 登录 [agentbeats.dev](https://agentbeats.dev) → **Register Agent** → 类型选 **purple**，
   填上面的 image 与本仓库 URL，注册后拿到 `agentbeats_id`。
2. 打开 [tau2-bench green agent 页](https://agentbeats.dev/agentbeater/tau2-benchmark) → **Quick Submit**。
3. 选本 purple agent，config 填 `domain = "telecom"`，并填 `DEEPSEEK_API_KEY`。
4. 提交后平台会向排行榜仓库开一个 `quick-submit-<uuid>` 分支的 PR，由
   `quick-submit.yml` → Amber runner 完成评测并回写 `results/`。

**方式 B：Manual Submit**

fork [tau2-agentbeats-leaderboard](https://github.com/RDI-Foundation/tau2-agentbeats-leaderboard)，
改 `scenario.toml`：participants 段必须填 `agentbeats_id`（GitHub Actions 下
`generate_compose.py` 会拒绝 `image` 写法），`env` 里给
`AGENT_LLM = "deepseek/deepseek-chat"` 与 `DEEPSEEK_API_KEY = "${DEEPSEEK_API_KEY}"`，
并在 fork 仓库 Settings → Secrets 里配好 `DEEPSEEK_API_KEY`，push 到非 main 分支触发 workflow。
