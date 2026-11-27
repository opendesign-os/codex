import asyncio
from typing import Any, Final, TypedDict

from codex.app_server import AsyncAppServerClient
from codex.protocol import types as protocol
from langgraph.types import interrupt
from pydantic import BaseModel, ValidationError

RESPONSES: Final[dict[str, type[BaseModel]]] = {
    "item/commandExecution/requestApproval": protocol.CommandExecutionRequestApprovalResponse,
    "item/fileChange/requestApproval": protocol.FileChangeRequestApprovalResponse,
    "item/permissions/requestApproval": protocol.PermissionsRequestApprovalResponse,
    "item/tool/requestUserInput": protocol.ToolRequestUserInputResponse,
    "mcpServer/elicitation/request": protocol.McpServerElicitationRequestResponse,
}


class Request(TypedDict):
    method: str
    params: dict[str, Any]


class Answer(TypedDict):
    request: Request
    response: dict[str, Any]


class Turn(TypedDict):
    id: str
    prompt: str
    request: Request | None
    answers: list[Answer]


class Inbox:
    def __init__(self, client: AsyncAppServerClient, answers: list[Answer]) -> None:
        self._answers = answers
        self._queue: asyncio.Queue[tuple[Request, asyncio.Future[dict[str, Any]]]] = asyncio.Queue()
        for method in RESPONSES:
            client.on_request(method, self._receive)

    async def next(self, turn: str) -> Request:
        while True:
            request, reply = await self._queue.get()
            if request["params"].get("turnId", turn) != turn:
                continue
            known = [answer["response"] for answer in self._answers if answer["request"] == request]
            if not known:
                return request
            reply.set_result(known[0])

    async def _receive(self, message: BaseModel) -> dict[str, Any]:
        payload = message.model_dump(mode="json", by_alias=True, exclude_none=True)
        reply: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((Request(method=payload["method"], params=payload["params"]), reply))
        return await reply


def ask(turn: Turn) -> Turn:
    request = turn["request"]
    if request is None:
        raise ValueError("The Codex turn has no request waiting for an answer")
    shown: dict[str, Any] = {"method": request["method"], "params": request["params"]}
    while True:
        response = interrupt(shown)
        try:
            RESPONSES[request["method"]].model_validate(response)
        except ValidationError as error:
            errors = error.errors(include_url=False, include_context=False, include_input=False)
            shown = {"method": request["method"], "params": request["params"], "errors": errors}
            continue
        answer = Answer(request=request, response=response)
        return Turn(id=turn["id"], prompt=turn["prompt"], request=None, answers=[*turn["answers"], answer])
