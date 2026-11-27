import uuid
from typing import Annotated, Any, NotRequired, TypedDict

from codex.app_server import (
    AppServerThreadResumeOptions,
    AppServerThreadStartOptions,
    AppServerTurnError,
    AppServerWebSocketOptions,
    AsyncAppServerClient,
)
from codex.app_server.transports import AsyncWebSocketTransport
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.config import get_config
from langgraph.graph import START, StateGraph, add_messages
from langgraph.runtime import Runtime

from codex_agent import sandbox, stream, workspace
from codex_agent.context import Context


class Input(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


class State(Input):
    session: NotRequired[str]


async def run(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    context = runtime.context
    configurable = get_config().get("configurable")
    if configurable is None:
        raise RuntimeError("A Codex run needs its thread and assistant in the config")
    thread: str = configurable["thread_id"]
    scope: str = configurable["assistant_id"]
    text = prompt(state["messages"])

    lease = await sandbox.acquire(scope, context)
    try:
        cwd, existed = await workspace.prepare(lease.sandbox, thread)
        client = AsyncAppServerClient(
            AsyncWebSocketTransport(lease.url, AppServerWebSocketOptions(bearer_token=lease.token))
        )
        async with sandbox.keepalive(lease.sandbox), client:
            session = state.get("session")
            if session and existed:
                codex = await client.resume_thread(session, AppServerThreadResumeOptions(cwd=cwd, config=context.codex))
            else:
                codex = await client.start_thread(AppServerThreadStartOptions(cwd=cwd, config=context.codex))
            try:
                reason = stream.failure(await stream.relay(await codex.run(text)))
            except AppServerTurnError as error:
                reason = str(error)
    finally:
        await lease.sandbox.close()

    update: dict[str, Any] = {"session": codex.id}
    if reason:
        update["messages"] = [AIMessage(id=str(uuid.uuid4()), content=f"Codex stopped: {reason}")]
    return update


def prompt(messages: list[AnyMessage]) -> str:
    if not messages or not isinstance(messages[-1], HumanMessage):
        raise ValueError("Codex acts on the thread's latest message, which must be a user message")
    return messages[-1].text


graph = (
    StateGraph(State, input_schema=Input, context_schema=Context)
    .add_node("codex", run)  # pyright: ignore[reportUnknownMemberType]
    .add_edge(START, "codex")
    .compile(name="codex")  # pyright: ignore[reportUnknownMemberType]
)
