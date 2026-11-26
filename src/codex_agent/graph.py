"""Codex, run through codex app-server, as a LangGraph graph, its models through a LiteLLM gateway.

Each thread has its own OpenSandbox sandbox, where `codex app-server` runs, with one workspace and one Codex thread.
A run connects to the app-server over WebSocket, sends the thread's latest user message to Codex as a turn, streams
the turn into the thread as it runs, and the next run on the thread resumes the same Codex thread.
"""

import asyncio
import json
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from codex.app_server import (
    AppServerThreadResumeOptions,
    AppServerThreadStartOptions,
    AppServerTurnError,
    AppServerTurnOptions,
    AppServerWebSocketOptions,
    AsyncAppServerClient,
    AsyncTurnStream,
)
from codex.app_server.transports import AsyncWebSocketTransport
from codex.protocol import types as protocol
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.config import get_config
from langgraph.graph import START, StateGraph, add_messages
from langgraph.graph.message import push_message
from langgraph.runtime import Runtime

from codex_agent import sandbox

# The approval policy and reviewer of each approval mode, as the OpenAI Codex SDK maps them.
_APPROVALS: dict[str, tuple[str, str | None]] = {
    "deny_all": ("never", None),
    "auto_review": ("on-request", "auto_review"),
}


class InputState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


class State(InputState):
    session_id: NotRequired[str]
    """The Codex thread the next run resumes."""


@dataclass(kw_only=True)
class Context:
    """Settings for a run, from the assistant's context or the run's."""

    model: str | None = None
    """A model the LiteLLM gateway serves, by its `model_name` there. Unset, CODEX_MODEL, else Codex's default."""

    effort: str | None = None
    """Reasoning effort, such as `low`, `medium` or `high`. Unset, the model's default."""

    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "danger-full-access"
    """Codex's own sandbox for its commands. The default, `danger-full-access`, leaves the thread's OpenSandbox sandbox
    as the boundary: a non-root user, writes only under /data and /tmp, and outbound traffic only to allowed hosts.
    `workspace-write` and `read-only` need bubblewrap, which cannot create namespaces in that hardened sandbox."""

    approval_mode: Literal["deny_all", "auto_review"] = "deny_all"
    """`deny_all` refuses Codex's requests to go beyond its sandbox mode; `auto_review` has Codex's reviewer decide them."""


@dataclass(frozen=True)
class _Tool:
    """A turn item that is a tool call: Codex's command, file change, MCP tool call, and the like."""

    id: str
    name: str
    args: dict[str, Any]
    output: str
    ok: bool

    def call(self) -> AIMessage:
        return AIMessage(
            id=self.id,
            content="",
            tool_calls=[{"id": self.id, "name": self.name, "args": self.args, "type": "tool_call"}],
        )

    def result(self) -> ToolMessage:
        return ToolMessage(
            id=f"{self.id}-output", tool_call_id=self.id, content=self.output, status="success" if self.ok else "error"
        )


