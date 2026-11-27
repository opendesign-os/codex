# 部署 codex

在 Linux 服务器上用 Docker 一键部署：拉代码、填 `.env`、执行一条命令。各环境变量和 assistant 字段的含义见
[README.md](README.md)。

服务器和本机共用同一个 `docker-compose.yml`。`.env` 里设了 `COMPOSE_PROFILES=server`，它就启动三样东西：

- **codex**：本服务（Zorvex），镜像由仓库里的 `Dockerfile` 构建，监听 2027 端口，所有请求都要带 API Key；
- **opensandbox-server**：OpenSandbox server，由它在本机 Docker 上为每个 assistant 创建沙箱；
- **opensandbox-codex**：沙箱镜像，装着 `uv.lock` 锁定版本的 Codex。

PostgreSQL 和 Redis 用服务器上已有的。codex 用 host 网络运行，所以 `.env` 里写的数据库地址（包括 `localhost`）在容器里照样
可用，OpenSandbox 返回的 `127.0.0.1` 沙箱地址也能直接访问。

## 准备

- Linux，Docker Engine 25 或更新版本，Compose 插件 2.23.1 或更新版本。
- 已有的 PostgreSQL，以及 Redis 5.0 或更新版本。表由 codex 启动时自动迁移创建，但 schema 本身要事先建好：

  ```sql
  CREATE SCHEMA codex;
  ```

- 网络：构建时要能访问 Docker Hub、ghcr.io、Debian 和 PyPI；运行时要能访问 LiteLLM 网关。

## 部署

```bash
git clone https://github.com/opendesign-os/codex.git
cd codex
cp .env.example .env
chmod 600 .env
vi .env
docker compose up -d --build
```

`.env` 在服务器上这样填：

| 变量 | 填什么 |
|---|---|
| `DATABASE_URI` | 已有的 PostgreSQL，保留末尾的 `?options=-csearch_path%3Dcodex` |
| `REDIS_URI` | 已有的 Redis |
| `API_KEY` | 客户端调用时带在 `x-api-key` 请求头里的密钥：`openssl rand -hex 32` 的输出 |
| `OPEN_SANDBOX_DOMAIN` | `127.0.0.1:8080` |
| `OPEN_SANDBOX_API_KEY` | 另一个 `openssl rand -hex 32` 的输出 |
| `SANDBOX_MOUNT_PATH` | 存放 skills 的宿主机目录，如 `/srv/codex` |
| `SANDBOX_IDLE`、`SANDBOX_RETENTION` | 如 `PT1H`、`P7D` |
| `BUILD_PROXY` | 服务器能直接访问外网就留空，否则填代理地址 |
| `COMPOSE_PROFILES` | `server` |

`docker compose up -d --build` 构建 codex 镜像和沙箱镜像，启动 OpenSandbox server，等它健康后再启动 codex。codex 每次启动
都会检查数据库，有新的迁移就执行；首次启动还会创建系统 assistant `codex`。

## 配置 assistant

按 README「Assistant 配置」一节，在数据库里给系统 assistant 填上 `base_url`、`api_key` 等；`base_url` 要填服务器能访问到
的地址。然后按它的 id 建 skills 目录：

```bash
curl -s http://127.0.0.1:2027/assistants/search -H 'x-api-key: <API_KEY>' -H 'Content-Type: application/json' -d '{"graph_id": "codex"}'
sudo mkdir -p /srv/codex/.agent/skills/<assistant_id>
```

## 验证

```bash
curl -s http://127.0.0.1:2027/ok
curl -s http://127.0.0.1:2027/runs/wait -H 'x-api-key: <API_KEY>' -H 'Content-Type: application/json' \
  -d '{"assistant_id": "codex", "input": {"messages": [{"role": "user", "content": "Reply with only OK."}]}}'
```

第一次运行要创建 assistant 的沙箱，耗时较长；返回的最后一条消息应该是 `OK`。

已验证和未验证的部分：
- 已验证：合并后的 `docker-compose.yml` 在本机 Docker Desktop 上跑过，OpenSandbox 和 skills 挂载正常；codex 镜像也在容器里
  验证过启动、自动迁移和 API Key 鉴权。
- 未验证：Linux 上用 host 网络运行的 codex 容器还没实际跑过，首次部署时以这一步的结果为准。

