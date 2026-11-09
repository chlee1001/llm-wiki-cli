"""A size-bounded brief: handoffs, in-progress work, candidates, and recent sessions.

The brief is read-only. Its text never exceeds the byte limit: whole items are dropped oldest first and
each section reports how many it left out, so nothing is cut in the middle of an item or a character.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Wiki, WikiError
from .curate import PENDING, candidate_scopes
from .handoff import scan_handoffs
from .history import state as history_state
from .history import handoff_threads
from .search import record_metadata
from .sync import SyncError, owns_repository, repository_git_dir

DEFAULT_LIMIT = 16 * 1024
MIN_LIMIT = 2048
EXCERPT_CHARS = 600
STALE_FETCH_HOURS = 24
WARNINGS_SHOWN = 5
# Section order is also the priority for leftover budget. First-pass shares are fractions of the limit:
# handoffs 8/16, history 2/16, candidates 3/16, sessions 3/16; whatever is left goes to sections that still omit items.
SECTIONS = (("handoffs", "Open handoffs", 8), ("history", "In-progress work", 2), ("candidates", "Candidates awaiting review", 3), ("sessions", "Recent sessions", 3))
EMPTY = {"handoffs": "_No open handoffs._", "history": "_No in-progress work._", "candidates": "_No candidates awaiting review._", "sessions": "_No sessions._"}


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _body(text: str) -> str:
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end != -1:
            return text[end + 5 :]
    return text


def _title(body: str, fallback: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def _section_text(body: str, heading: str) -> str:
    lines = body.splitlines()
    try:
        start = lines.index(f"## {heading}") + 1
    except ValueError:
        return ""
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start:end]).strip()


def _excerpt(text: str) -> str:
    text = text.strip()
    return text if len(text) <= EXCERPT_CHARS else text[:EXCERPT_CHARS].rstrip() + "…"


def _created(path: Path, meta: dict[str, str | None]) -> tuple[float, str]:
    value = meta.get("created")
    if value:
        try:
            return datetime.fromisoformat(value).timestamp(), path.name
        except ValueError:
            pass
    return 0.0, path.name


def _item(kind: str, path: Path, wiki: Wiki, text: str, excerpt: str, item_id: str | None = None) -> dict[str, Any]:
    meta = record_metadata(path)
    body = _body(text)
    title = _title(body, path.stem)
    relative = path.relative_to(wiki.directory).as_posix()
    details = [f"`{relative}`"] + ([meta["created"]] if meta.get("created") else []) + ([meta["status"]] if kind == "candidates" and meta.get("status") else [])
    content = _excerpt(excerpt if excerpt else body)
    block = f"### {title}\n\n{' · '.join(details)}\n\n{content}\n\n" if content else f"### {title}\n\n{' · '.join(details)}\n\n"
    item = {"title": title, "path": relative, "created": meta.get("created"), "text": block, "_key": _created(path, meta)}
    if kind == "candidates":
        item["status"] = meta.get("status")
    if item_id is not None:
        item["id"] = item_id
    return item


def _handoff_excerpt(body: str) -> str:
    return "\n".join(line for line in body.splitlines() if not line.startswith(("# ", "Session: ")))


def _collect(wiki: Wiki, project: str | None) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], list[str], int]:
    """Gather the brief's items; files that cannot be read as UTF-8 are listed, never silently dropped."""
    found: dict[str, list[dict[str, Any]]] = {"handoffs": [], "history": [], "candidates": [], "sessions": []}
    duplicates: list[dict[str, Any]] = []
    unreadable: list[str] = []
    superseded = 0
    threads = handoff_threads(wiki, project)

    def add(kind: str, path: Path, text: str | None, excerpt: Callable[[str], str], item_id: str | None = None) -> None:
        if text is None:
            unreadable.append(path.relative_to(wiki.directory).as_posix())
            return
        found[kind].append(_item(kind, path, wiki, text, excerpt(_body(text)), item_id))

    for item in history_state(wiki, project=project)["open_work"]:
        title = " ".join(item["title"].split())
        link = f'{item["path"]}#{item["record_id"]}'
        found["history"].append({**item, "text": f'- {title} · {item["since"]}→{item["last_week"]} · {item["work_status"]} · {item["evidence"]} · {link}\n\n', "_key": (item["last_week"], item["record_id"])})
    for scope in candidate_scopes(wiki, project):
        state = scan_handoffs(scope / "handoffs", wiki.directory, threads)
        superseded += len(state["superseded"])
        for entry in state["open"]:
            path = wiki.directory / entry["path"]
            count = len(found["handoffs"])
            add("handoffs", path, _read(path), _handoff_excerpt, entry["id"])
            if len(found["handoffs"]) > count and "history" in entry:
                history = entry["history"]
                found["handoffs"][-1]["text"] += f'history: {history["record_id"]} ({history["work_status"]})\n\n'
        duplicates += [{"id": entry["id"], "agents": entry["agents"]} for entry in state["taken"] if entry["duplicate_take"]]
        candidates = scope / "inbox" / "candidates"
        if candidates.is_dir():
            for path in candidates.glob("*.md"):
                text = _read(path)
                if text is None or record_metadata(path).get("status") in PENDING:
                    add("candidates", path, text, lambda body: body.split("\n", 2)[-1])
        sessions = scope / "sessions"
        if sessions.is_dir():
            for path in sessions.rglob("*.md"):
                add("sessions", path, _read(path), lambda body: _section_text(body, "Completed"))
    for kind in found:
        found[kind].sort(key=lambda item: item["_key"], reverse=True)
    return found, duplicates, sorted(unreadable), superseded


