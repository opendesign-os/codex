import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Iterable
from contextlib import aclosing, suppress
from dataclasses import dataclass
from typing import Any, cast

from codex.app_server import AppServerRpcError, AsyncAppServerClient, AsyncAppServerThread, AsyncTurnStream
from codex.protocol import types as protocol
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage, BaseMessage, ToolMessage
from langgraph.graph.message import push_message
from pydantic import BaseModel

from agent import human


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


def active(thread: AsyncAppServerThread) -> protocol.Turn | None:
    return next((turn for turn in thread.snapshot.turns if turn.status.root == "inProgress"), None)


def attach(client: AsyncAppServerClient, thread: AsyncAppServerThread, turn: protocol.Turn) -> AsyncTurnStream:
    subscription = client.events.subscribe()
    subscription.update_predicate(_scope(thread.id, turn.id))
    return AsyncTurnStream(thread, subscription, turn)


async def abandon(client: AsyncAppServerClient, thread: str, turn: protocol.Turn) -> None:
    await _stop(client, thread, turn.id, {process for item in turn.items for process in _processes(item)})


async def relay(
    client: AsyncAppServerClient, turn: AsyncTurnStream, inbox: human.Inbox, transcript: list[AnyMessage]
) -> protocol.Turn | human.Request:
    started: dict[str, dict[str, Any]] = {}
    streamed: set[str] = set()
    processes: set[str] = set()
    try:
        async with aclosing(_events(turn, inbox)) as events:
            async for event in events:
                if not isinstance(event, BaseModel):
                    return event
                match event:
                    case protocol.ItemAgentMessageDeltaNotification(params=params):
                        streamed.add(params.itemId)
                        _emit(AIMessageChunk(id=params.itemId, content=params.delta))
                    case protocol.ItemStartedNotificationModel(params=params):
                        processes.update(_processes(params.item))
                        if tool := parse(params.item):
                            started[tool.id] = tool.args
                            _emit(tool.call())
                    case protocol.ItemCompletedNotificationModel(params=params):
                        processes.update(_processes(params.item))
                        if tool := parse(params.item):
                            if started.pop(tool.id, None) != tool.args:
                                _emit(tool.call())
                            _emit(tool.result())
                            transcript.extend((tool.call(), tool.result()))
                        elif isinstance(item := params.item.root, protocol.AgentMessageThreadItem) and item.text:
                            message = AIMessage(id=item.id, content=item.text)
                            if item.id not in streamed:
                                _emit(message)
                            transcript.append(message)
                    case protocol.TurnCompletedNotificationModel(params=params):
                        return params.turn
                    case _:
                        pass
    except BaseException:
        with suppress(Exception):
            await asyncio.wait_for(_stop(client, turn.thread_id, turn.turn_id, processes), 10)
        raise
    raise RuntimeError("Codex's turn ended without completing")


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


async def _events(turn: AsyncTurnStream, inbox: human.Inbox) -> AsyncGenerator[BaseModel | human.Request]:
    reading = asyncio.ensure_future(anext(turn))
    asking = asyncio.ensure_future(inbox.next(turn.turn_id))
    try:
        while True:
            await asyncio.wait((reading, asking), return_when=asyncio.FIRST_COMPLETED)
            if reading.done():
                try:
                    notification = reading.result()
                except StopAsyncIteration:
                    return
                yield notification
                reading = asyncio.ensure_future(anext(turn))
            else:
                yield asking.result()
                asking = asyncio.ensure_future(inbox.next(turn.turn_id))
    finally:
        reading.cancel()
        asking.cancel()


def _scope(thread: str, turn: str) -> Callable[[BaseModel], bool]:
    def within(notification: BaseModel) -> bool:
        params = getattr(notification, "params", None)
        current = getattr(params, "turnId", None) or getattr(getattr(params, "turn", None), "id", None)
        return getattr(params, "threadId", None) == thread and current == turn

    return within


async def _stop(client: AsyncAppServerClient, thread: str, turn: str, processes: Iterable[str]) -> None:
    await client.rpc.request_typed(
        "turn/interrupt",
        protocol.TurnInterruptParams(threadId=thread, turnId=turn),
        protocol.TurnInterruptResponse,
    )
    for process in processes:
        with suppress(AppServerRpcError):
            await client.rpc.request_typed(
                "thread/backgroundTerminals/terminate",
                protocol.ThreadBackgroundTerminalsTerminateParams(threadId=thread, processId=process),
                protocol.ThreadBackgroundTerminalsTerminateResponse,
            )


def _processes(item: protocol.ThreadItem) -> set[str]:
    match item.root:
        case protocol.CommandExecutionThreadItem(processId=str() as process):
            return {process}
        case _:
            return set()


def _emit(message: BaseMessage) -> None:
    push_message(message, state_key=None)


def _text(result: protocol.McpToolCallResult | None) -> str:
    if result is None:
        return ""
    blocks = [cast(dict[str, object], block) for block in result.content if isinstance(block, dict)]
    texts = [str(block["text"]) for block in blocks if block.get("type") == "text"]
    return "\n".join(texts) if texts else _dump(result.structuredContent or result.content)


def _dump(value: object) -> str:
    return json.dumps(value, default=lambda model: model.model_dump(mode="json"))