## 安全

- **API**：除 `/ok` 和 `/info` 外，所有请求都要带正确的 `x-api-key`，否则返回 401。2027 端口直接对外；走公网时建议在前面
  加一层 HTTPS 反向代理，否则 key 以明文传输。
- **沙箱端口**：Docker 把沙箱端口（40000–49151）发布在所有网卡上，而且它的 NAT 规则会绕过主机的 INPUT 规则（ufw 也拦不
  住）。在 `DOCKER-USER` 链里拦掉，把 `eth0` 换成公网网卡，并把规则持久化：

  ```bash
  sudo iptables -I DOCKER-USER -i eth0 -p tcp -m conntrack --ctorigdstport 40000:49151 --ctdir ORIGINAL -j DROP
  ```

  也可以用云厂商的安全组，只放行 SSH 和 2027（或反向代理的 443），效果相同。
- **密钥**：`.env` 里有 `API_KEY` 和 OpenSandbox 的 key，权限设为 600。数据库里以明文保存每个 assistant 的网关 key，
  数据库的访问权限和备份同样要严格控制。

## 运维

| 事项 | 命令 |
|---|---|
| 更新 | `git pull && docker compose up -d --build` |
| 日志 | `docker compose logs -f codex`、`docker compose logs -f opensandbox-server`，单个沙箱用 `docker logs <container>` |
| 重启 | `docker compose restart codex` |
| 备份 | `pg_dump --schema=codex`，以及每个 assistant 的卷 `agentos-codex-<assistant_id>`（工作区和 Codex 会话） |

- 重启 codex 时，正在进行的运行有 30 秒收尾，没完成的放回队列，重启后重新开始；等待人工答复的请求保存在沙箱里，不受影响。
- Codex 升级（`uv.lock` 里的 `openai-codex-cli-bin` 变了）后，`docker-compose.yml` 里 `CODEX_VERSION` 的默认值要跟着改，
  沙箱镜像的标签必须和它一致。旧镜像的沙箱会在下次运行时被替换：卷里的工作区和会话保留，但其中等待人工答复的请求会丢失。

## 排障

| 现象 | 处理 |
|---|---|
| `required variable OPEN_SANDBOX_API_KEY is missing a value` | 在仓库根目录（`.env` 所在目录）运行 `docker compose`。 |
| `docker compose up` 没有启动 codex | `.env` 里设 `COMPOSE_PROFILES=server`。 |
| codex 启动失败，日志里有 `no schema has been selected to create in` | 数据库里还没建 `codex` schema。 |
| codex 启动失败，日志里有 `API_KEY` 的校验错误 | `API_KEY` 至少 32 位。 |
| 请求返回 401 `Missing or invalid x-api-key header` | 请求头带上 `x-api-key: <API_KEY>`。 |
| codex 连不上数据库或 Redis | codex 用 host 网络，`DATABASE_URI` 和 `REDIS_URI` 要写宿主机能访问的地址。 |
| 运行报 `SandboxApiException`，OpenSandbox 日志里有 `images/<id>/json` 404 | 同一标签的沙箱镜像重建后，旧镜像上的沙箱查不到镜像。用 `docker rm -f` 删掉旧的 `sandbox-<id>` 和 `sandbox-egress-<id>` 容器，下次运行会重建。 |
| 沙箱启动时 `skills` 挂载失败 | `SANDBOX_MOUNT_PATH` 下 assistant 的 skills 目录要先建好。 |
| 沙箱一直不就绪，egress sidecar 的就绪检查失败 | 检查 `docker-compose.yml` 里 opensandbox-server 的 `extra_hosts` 和 `host_ip`。 |
| 发往 `127.0.0.1` 的请求走了代理 | 环境里设了 `HTTP_PROXY` 或 `HTTPS_PROXY` 时，加上 `NO_PROXY=127.0.0.1,localhost`。 |
| 运行因连接网关出错而中止 | 服务器要能访问 `base_url` 的主机；Codex 还要访问的其他主机要加到 `allowed_hosts`。 |
| 命令报 `bwrap: No permissions to create a new namespace` | Codex 自己的沙箱在容器里起不来：按 README 设 `sandbox_mode = "danger-full-access"`。 |
