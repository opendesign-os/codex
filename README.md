# codex

Codex as the LangGraph graph `codex`, served by [Zorvex](../zorvex): it runs `codex app-server` in an
[OpenSandbox](https://github.com/alibaba/OpenSandbox) sandbox and drives it with the
[codex-python](https://github.com/gersmann/codex-python) client. Each assistant has one sandbox, shared by its threads,
and each thread is a Codex session with its own workspace, `/data/workspace/<thread>`. A run sends the thread's latest
user message to Codex as a turn and streams Codex's messages and tool calls into the thread; the next run resumes it.
When Codex asks for an approval or an answer, the run stops at a LangGraph interrupt (see *Human in the loop*).

## Run

Zorvex keeps its data in PostgreSQL, in the schema `codex`:

```sql
CREATE SCHEMA codex;
```

```bash
cp .env.example .env            # the database password, OPEN_SANDBOX_API_KEY and SANDBOX_MOUNT_PATH
docker compose up -d --build    # the OpenSandbox server on 127.0.0.1:8080 and the sandbox image
uv sync
uv run zorvex dev --port 2027 --no-db-check
```

## Environment

All required; `src/codex_agent/settings.py` reads them and the server stops at startup when one is missing.

| Variable | |
|---|---|
| `DATABASE_URI`, `REDIS_URI` | Zorvex's PostgreSQL and Redis. |
| `OPEN_SANDBOX_DOMAIN` | The OpenSandbox server, `127.0.0.1:8080` with `docker-compose.yml`. |
| `OPEN_SANDBOX_API_KEY` | The OpenSandbox server's API key, which `docker-compose.yml` also gives the server. It signs each sandbox's app-server token. |
| `SANDBOX_MOUNT_PATH` | Where the assistants' skills are: each sandbox mounts `<path>/.agent/skills/<assistant_id>` read-only as `/etc/codex/skills`. A Windows path, unquoted or in single quotes, or a path on the Docker host. |
| `SANDBOX_IDLE` | How long a sandbox outlives its last run, as an ISO 8601 duration such as `PT1H`. |
| `SANDBOX_RETENTION` | How long a thread's session and workspace outlive its last run, such as `P7D`. |

## Assistants

An assistant's context (`src/codex_agent/context.py`) has four fields, all required, and no others:

| Field | |
|---|---|
| `base_url` | The LiteLLM gateway's OpenAI-compatible base URL, reachable from the sandbox. |
| `api_key` | The gateway's key. The sandbox only gets a placeholder; OpenSandbox's Credential Vault puts the key on its requests to the gateway. |
| `allowed_hosts` | Hosts the sandbox may reach besides the gateway, `[]` for none; everything else is blocked. |
| `codex` | Codex's own config, as in its `config.toml`, for every thread. Set `sandbox_mode = "danger-full-access"` and `approval_policy = "never"`: the OpenSandbox sandbox is the boundary, and Codex's own sandbox cannot run inside it. Without `model`, Codex picks one from the gateway's `/models` catalog, which takes LiteLLM v1.106 or later. |

`base_url`, `api_key`, `allowed_hosts` and whether `codex.model` is set make up the sandbox, so changing them replaces
it on the next run; its volume keeps the workspaces and sessions. The context is stored as given, the key included.

The system assistant `codex` belongs to the server, so it is configured in the database: its row in `codex.assistant`,
whose `config.configurable` is the context, and a new row in `codex.assistant_versions` for each change.

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

## Human in the loop

When Codex asks a human, the run stops at an interrupt and the thread is `interrupted`, with `next` = `["human"]`.
These requests become interrupts:
- command approvals
- file-change approvals
- permission requests
- `request_user_input` questions
- MCP elicitations

The interrupt's value is Codex's request as it sent it, `{"method", "params"}`; the thread's `turn` holds it too.
The answer is Codex's own response to that method, sent as the resume value:

```bash
curl -N http://127.0.0.1:2027/threads/<thread>/runs/stream -H 'Content-Type: application/json' \
  -d '{"assistant_id": "codex", "command": {"resume": {"decision": "accept"}}, "stream_mode": ["messages-tuple", "updates"]}'
```

| `method` | Resume value |
|---|---|
| `item/commandExecution/requestApproval` | `{"decision": "accept"}`, or one of `params.availableDecisions` such as `"acceptForSession"`, `{"acceptWithExecpolicyAmendment": {...}}`, `"decline"` or `"cancel"` (which also ends the turn) |
| `item/fileChange/requestApproval` | `{"decision": "accept" \| "acceptForSession" \| "decline" \| "cancel"}` |
| `item/permissions/requestApproval` | `{"permissions": {...}, "scope": "turn" \| "session"}` |
| `item/tool/requestUserInput` | `{"answers": {"<question id>": {"answers": ["..."]}}}` |
| `mcpServer/elicitation/request` | `{"action": "accept" \| "decline" \| "cancel", "content": {...}}` |

An answer that does not fit the method is asked again, with `errors` added to the interrupt's value.

- **New message instead of an answer.** Codex's turn is interrupted; the new message starts a new turn.
- **How long a request waits.** It waits inside the app-server as long as the sandbox lives: `SANDBOX_IDLE` after the
  assistant's last run.
- **When the answer can no longer be delivered.** If the sandbox stops or is replaced first, for example because `base_url`, `api_key`, `allowed_hosts` or whether
  `codex.model` is set changed, the answer cannot be delivered and the thread says so.

Codex asks only under an `approval_policy` other than `never`, such as `on-request`. Codex 0.167 rejects `untrusted`.
Codex's own sandbox cannot start inside the OpenSandbox sandbox: bwrap needs user namespaces. So under `read-only` or
`workspace-write`, a command first fails in it, and the model may or may not ask to run it unsandboxed. Command approvals
therefore depend on the model; under `danger-full-access`, commands do not ask at all.

## Notes

- **The sandbox image** is `opensandbox-codex:<version>`, the `openai-codex-cli-bin` that `uv.lock` pins. `docker-compose.yml` builds it; `CODEX_VERSION=<version> docker compose build` builds another.
- **The sandbox's volume**, `agentos-codex-<assistant_id>`, holds the workspaces and Codex's home with the sessions. A sandbox stops an hour after its last run and comes back with its volume; a thread's session and workspace are reclaimed after seven days without a run.
- **The app-server** runs as long as the sandbox: commands Codex leaves running, such as a dev server, outlive the run.
- **Docker**: `docker-compose.yml` runs the OpenSandbox server, which creates the sandboxes on this machine's Docker, and builds the sandbox image. The build reaches Debian and PyPI through Docker Desktop's proxy, `http.docker.internal:3128`, and so through whatever proxy Docker Desktop is set to.