async def run_codex(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    """Send the latest user message to Codex in the thread's sandbox and stream its turn into the thread."""
    context = runtime.context or Context()
    # Every run has a thread: a stateless run gets one of its own.
    configurable = get_config().get("configurable", {})
    prompt = _prompt(state["messages"])
    approval_policy, approvals_reviewer = _APPROVALS[context.approval_mode]
    settings: dict[str, Any] = {
        "cwd": sandbox.WORKSPACE,
        "model": context.model,
        "sandbox": context.sandbox,
        "approval_policy": approval_policy,
        "approvals_reviewer": approvals_reviewer,
    }

    box = await sandbox.acquire(configurable["thread_id"], assistant_id=configurable.get("assistant_id"))
    try:
        url, token = await sandbox.app_server(box)
        client = AsyncAppServerClient(AsyncWebSocketTransport(url, AppServerWebSocketOptions(bearer_token=token)))
        async with sandbox.kept_alive(box), client:
            if session_id := state.get("session_id"):
                thread = await client.resume_thread(session_id, AppServerThreadResumeOptions(**settings))
            else:
                thread = await client.start_thread(AppServerThreadStartOptions(**settings))
            turn_options = AppServerTurnOptions.model_validate({"effort": context.effort})
            try:
                stopped = _stopped(await _stream(await thread.run(prompt, turn_options)))
            except AppServerTurnError as error:
                stopped = str(error)
    finally:
        await box.close()

    update: dict[str, Any] = {"session_id": thread.id}
    if stopped:
        update["messages"] = [AIMessage(id=str(uuid.uuid4()), content=f"Codex stopped: {stopped}")]
    return update


def _prompt(messages: list[AnyMessage]) -> str:
    if not messages or not isinstance(messages[-1], HumanMessage):
        raise ValueError("Codex acts on the thread's latest message, which must be a user message")
    return messages[-1].text


def _stopped(turn: protocol.Turn) -> str | None:
    """Why the turn stopped short of completing; None when it completed."""
    if turn.status.root == "completed":
        return None
    return turn.error.message if turn.error else turn.status.root


async def _stream(stream: AsyncTurnStream) -> protocol.Turn:
    """Push the turn into the thread as it runs: tool calls as they start, the rest as it completes."""
    started: dict[str, dict[str, Any]] = {}
    completed: protocol.Turn | None = None
    try:
        async for notification in stream:
            match notification:
                case protocol.ItemStartedNotificationModel(params=params):
                    if tool := _tool(params.item):
                        started[tool.id] = tool.args
                        push_message(tool.call())
                case protocol.ItemCompletedNotificationModel(params=params):
                    if tool := _tool(params.item):
                        if started.pop(tool.id, None) != tool.args:
                            push_message(tool.call())
                        push_message(tool.result())
                    elif isinstance(item := params.item.root, protocol.AgentMessageThreadItem) and item.text:
                        push_message(AIMessage(id=item.id, content=item.text))
                case protocol.TurnCompletedNotificationModel(params=params):
                    completed = params.turn
    except BaseException:
        # Cancelled or failed mid-turn: stop Codex's turn, which goes on in the sandbox otherwise.
        with suppress(Exception):
            await asyncio.wait_for(stream.interrupt(), 5)
        raise
    if completed is None:
        raise RuntimeError("Codex's turn ended without completing")
    return completed


def _tool(item: protocol.ThreadItem) -> _Tool | None:
    """The tool call a turn item stands for; None for Codex's messages, its reasoning and the user's input."""
    match item.root:
        case protocol.CommandExecutionThreadItem() as run:
            ok = run.status.root == "completed" and run.exitCode == 0
            return _Tool(run.id, "shell", {"command": run.command}, run.aggregatedOutput or "", ok)
        case protocol.FileChangeThreadItem() as patch:
            changes = [
                {"path": change.path, "kind": change.kind.model_dump(mode="json"), "diff": change.diff}
                for change in patch.changes
            ]
            return _Tool(
                patch.id, "apply_patch", {"changes": changes}, patch.status.root, patch.status.root == "completed"
            )
        case protocol.McpToolCallThreadItem() as call:
            output = call.error.message if call.error else _mcp_output(call.result)
            args = call.arguments if isinstance(call.arguments, dict) else {"arguments": call.arguments}
            return _Tool(call.id, f"mcp__{call.server}__{call.tool}", args, output, call.status.root == "completed")
        case protocol.WebSearchThreadItem() as search:
            return _Tool(search.id, "web_search", {"query": search.query}, _json(search.results or []), True)
        case protocol.ImageViewThreadItem() as view:
            return _Tool(view.id, "view_image", {"path": view.path.root}, "", True)
        case protocol.CollabAgentToolCallThreadItem() as call:
            args = {"prompt": call.prompt, "agents": call.receiverThreadIds}
            agents = {
                agent: state.model_dump(mode="json", exclude_none=True) for agent, state in call.agentsStates.items()
            }
            return _Tool(call.id, call.tool.root, args, _json(agents), call.status.root == "completed")
    return None


def _mcp_output(result: protocol.McpToolCallResult | None) -> str:
    if result is None:
        return ""
    texts = [block["text"] for block in result.content if isinstance(block, dict) and block.get("type") == "text"]
    return "\n".join(texts) if texts else _json(result.structuredContent or result.content)


def _json(value: Any) -> str:
    return json.dumps(value, default=lambda model: model.model_dump(mode="json"))


graph = (
    # ty does not yet match TypedDict states against LangGraph's StateLike bound; pyright does.
    StateGraph(State, input_schema=InputState, context_schema=Context)  # ty: ignore[invalid-argument-type]
    .add_node("codex", run_codex)
    .add_edge(START, "codex")
    .compile(name="codex")
)
