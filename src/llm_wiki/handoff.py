"""Handoff files: what the next agent should pick up, and who took it.

A handoff is ``projects/<name>/handoffs/<id>.md`` (``shared/handoffs/`` for work outside any project) where ``<id>`` is the session file's stem. It is never
edited. Taking one adds ``<id>.taken.<agent>.<host>``; a handoff is open while no such file exists. Two
machines can still take the same handoff before they sync, so that case is reported, not prevented.
"""
from __future__ import annotations

import json
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Wiki, WikiError, require_writable
from .history import handoff_threads
from .records import TMP_DIR, _clean_single, create_exclusive, slug
from .sync import require_no_rebase

ID_PATTERN = re.compile(r"^[A-Za-z0-9가-힣][A-Za-z0-9가-힣._-]*$")


def handoffs_dir(wiki: Wiki, project: str | None) -> Path:
    """A project's handoffs directory, or the shared one when project is None (where wrap puts them then)."""
    if project is None:
        return wiki.directory / "shared" / "handoffs"
    if project not in wiki.projects:
        raise WikiError(f"Unknown project: {project}")
    return wiki.directory / "projects" / project / "handoffs"


def host_slug() -> str:
    return slug(socket.gethostname().split(".")[0], "host")


def render_handoff(project: str, session_relative: str, created: datetime, text: str, title: str) -> str:
    return f"""---
type: handoff
project: {json.dumps(project, ensure_ascii=False)}
session: {json.dumps(session_relative, ensure_ascii=False)}
created: {created.isoformat(timespec='seconds')}
---

# {title}

Session: `{session_relative}`

{text}
"""


def _valid_id(handoff_id: str) -> str:
    if not ID_PATTERN.fullmatch(handoff_id) or ".taken." in handoff_id or handoff_id.endswith(".md"):
        raise WikiError(f"Invalid handoff id: {handoff_id!r}")
    return handoff_id


def take(wiki: Wiki, project: str | None, handoff_id: str, agent: str, now: datetime | None = None) -> Path:
    """Record that ``agent`` took the handoff by adding a new file; nothing existing is modified.

    A project's own handoff wins; otherwise the shared one with that id, which brief also lists.
    """
    require_writable(wiki)
    require_no_rebase(wiki)
    handoff_id = _valid_id(handoff_id)
    directory = handoffs_dir(wiki, project)
    source = directory / f"{handoff_id}.md"
    if not source.is_file() and project is not None:
        directory = handoffs_dir(wiki, None)
        source = directory / f"{handoff_id}.md"
    if not source.is_file():
        raise WikiError(f"Unknown handoff: {handoff_id}")
    agent_slug = slug(agent, "agent")
    taken = directory / f"{handoff_id}.taken.{agent_slug}.{host_slug()}"
    when = now or datetime.now().astimezone()
    body = f"taken: {when.isoformat(timespec='seconds')}\nagent: {json.dumps(_clean_single(agent), ensure_ascii=False)}\n"
    others = sorted(directory.glob(f"{handoff_id}.taken.{agent_slug}.*"))
    if others:
        raise WikiError(f"이미 가져간 인수인계입니다: {handoff_id} ({agent_slug})")
    try:
        create_exclusive(taken, body, wiki.directory / TMP_DIR)
    except FileExistsError:
        raise WikiError(f"이미 가져간 인수인계입니다: {handoff_id} ({agent_slug})") from None
    return taken


def _session(source: Path) -> str | None:
    """The ``session:`` value render_handoff wrote; None when the file cannot be read that way."""
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in lines[1:] if lines[:1] == ["---"] else []:
        if line == "---":
            break
        if line.startswith("session: "):
            try:
                value = json.loads(line[len("session: ") :])
            except json.JSONDecodeError:
                return None
            return value if isinstance(value, str) else None
    return None


def scan_handoffs(directory: Path, wiki_directory: Path, threads: dict[str, dict] | None = None) -> dict[str, list[dict[str, Any]]]:
    """List open, taken and superseded handoffs in one handoffs directory, newest first.

    ``duplicate_take`` marks a handoff that more than one agent took, which only sync can reveal.
    A handoff whose session a history record follows with a later wrap is superseded; ``threads``
    comes from ``history.handoff_threads``.
    """
    open_items: list[dict[str, Any]] = []
    taken_items: list[dict[str, Any]] = []
    superseded_items: list[dict[str, Any]] = []
    if not directory.is_dir():
        return {"open": open_items, "taken": taken_items, "superseded": superseded_items}
    for source in sorted(directory.glob("*.md"), reverse=True):
        handoff_id = source.stem
        agents = [marker.name[len(handoff_id) + len(".taken.") :].rsplit(".", 1)[0] for marker in sorted(directory.glob(f"{handoff_id}.taken.*"))]
        entry: dict[str, Any] = {"id": handoff_id, "path": source.relative_to(wiki_directory).as_posix()}
        if agents:
            entry["agents"] = agents
            entry["duplicate_take"] = len(agents) > 1
            taken_items.append(entry)
        else:
            session = _session(source) if threads else None
            # history validated each wrap path; the stem check keeps a copied or edited handoff from borrowing another thread.
            thread = threads.get(session) if session and Path(session).name == source.name else None
            if thread and thread["by"] is not None:
                superseded_items.append({**entry, "record_id": thread["record_id"], "by": thread["by"]})
            else:
                if thread:
                    entry["history"] = {"record_id": thread["record_id"], "work_status": thread["work_status"]}
                open_items.append(entry)
    return {"open": open_items, "taken": taken_items, "superseded": superseded_items}


def handoff_state(wiki: Wiki, project: str) -> dict[str, list[dict[str, Any]]]:
    """List open, taken and superseded handoffs of a project, newest first."""
    return scan_handoffs(handoffs_dir(wiki, project), wiki.directory, handoff_threads(wiki, project))
