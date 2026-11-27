# codex

Codex, through `codex app-server` and the [codex-python](https://github.com/gersmann/codex-python) client, as one
LangGraph graph, `codex`, served by [Zorvex](../zorvex) (installed from the checkout at `../zorvex`). Codex's models come
through a LiteLLM gateway.

Each assistant has one sandbox on OpenSandbox, shared by its threads, where `codex app-server` runs. Every thread is a
Codex session of its own under its own workspace subdirectory. A run connects to the app-server over WebSocket and sends
the thread's latest user message to Codex as a turn. It streams the turn into the thread as it runs: Codex's messages,
and its commands, file changes and other tool calls with their results. The next run on the thread resumes its session.

## Run

The server keeps its data in this machine's PostgreSQL, in the `codex` schema of the `postgres` database
(`DATABASE_URI`), and uses Redis database 3 (`REDIS_URI`). Create the schema once:

```sql
CREATE SCHEMA codex;
```

The sandboxes come from the OpenSandbox server in [`../opensandbox`](../opensandbox), with the `opensandbox-codex`
image built there.

```bash
cp .env.example .env                           # fill in the database password, the gateway's settings and the OpenSandbox API key
uv sync
uv run zorvex dev --port 2027 --no-db-check    # the server on 127.0.0.1:2027; --no-db-check: no PostgreSQL container
docker compose up --build                      # or the server in Docker
```

Call `codex` from the console or any LangGraph SDK client:

```python
from langgraph_sdk import get_client

client = get_client(url="http://127.0.0.1:2027")
thread = await client.threads.create()
async for part in client.runs.stream(
    thread["thread_id"],
    "codex",
    input={"messages": [{"role": "user", "content": "Write a hello world in Python and run it"}]},
    stream_mode="messages-tuple",
):
    print(part.event, part.data)
```

## Configuration

- **OpenSandbox**: its server is `OPEN_SANDBOX_DOMAIN` (`host:port`, required) over `OPEN_SANDBOX_PROTOCOL` (`http`, the default, or `https`), with `OPEN_SANDBOX_API_KEY`. The sandboxes' endpoints use the same protocol. The API key also derives each sandbox's app-server token.
  - `SANDBOX_IMAGE` is the sandbox image. It holds the Codex runtime that codex-python pins (`openai-codex-cli-bin` in `uv.lock`), so rebuild it when that changes.
  - `SANDBOX_SHARED_PATH` is the shared skills directory, as the Docker host sees it. With Docker Desktop, `G:\workspace\.codex` is `/run/desktop/mnt/host/g/workspace/.codex`.
  - `SANDBOX_IDLE_SECONDS` (default 3600) is how long a sandbox outlives its last run.
  - `SANDBOX_ALLOWED_HOSTS` (comma-separated) are hosts the sandbox may reach besides the gateway.
- **The gateway**:
  - `LITELLM_BASE_URL` is its OpenAI-compatible base URL, reachable from the sandbox. `LITELLM_API_KEY` is a LiteLLM virtual key.
  - The key stays in this server by default: the sandbox gets a placeholder and OpenSandbox's Credential Vault puts the key on the requests to the gateway. `OPEN_SANDBOX_INSECURE_EGRESS` passes the key straight into the sandbox instead, for local dev where the vault cannot run.
  - Codex calls the gateway's Responses API (`/v1/responses`). With no `CODEX_MODEL` it also reads the model list (`/v1/models`) to discover the models and their context windows and reasoning efforts.
- **`CODEX_MODEL`** pins the model Codex uses, by its `model_name` at the gateway, such as `codex/gpt-5.6-luna`, unless a run's context names another. With it set, Codex uses that model directly and skips the `/models` catalog. Leave it empty only when the gateway answers `/models` in Codex's catalog format (LiteLLM v1.106 and later), which Codex then reads to pick a model; otherwise every run fails with "No models are available".
- **A run's context** (or an assistant's) sets `model`, `effort`, `sandbox` and `approval_mode`.
  - The default `sandbox`, `danger-full-access`, leaves the thread's OpenSandbox sandbox as the boundary. It runs Codex's commands as a non-root user, lets them write only under `/data` and `/tmp`, and lets them reach only the allowed hosts.
  - `workspace-write` and `read-only` need Codex's bubblewrap sandbox, which cannot create namespaces in that hardened sandbox, so commands fail under them.
  - The default `approval_mode`, `deny_all`, refuses Codex's requests to go beyond its sandbox mode; `auto_review` has Codex's reviewer decide them.
- **Instructions and skills**: Codex reads the workspace's own `AGENTS.md`. The shared skills directory is mounted read-only as `/etc/codex`, so its `skills` are Codex's admin skills.

## Notes

- **The sandbox's volume**: it holds the workspaces (`/data/workspace/<thread>`) and Codex's home with its sessions (`/data/home/.codex`). It is the Docker volume `agentos-codex-<assistant_id>`, so a sandbox that expired comes back with both.
- **The app-server**: it runs as long as the sandbox, so it is only started once, not once per run. Commands Codex leaves running, such as a dev server, outlive the run too.
- **Docker**: the server runs on the host's network, so `.env` works unchanged. Docker Desktop needs host networking turned on (Settings > Resources > Network). The image builds from the parent directory, to include `../zorvex`.
