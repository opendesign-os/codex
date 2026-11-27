from typing import Any

from codex.protocol import types as protocol

type Block = str | dict[str, Any]


def inputs(content: str | list[Block]) -> list[protocol.UserInput]:
    blocks = [content] if isinstance(content, str) else content
    return [protocol.UserInput.model_validate(_codex(block)) for block in blocks]


def blocks(inputs: list[protocol.UserInput]) -> list[dict[str, Any]]:
    return [_langchain(item.model_dump(mode="json", by_alias=True, exclude_none=True)) for item in inputs]


def _codex(block: Block) -> dict[str, Any]:
    match block:
        case str():
            return {"type": "text", "text": block}
        case {"type": "text", "text": str() as text}:
            return {"type": "text", "text": text}
        case {"type": "image_url", "image_url": {"url": str() as url}}:
            return {"type": "image", "url": url}
        case {"type": "image_url", "image_url": str() as url}:
            return {"type": "image", "url": url}
        case {"type": "image" | "audio" as kind, "url": str() as url}:
            return {"type": kind, "url": url}
        case {"type": "image" | "audio" as kind, "base64": str() as data, "mime_type": str() as mime}:
            return {"type": kind, "url": f"data:{mime};base64,{data}"}
        case {"type": "image" | "audio" as kind, "file_id": str() as file}:
            return {"type": kind, "fileId": file}
        case _:
            return block


def _langchain(item: dict[str, Any]) -> dict[str, Any]:
    match item:
        case {"type": "text", "text": str() as text}:
            return {"type": "text", "text": text}
        case {"type": "image" | "audio" as kind, "fileId": str() as file}:
            return {"type": kind, "file_id": file}
        case _:
            return item
