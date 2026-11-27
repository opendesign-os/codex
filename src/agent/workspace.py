import logging
import shlex
from contextlib import suppress
from datetime import UTC, datetime
from typing import Final

from opensandbox import Sandbox
from opensandbox.models.filesystem import DirectoryListEntry, WriteEntry

from agent.settings import settings

logger = logging.getLogger(__name__)

ROOT: Final = "/data/workspace"
MARKERS: Final = f"{ROOT}/.lastrun"


async def prepare(sandbox: Sandbox, thread: str) -> tuple[str, bool]:
    await sandbox.files.create_directories([WriteEntry(path=MARKERS)])
    now = datetime.now(UTC)
    entries = await sandbox.files.list_directory(DirectoryListEntry(path=MARKERS))
    seen = {entry.path.rsplit("/", 1)[-1]: entry.modified_at for entry in entries}
    stale = [name for name, last in seen.items() if now - last > settings.retention]
    if stale:
        with suppress(Exception):
            await sandbox.files.delete_directories([path(name) for name in stale])
        await sandbox.files.delete_files([f"{MARKERS}/{name}" for name in stale])
        logger.info("Reclaimed %d idle Codex session(s) in sandbox %s", len(stale), sandbox.id)
    cwd = path(thread)
    await sandbox.files.create_directories([WriteEntry(path=cwd)])
    await sandbox.files.write_file(f"{MARKERS}/{thread}", now.isoformat())
    return cwd, thread in seen and thread not in stale


async def copy(sandbox: Sandbox, source: str, target: str) -> None:
    origin, destination = shlex.quote(source), shlex.quote(target)
    execution = await sandbox.commands.run(f"test ! -d {origin} || cp -a {origin}/. {destination}/")
    if execution.error is not None:
        raise RuntimeError(f"Copying workspace {source} to {target} failed: {execution.error.value}")


def path(thread: str) -> str:
    return f"{ROOT}/{thread}"
