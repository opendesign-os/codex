import uuid
from typing import Annotated, Any, NotRequired, TypedDict

from codex.app_server import (
    AppServerRpcError,
    AppServerThreadForkOptions,
    AppServerThreadResumeOptions,
    AppServerThreadStartOptions,
    AppServerTurnError,
    AsyncAppServerClient,
    AsyncAppServerThread,
    AsyncTurnStream,
)
from codex.protocol import types as protocol
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.config import get_config
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.runtime import Runtime
from opensandbox import Sandbox

from agent import content, human, sandbox, stream, workspace
from agent.context import Context


class Input(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


class State(Input):
    session: NotRequired[str]
    head: NotRequired[str | None]
    turn: NotRequired[human.Turn | None]


async def run(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    context = runtime.context
    configurable = get_config().get("configurable")
    if configurable is None:
        raise RuntimeError("A Codex run needs its thread and assistant in the config")
    thread: str = configurable["thread_id"]
    scope: str = configurable["assistant_id"]
    paused = _paused(state.get("turn"), state["messages"])
    asked, prompt = (paused["prompt"], None) if paused else _prompt(state["messages"])
    answers = paused["answers"] if paused else []
    head = state.get("head")
    transcript: list[AnyMessage] = []
    waiting: human.Turn | None = None
    reason: str | None = None

    lease = await sandbox.acquire(scope, context)
    async with sandbox.connect(lease) as client:
        cwd, existed = await workspace.prepare(lease.sandbox, thread)
        inbox = human.Inbox(client, answers)
        codex = await _open(client, lease.sandbox, state.get("session"), head, cwd, existed, context)
        turn = await _follow(client, codex, paused, asked, prompt, context)
        if turn is None:
            reason = "the turn waiting for an answer is no longer running"
        else:
            try:
                outcome = await stream.relay(client, turn, inbox, transcript, asked)
            except AppServerTurnError as error:
                reason = str(error)
            else:
                if isinstance(outcome, protocol.Turn):
                    reason = stream.failure(outcome)
                    head = outcome.id
                else:
                    waiting = human.Turn(id=turn.turn_id, prompt=asked, request=outcome, answers=answers)

    if reason:
        transcript.append(AIMessage(id=str(uuid.uuid4()), content=f"Codex stopped: {reason}"))
    return {"session": codex.id, "head": head, "messages": transcript, "turn": waiting}


async def answer(state: State) -> dict[str, Any]:
    turn = state.get("turn")
    if turn is None:
        raise RuntimeError("No Codex turn is waiting for an answer")
    return {"turn": human.ask(turn)}


def route(state: State) -> str:
    turn = state.get("turn")
    return "human" if turn and turn["request"] else END


async def _open(
    client: AsyncAppServerClient,
    box: Sandbox,
    session: str | None,
    head: str | None,
    cwd: str,
    existed: bool,
    context: Context,
) -> AsyncAppServerThread:
    found = await _read(client, session) if session else None
    if found is not None and found.cwd.root != cwd:
        await workspace.copy(box, found.cwd.root, cwd)
        return await _fork(client, found, head, cwd, context)
    if found is not None and existed:
        if _diverged(found, head):
            return await _fork(client, found, head, cwd, context)
        options = AppServerThreadResumeOptions(cwd=cwd, config=context.codex)
        return await client.resume_thread(found.id, options)
    options = AppServerThreadStartOptions(cwd=cwd, config=context.codex, dynamic_tools=list(context.tools))
    return await client.start_thread(options)


async def _read(client: AsyncAppServerClient, session: str) -> protocol.Thread | None:
    try:
        return await client.read_thread(session, include_turns=True)
    except AppServerRpcError:
        return None


def _diverged(thread: protocol.Thread, head: str | None) -> bool:
    ids = [turn.id for turn in thread.turns]
    after = thread.turns[ids.index(head) + 1 :] if head in ids else thread.turns
    return head is not None and any(turn.status.root == "completed" for turn in after)


async def _fork(
    client: AsyncAppServerClient, source: protocol.Thread, head: str | None, cwd: str, context: Context
) -> AsyncAppServerThread:
    origin = await client.resume_thread(source.id)
    return await origin.fork(AppServerThreadForkOptions(cwd=cwd, config=context.codex, last_turn_id=head))


async def _follow(
    client: AsyncAppServerClient,
    codex: AsyncAppServerThread,
    paused: human.Turn | None,
    asked: str,
    prompt: HumanMessage | None,
    context: Context,
) -> AsyncTurnStream | None:
    current = stream.active(codex)
    if paused and current and current.id == paused["id"]:
        return stream.attach(client, codex, current)
    if current:
        await stream.abandon(client, codex.id, current)
    if prompt is None:
        return None
    if context.review is not None:
        return await codex.review(target=context.review)
    options = context.turn.model_copy(update={"client_user_message_id": asked})
    return await codex.run(content.inputs(prompt.content), options)


def _paused(turn: human.Turn | None, messages: list[AnyMessage]) -> human.Turn | None:
    asked = [message.id for message in messages if isinstance(message, HumanMessage)]
    return turn if turn and asked and asked[-1] == turn["prompt"] else None


def _prompt(messages: list[AnyMessage]) -> tuple[str, HumanMessage]:
    latest = messages[-1] if messages else None
    if not isinstance(latest, HumanMessage) or latest.id is None:
        raise ValueError("Codex acts on the thread's latest message, which must be a user message")
    return latest.id, latest


graph = (
    StateGraph(State, input_schema=Input, context_schema=Context)
    .add_node("codex", run)  # pyright: ignore[reportUnknownMemberType]
    .add_node("human", answer)  # pyright: ignore[reportUnknownMemberType]
    .add_edge(START, "codex")
    .add_conditional_edges("codex", route, ["human", END])
    .add_edge("human", "codex")
    .compile(name="codex")  # pyright: ignore[reportUnknownMemberType]
)
