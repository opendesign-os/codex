import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast

from codex.app_server import AsyncTurnStream
from codex.protocol import types as protocol
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph.message import push_message


@dataclass(frozen=True)
class Tool:
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
        status = "success" if self.ok else "error"
        return ToolMessage(id=f"{self.id}-output", tool_call_id=self.id, content=self.output, status=status)


async def relay(turn: AsyncTurnStream) -> protocol.Turn:
    started: dict[str, dict[str, Any]] = {}
    completed: protocol.Turn | None = None
    try:
        async for notification in turn:
            match notification:
                case protocol.ItemStartedNotificationModel(params=params):
                    if tool := parse(params.item):
                        started[tool.id] = tool.args
                        push_message(tool.call())
                case protocol.ItemCompletedNotificationModel(params=params):
                    if tool := parse(params.item):
                        if started.pop(tool.id, None) != tool.args:
                            push_message(tool.call())
                        push_message(tool.result())
                    elif isinstance(item := params.item.root, protocol.AgentMessageThreadItem) and item.text:
                        push_message(AIMessage(id=item.id, content=item.text))
                case protocol.TurnCompletedNotificationModel(params=params):
                    completed = params.turn
                case _:
                    pass
    except BaseException:
        with suppress(Exception):
            await asyncio.wait_for(turn.interrupt(), 5)
        raise
    if completed is None:
        raise RuntimeError("Codex's turn ended without completing")
    return completed


def failure(turn: protocol.Turn) -> str | None:
    if turn.status.root == "completed":
        return None
    return turn.error.message if turn.error else turn.status.root


def parse(item: protocol.ThreadItem) -> Tool | None:
    match item.root:
        case protocol.CommandExecutionThreadItem() as run:
            ok = run.status.root == "completed" and run.exitCode == 0
            return Tool(run.id, "shell", {"command": run.command}, run.aggregatedOutput or "", ok)
        case protocol.FileChangeThreadItem() as patch:
            changes = [
                {"path": change.path, "kind": change.kind.model_dump(mode="json"), "diff": change.diff}
                for change in patch.changes
            ]
            done = patch.status.root == "completed"
            return Tool(patch.id, "apply_patch", {"changes": changes}, patch.status.root, done)
        case protocol.McpToolCallThreadItem() as call:
            output = call.error.message if call.error else _text(call.result)
            raw = call.arguments
            args = cast(dict[str, Any], raw) if isinstance(raw, dict) else {"arguments": raw}
            return Tool(call.id, f"mcp__{call.server}__{call.tool}", args, output, call.status.root == "completed")
        case protocol.WebSearchThreadItem() as search:
            return Tool(search.id, "web_search", {"query": search.query}, _dump(search.results or []), True)
        case protocol.ImageViewThreadItem() as view:
            return Tool(view.id, "view_image", {"path": view.path.root}, "", True)
        case protocol.CollabAgentToolCallThreadItem() as call:
            args = {"prompt": call.prompt, "agents": call.receiverThreadIds}
            agents = {
                agent: state.model_dump(mode="json", exclude_none=True) for agent, state in call.agentsStates.items()
            }
            return Tool(call.id, call.tool.root, args, _dump(agents), call.status.root == "completed")
        case _:
            return None


def _text(result: protocol.McpToolCallResult | None) -> str:
    if result is None:
        return ""
    blocks = [cast(dict[str, object], block) for block in result.content if isinstance(block, dict)]
    texts = [str(block["text"]) for block in blocks if block.get("type") == "text"]
    return "\n".join(texts) if texts else _dump(result.structuredContent or result.content)


def _dump(value: object) -> str:
    return json.dumps(value, default=lambda model: model.model_dump(mode="json"))
