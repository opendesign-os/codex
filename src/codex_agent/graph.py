import uuid
from typing import Annotated, Any, NotRequired, TypedDict

from codex.app_server import (
    AppServerInitializeOptions,
    AppServerThreadResumeOptions,
    AppServerThreadStartOptions,
    AppServerTurnError,
    AppServerWebSocketOptions,
    AsyncAppServerClient,
    AsyncAppServerThread,
    AsyncTurnStream,
)
from codex.app_server.transports import AsyncWebSocketTransport
from codex.protocol import types as protocol
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.runtime import Runtime

from codex_agent import human, sandbox, stream, workspace
from codex_agent.context import Context


class Input(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


class State(Input):
    session: NotRequired[str]
    turn: NotRequired[human.Turn | None]


async def run(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    context = runtime.context
    configurable = get_config().get("configurable")
    if configurable is None:
        raise RuntimeError("A Codex run needs its thread and assistant in the config")
    thread: str = configurable["thread_id"]
    scope: str = configurable["assistant_id"]
    paused = _paused(state.get("turn"), state["messages"])
    asked, text = (paused["prompt"], None) if paused else _prompt(state["messages"])
    answers = paused["answers"] if paused else []
    transcript: list[AnyMessage] = []
    waiting: human.Turn | None = None
    reason: str | None = None

    lease = await sandbox.acquire(scope, context)
    try:
        cwd, existed = await workspace.prepare(lease.sandbox, thread)
        client = AsyncAppServerClient(
            AsyncWebSocketTransport(lease.url, AppServerWebSocketOptions(bearer_token=lease.token)),
            AppServerInitializeOptions(experimental_api=True),
        )
        async with sandbox.keepalive(lease.sandbox), client:
            inbox = human.Inbox(client, answers)
            session = state.get("session")
            if session and existed:
                codex = await client.resume_thread(session, AppServerThreadResumeOptions(cwd=cwd, config=context.codex))
            else:
                codex = await client.start_thread(AppServerThreadStartOptions(cwd=cwd, config=context.codex))
            turn = await _follow(client, codex, paused, text)
            if turn is None:
                reason = "the turn waiting for an answer is no longer running"
            else:
                try:
                    outcome = await stream.relay(client, turn, inbox, transcript)
                except AppServerTurnError as error:
                    reason = str(error)
                else:
                    if isinstance(outcome, protocol.Turn):
                        reason = stream.failure(outcome)
                    else:
                        waiting = human.Turn(id=turn.turn_id, prompt=asked, request=outcome, answers=answers)
    finally:
        await lease.sandbox.close()

    if reason:
        transcript.append(AIMessage(id=str(uuid.uuid4()), content=f"Codex stopped: {reason}"))
    return {"session": codex.id, "messages": transcript, "turn": waiting}


async def answer(state: State) -> dict[str, Any]:
    turn = state.get("turn")
    if turn is None:
        raise RuntimeError("No Codex turn is waiting for an answer")
    return {"turn": human.ask(turn)}


def route(state: State) -> str:
    turn = state.get("turn")
    return "human" if turn and turn["request"] else END


async def _follow(
    client: AsyncAppServerClient, codex: AsyncAppServerThread, paused: human.Turn | None, text: str | None
) -> AsyncTurnStream | None:
    current = stream.active(codex)
    if paused and current and current.id == paused["id"]:
        return stream.attach(client, codex, current)
    if current:
        await stream.abandon(client, codex.id, current)
    return await codex.run(text) if text is not None else None


def _paused(turn: human.Turn | None, messages: list[AnyMessage]) -> human.Turn | None:
    asked = [message.id for message in messages if isinstance(message, HumanMessage)]
    return turn if turn and asked and asked[-1] == turn["prompt"] else None


def _prompt(messages: list[AnyMessage]) -> tuple[str, str]:
    latest = messages[-1] if messages else None
    if not isinstance(latest, HumanMessage) or latest.id is None:
        raise ValueError("Codex acts on the thread's latest message, which must be a user message")
    return latest.id, latest.text


graph = (
    StateGraph(State, input_schema=Input, context_schema=Context)
    .add_node("codex", run)  # pyright: ignore[reportUnknownMemberType]
    .add_node("human", answer)  # pyright: ignore[reportUnknownMemberType]
    .add_edge(START, "codex")
    .add_conditional_edges("codex", route, ["human", END])
    .add_edge("human", "codex")
    .compile(name="codex")  # pyright: ignore[reportUnknownMemberType]
)
