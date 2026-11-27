# codex

把 Codex 做成 LangGraph 图 `codex`，由 [Zorvex](../zorvex) 提供服务：在 [OpenSandbox](https://github.com/alibaba/OpenSandbox)
沙箱里运行 `codex app-server`，用 [codex-python](https://github.com/gersmann/codex-python) 客户端驱动它。每个 assistant
一个沙箱，由它的所有线程共用；每个线程是一个 Codex 会话，有自己的工作区 `/data/workspace/<thread>`。一次运行把线程里最新的
用户消息作为一轮（turn）发给 Codex，并把 Codex 的消息和工具调用流式写回线程；下一次运行接着这个会话继续。Codex 请求审批或
向人提问时，运行停在 LangGraph 的 interrupt 上（见「人工介入（HITL）」）。

## 运行

Zorvex 的数据存放在 PostgreSQL 的 `codex` schema 里。schema 要事先建好，表由 Zorvex 在启动时自动迁移创建：

```sql
CREATE SCHEMA codex;
```

本机开发时，Docker 只跑 OpenSandbox server 和沙箱镜像，服务本身用 `zorvex dev` 跑在本机：

```bash
cp .env.example .env            # 填数据库密码、API_KEY、OPEN_SANDBOX_API_KEY 和 SANDBOX_MOUNT_PATH，构建要走代理时填 BUILD_PROXY
docker compose up -d --build    # 启动 127.0.0.1:8080 上的 OpenSandbox server，并构建沙箱镜像
uv sync
uv run zorvex dev --port 2027 --no-db-check
```

服务器上用同一个 `docker-compose.yml`，把服务本身也作为容器启动，见 [DEPLOY.md](DEPLOY.md)。

## 环境变量

全部必填，由 `src/agent/settings.py` 读取；缺任何一个，服务都会在启动时停止。

| 变量 | 说明 |
|---|---|
| `DATABASE_URI`、`REDIS_URI` | Zorvex 使用的 PostgreSQL 和 Redis。 |
| `API_KEY` | 调用 API 的密钥，至少 32 位，可以用 `openssl rand -hex 32` 生成。 |
| `OPEN_SANDBOX_DOMAIN` | OpenSandbox server 的地址，用 `docker-compose.yml` 启动时是 `127.0.0.1:8080`。 |
| `OPEN_SANDBOX_API_KEY` | OpenSandbox server 的 API key，`docker-compose.yml` 也会把它交给 server。它同时用来签发每个沙箱的 app-server token。 |
| `SANDBOX_MOUNT_PATH` | assistant 的 skills 所在目录：每个沙箱把 `<path>/.agent/skills/<assistant_id>` 只读挂载到 `/etc/codex/skills`。可以写 Windows 路径（不加引号或加单引号），也可以写 Docker 主机上的路径。 |
| `SANDBOX_IDLE` | 沙箱在最后一次运行之后保留多久，用 ISO 8601 时长表示，如 `PT1H`。 |
| `SANDBOX_RETENTION` | 线程的会话和工作区在最后一次运行之后保留多久，如 `P7D`。 |

`docker-compose.yml` 另外读两个可选变量：

| 变量 | 说明 |
|---|---|
| `BUILD_PROXY` | 构建镜像时用的 HTTP 代理，留空则直连。本机 Docker Desktop 经 Clash 上网时填 `http://http.docker.internal:3128`。 |
| `COMPOSE_PROFILES` | 填 `server` 时，`docker compose up` 把本服务也作为容器启动；本机开发留空。 |

## 鉴权

除 `/ok` 和 `/info` 外，所有请求都要在 `x-api-key` 请求头里带上 `API_KEY`，否则返回 401。langgraph_sdk 的
`get_client(url=..., api_key=...)` 会自动带上这个请求头。鉴权由 `src/agent/auth.py` 完成，通过的请求都算同一个用户，
所以能看到全部线程。

## Assistant 配置

assistant 的 context（`src/agent/context.py`）有四个字段，全部必填，不接受其他字段：

| 字段 | 说明 |
|---|---|
| `base_url` | LiteLLM 网关的 OpenAI 兼容 base URL，沙箱要能访问到。 |
| `api_key` | 网关的 key。沙箱里只有一个占位符，由 OpenSandbox 的 Credential Vault 在发往网关的请求上换成真实的 key。 |
| `allowed_hosts` | 除网关之外沙箱可以访问的主机，`[]` 表示没有；其余一律拦截。 |
| `codex` | Codex 自身的配置，写法和它的 `config.toml` 一样，对所有线程生效。设 `sandbox_mode = "danger-full-access"` 和 `approval_policy = "never"`：隔离边界是 OpenSandbox 沙箱，Codex 自己的沙箱在里面跑不起来。不设 `model` 时，Codex 从网关的 `/models` 列表里挑模型，这要求 LiteLLM v1.106 或更新版本。 |

`base_url`、`api_key`、`allowed_hosts` 以及是否设置了 `codex.model` 决定了沙箱本身，改了其中任何一项，下次运行就会换一个新沙箱；
工作区和会话在卷里，不受影响。context 按原样存储，包括 key。

系统 assistant `codex` 属于服务端，所以在数据库里配置：`codex.assistant` 里它那一行的 `config.configurable` 就是 context，
每次修改还要在 `codex.assistant_versions` 里新增一行。

```sql
BEGIN;

UPDATE codex.assistant AS a
SET config = a.config || jsonb_build_object('configurable',
        coalesce(a.config -> 'configurable', '{}'::jsonb) || jsonb_build_object(
            'base_url', 'https://litellm.example.com/v1',
            'api_key',  'sk-...',
            'allowed_hosts', '[]'::jsonb,
            'codex', jsonb_build_object(
                'model', 'codex/gpt-5.6-luna',
                'sandbox_mode', 'danger-full-access',
                'approval_policy', 'never')
        )),
    version = (SELECT coalesce(max(v.version), 0) + 1
               FROM codex.assistant_versions AS v
               WHERE v.assistant_id = a.assistant_id),
    updated_at = now()
WHERE a.graph_id = 'codex' AND a.user_id = 'system';

INSERT INTO codex.assistant_versions (assistant_id, version, graph_id, config, metadata, name, description)
SELECT assistant_id, version, graph_id, config, metadata, name, description
FROM codex.assistant
WHERE graph_id = 'codex' AND user_id = 'system';

COMMIT;
```

## 人工介入（HITL）

Codex 向人提问时，运行停在 interrupt 上，线程状态为 `interrupted`，`next` 为 `["human"]`。以下请求会变成 interrupt：
- 命令审批
- 文件改动审批
- 权限请求
- `request_user_input` 提问
- MCP elicitation

interrupt 的值就是 Codex 发来的原始请求 `{"method", "params"}`，线程状态里的 `turn` 也存了一份。答复用 Codex 对这个
method 的原生响应格式，作为 resume 的值发回：

```bash
curl -N http://127.0.0.1:2027/threads/<thread>/runs/stream -H 'x-api-key: <API_KEY>' -H 'Content-Type: application/json' \
  -d '{"assistant_id": "codex", "command": {"resume": {"decision": "accept"}}, "stream_mode": ["messages-tuple", "updates"]}'
```

| `method` | resume 的值 |
|---|---|
| `item/commandExecution/requestApproval` | `{"decision": "accept"}`，或 `params.availableDecisions` 里的其他选项，如 `"acceptForSession"`、`{"acceptWithExecpolicyAmendment": {...}}`、`"decline"`、`"cancel"`（`cancel` 还会结束这一轮） |
| `item/fileChange/requestApproval` | `{"decision": "accept" \| "acceptForSession" \| "decline" \| "cancel"}` |
| `item/permissions/requestApproval` | `{"permissions": {...}, "scope": "turn" \| "session"}` |
| `item/tool/requestUserInput` | `{"answers": {"<question id>": {"answers": ["..."]}}}` |
| `mcpServer/elicitation/request` | `{"action": "accept" \| "decline" \| "cancel", "content": {...}}` |

答复不符合该 method 的格式时，会重新询问，并在 interrupt 的值里附上 `errors`。

- **不答复，直接发新消息**：Codex 当前这一轮被中断，新消息开始新的一轮。
- **请求能等多久**：请求保存在 app-server 里，沙箱在多久它就能等多久，即 assistant 最后一次运行之后的 `SANDBOX_IDLE`。
- **答复送不到的情况**：如果沙箱先停止或被替换，比如改了 `base_url`、`api_key`、`allowed_hosts`，或者改变了是否设置
  `codex.model`，答复就送不到了，线程里会明确说明。

只有 `approval_policy` 不是 `never`（比如 `on-request`）时，Codex 才会提问；Codex 0.167 已经不接受 `untrusted`。Codex
自己的沙箱在 OpenSandbox 沙箱里起不来（bwrap 需要 user namespace），所以在 `read-only` 或 `workspace-write` 下，命令会先在
它里面失败，之后模型可能申请在沙箱外运行，也可能不申请。因此命令审批取决于模型；在 `danger-full-access` 下，命令根本不会请求审批。

## 说明

- **沙箱镜像**是 `opensandbox-codex:<version>`，版本就是 `uv.lock` 锁定的 `openai-codex-cli-bin`。由 `docker-compose.yml` 构建；`CODEX_VERSION=<version> docker compose build` 构建其他版本。
- **沙箱的卷** `agentos-codex-<assistant_id>` 存放工作区和 Codex 的 home（含会话）。沙箱在最后一次运行一小时后停止，再启动时挂回原来的卷；线程的会话和工作区在七天没有运行后回收。
- **app-server** 和沙箱同生命周期：Codex 留在后台的命令（比如开发服务器）在运行结束后仍会继续跑。
- **Docker**：`docker-compose.yml` 本机和服务器共用。OpenSandbox server（由它在本机的 Docker 上创建沙箱）和沙箱镜像总会启动；`COMPOSE_PROFILES=server` 时再加上本服务的容器，由 `Dockerfile` 构建，用 host 网络运行。OpenSandbox 的 `allowed_host_paths` 同时列出 Docker Desktop 的磁盘挂载根目录 `/run/desktop/mnt/host` 和 `SANDBOX_MOUNT_PATH`，所以 Windows 路径和 Linux 路径都能用。
