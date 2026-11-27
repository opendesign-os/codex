import asyncio
import json
from collections.abc import AsyncGenerator, Callable, Iterable
from contextlib import aclosing, suppress
from dataclasses import dataclass, field
from typing import Any, cast

from codex.app_server import AppServerRpcError, AsyncAppServerClient, AsyncAppServerThread, AsyncTurnStream
from codex.protocol import types as protocol
from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.messages.ai import UsageMetadata
from langgraph.config import get_stream_writer
from langgraph.graph.message import push_message
from pydantic import BaseModel

from agent import content, human


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


@dataclass
class Usage:
    base: dict[str, int] | None = None
    total: dict[str, int] = field(default_factory=dict[str, int])

    def update(self, usage: protocol.ThreadTokenUsage) -> None:
        total = usage.total.model_dump(exclude_none=True)
        if self.base is None:
            last = usage.last.model_dump(exclude_none=True)
            self.base = {key: value - last.get(key, 0) for key, value in total.items()}
        self.total = total

    def attach(self, transcript: list[AnyMessage]) -> None:
        if self.base is None:
            return
        spent = {key: value - self.base.get(key, 0) for key, value in self.total.items()}
        usage = UsageMetadata(
            input_tokens=spent["inputTokens"],
            output_tokens=spent["outputTokens"],
            total_tokens=spent["totalTokens"],
            input_token_details={"cache_read": spent["cachedInputTokens"]},
            output_token_details={"reasoning": spent["reasoningOutputTokens"]},
        )
        for index in range(len(transcript) - 1, -1, -1):
            if isinstance(message := transcript[index], AIMessage) and not message.tool_calls:
                transcript[index] = message.model_copy(update={"usage_metadata": usage})
                return


def active(thread: AsyncAppServerThread) -> protocol.Turn | None:
    return next((turn for turn in thread.snapshot.turns if turn.status.root == "inProgress"), None)


def attach(client: AsyncAppServerClient, thread: AsyncAppServerThread, turn: protocol.Turn) -> AsyncTurnStream:
    subscription = client.events.subscribe()
    subscription.update_predicate(_scope(thread.id, turn.id))
    return AsyncTurnStream(thread, subscription, turn)


async def abandon(client: AsyncAppServerClient, thread: str, turn: protocol.Turn) -> None:
    await _stop(client, thread, turn.id, {process for item in turn.items for process in _processes(item)})


async def relay(
    client: AsyncAppServerClient,
    turn: AsyncTurnStream,
    inbox: human.Inbox,
    transcript: list[AnyMessage],
    prompt: str | None,
) -> protocol.Turn | human.Request:
    write = get_stream_writer()
    started: dict[str, dict[str, Any]] = {}
    streamed: set[str] = set()
    processes: set[str] = set()
    usage = Usage()
    try:
        async with aclosing(_events(turn, inbox)) as events:
            async for event in events:
                if not isinstance(event, BaseModel):
                    return event
                write(event.model_dump(mode="json", by_alias=True, exclude_none=True))
                match event:
                    case protocol.ItemAgentMessageDeltaNotification(params=params):
                        streamed.add(params.itemId)
                        _emit(AIMessageChunk(id=params.itemId, content=params.delta))
                    case protocol.ItemReasoningSummaryTextDeltaNotification(params=params):
                        streamed.add(params.itemId)
                        _emit(AIMessageChunk(id=params.itemId, content=[_reasoning(params.delta, params.summaryIndex)]))
                    case protocol.ThreadTokenUsageUpdatedNotificationModel(params=params):
                        usage.update(params.tokenUsage)
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
                        elif message := _message(params.item, prompt):
                            if message.id not in streamed:
                                _emit(message)
                            transcript.append(message)
                    case protocol.TurnCompletedNotificationModel(params=params):
                        usage.attach(transcript)
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
            name = f"mcp__{call.server}__{call.tool}"
            output = call.error.message if call.error else _text(call.result)
            return Tool(call.id, name, _args(call.arguments), output, call.status.root == "completed")
        case protocol.DynamicToolCallThreadItem() as call:
            name = f"{call.namespace}__{call.tool}" if call.namespace else call.tool
            items = [item.root for item in call.contentItems or []]
            texts = [
                item.text for item in items if isinstance(item, protocol.InputTextDynamicToolCallOutputContentItem)
            ]
            output = "\n".join(texts) if len(texts) == len(items) else _dump(items)
            return Tool(call.id, name, _args(call.arguments), output, call.success is True)
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


def _message(item: protocol.ThreadItem, prompt: str | None) -> AnyMessage | None:
    match item.root:
        case protocol.AgentMessageThreadItem(id=key, text=str() as text) if text:
            return AIMessage(id=key, content=text)
        case protocol.ReasoningThreadItem(id=key, summary=[*summary]) if summary:
            return AIMessage(id=key, content=[_reasoning("\n\n".join(summary), 0)])
        case protocol.PlanThreadItem(id=key, text=str() as text) if text:
            return AIMessage(id=key, content=text)
        case protocol.ExitedReviewModeThreadItem(id=key, review=str() as review):
            return AIMessage(id=key, content=review)
        case protocol.ImageGenerationThreadItem(root=protocol.ResultThreadItem(id=key, result=str() as result)):
            return AIMessage(id=key, content=[{"type": "image", "base64": result, "mime_type": "image/png"}])
        case protocol.ImageGenerationThreadItem(root=protocol.FileIdThreadItem(id=key, fileId=str() as file)):
            return AIMessage(id=key, content=[{"type": "image", "file_id": file}])
        case protocol.UserMessageThreadItem(id=key, clientId=client, content=inputs) if client != prompt:
            return HumanMessage(id=client or key, content=cast(list[str | dict[str, Any]], content.blocks(inputs)))
        case _:
            return None


def _reasoning(text: str, index: int) -> dict[str, Any]:
    return {"type": "reasoning", "reasoning": text, "index": index}


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


def _args(raw: object) -> dict[str, Any]:
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else {"arguments": raw}


def _text(result: protocol.McpToolCallResult | None) -> str:
    if result is None:
        return ""
    blocks = [cast(dict[str, object], block) for block in result.content if isinstance(block, dict)]
    texts = [str(block["text"]) for block in blocks if block.get("type") == "text"]
    return "\n".join(texts) if texts else _dump(result.structuredContent or result.content)


def _dump(value: object) -> str:
    return json.dumps(value, default=lambda model: model.model_dump(mode="json", exclude_none=True))