def _freshness(wiki: Wiki) -> tuple[str | None, list[str]]:
    try:
        if not owns_repository(wiki.directory):
            return None, []
        git_dir = repository_git_dir(wiki.directory)
    except SyncError as exc:
        if exc.payload.get("reason") == "no_git":
            return None, []  # a wiki without git has nothing to fetch, as for owns_repository
        return None, [f"could not check fetch freshness ({exc.code}): {exc}"]
    fetch_head = git_dir / "FETCH_HEAD" if git_dir else None
    if fetch_head is None or not fetch_head.exists():
        return None, ["never fetched from the remote; run 'llm-wiki sync --pull-only' to see other machines' records"]
    mtime = fetch_head.stat().st_mtime
    stamp = datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
    hours = (time.time() - mtime) / 3600
    if hours > STALE_FETCH_HOURS:
        return stamp, [f"last fetch {int(hours)} hours ago ({stamp}); run 'llm-wiki sync --pull-only' to refresh"]
    return stamp, []


def _folded(lines: list[str], what: str) -> list[str]:
    """Keep the first few warnings of one kind so they cannot crowd out the brief; JSON has all."""
    if len(lines) <= WARNINGS_SHOWN:
        return lines
    return lines[:WARNINGS_SHOWN] + [f"{len(lines) - WARNINGS_SHOWN} more {what}; see --json"]


def _render(wiki: Wiki, project: str | None, warnings: list[str], shown: dict[str, list[dict[str, Any]]], omitted: dict[str, int], superseded: int) -> str:
    parts = [f"# LLM Wiki brief: {wiki.name} / {project or 'all projects'}\n\n"]
    parts += [f"> warning: {warning}\n" for warning in warnings]
    if warnings:
        parts.append("\n")
    for kind, heading, _ in SECTIONS:
        extra = f", {superseded} superseded" if kind == "handoffs" else ""
        parts.append(f"## {heading} ({len(shown[kind])} shown, {omitted[kind]} omitted{extra})\n\n")
        parts += [item["text"] for item in shown[kind]] or [EMPTY[kind] + "\n\n"]
    return "".join(parts).rstrip("\n") + "\n"


def brief(wiki: Wiki, project: str | None, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    if limit < MIN_LIMIT:
        raise WikiError(f"--max-bytes must be at least {MIN_LIMIT}.")
    found, duplicates, unreadable, superseded = _collect(wiki, project)
    last_fetch, warnings = _freshness(wiki)
    warnings += _folded([f"unreadable record skipped (not valid UTF-8 or not readable): {path}" for path in unreadable], "unreadable records")
    warnings += _folded([f"duplicate take: {item['id']} by {', '.join(item['agents'])}" for item in duplicates], "duplicate takes")
    # Upper bound of everything except the items: counts at their widest and every empty-section line.
    fixed = len(_render(wiki, project, warnings, {k: [] for k in found}, {k: len(v) * 10 + 9 for k, v in found.items()}, superseded).encode("utf-8"))
    available = limit - fixed
    if available <= 0:
        raise WikiError("--max-bytes is too small for the brief headings.")
    shown: dict[str, list[dict[str, Any]]] = {kind: [] for kind in found}
    used = 0

    def fill(kind: str, allowance: int) -> int:
        spent = 0
        queue = found[kind][len(shown[kind]) :]
        for item in queue:
            size = len(item["text"].encode("utf-8"))
            if spent + size > allowance:
                break
            shown[kind].append(item)
            spent += size
        return spent

    for kind, _, share in SECTIONS:
        used += fill(kind, min(limit * share // 16, available - used))
    for kind, _, _ in SECTIONS:
        used += fill(kind, available - used)
    omitted = {kind: len(found[kind]) - len(shown[kind]) for kind in found}
    text = _render(wiki, project, warnings, shown, omitted, superseded)
    sections = {kind: {"items": [{k: v for k, v in item.items() if k != "_key"} for item in shown[kind]], "omitted": omitted[kind]} for kind in found}
    sections["handoffs"]["superseded"] = superseded
    return {"text": text, "bytes": len(text.encode("utf-8")), "limit": limit, "sections": sections, "duplicate_takes": duplicates, "last_fetch": last_fetch, "warnings": warnings, "unreadable": unreadable}
