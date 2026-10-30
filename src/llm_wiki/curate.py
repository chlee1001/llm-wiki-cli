"""Candidate curation: review material, and the owner's accept or reject decision.

A decision rewrites only the candidate's front matter: the ``status`` line changes and ``decided_by``,
``decided_at`` and ``reason`` are appended. ``decided_by`` and ``reason`` are single-line redacted JSON
strings; ``decided_at`` is a bare ISO timestamp like ``created``. Every byte after the
closing delimiter stays as it was, and the file stays in ``inbox/candidates/``. A decided candidate is
never decided again.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Wiki, WikiError, require_writable
from .records import TMP_DIR, _clean_single, _now, append_log, replace_atomic
from .search import record_metadata
from .sync import require_no_rebase

PENDING = ("needs-review", "candidate-with-evidence")
DECIDED = ("accepted", "rejected")
DECISION_FIELDS = ("decided_by", "decided_at", "reason")
NO_SOURCES = "None recorded."


def candidate_scopes(wiki: Wiki, project: str | None) -> list[Path]:
    """The shared area plus one project, or plus every project when none is given."""
    if project is not None and project not in wiki.projects:
        raise WikiError(f"Unknown project: {project}")
    names = [project] if project is not None else sorted(wiki.projects)
    return [wiki.directory / "shared", *(wiki.directory / "projects" / name for name in names)]


def _locate(wiki: Wiki, raw: str) -> tuple[Path, str]:
    """Resolve an absolute or wiki-relative path to a real candidate file inside this wiki."""
    refused = WikiError(f"Not a candidate file in this wiki: {raw}")
    given = Path(raw).expanduser()
    real_root = wiki.directory.resolve()
    for base in (Path(os.path.normpath(wiki.directory.absolute())), real_root):
        lexical = Path(os.path.normpath(given if given.is_absolute() else base / given))
        try:
            relative = lexical.relative_to(base)
            break
        except ValueError:
            continue
    else:
        raise refused
    parts = relative.parts
    shared = len(parts) == 4 and parts[0] == "shared" and parts[1:3] == ("inbox", "candidates")
    project = len(parts) == 5 and parts[0] == "projects" and parts[1] in wiki.projects and parts[2:4] == ("inbox", "candidates")
    if not (shared or project) or not parts[-1].endswith(".md"):
        raise refused
    path = real_root / relative
    if path.is_symlink() or not path.is_file() or path.resolve() != path:
        raise refused
    return path, relative.as_posix()


def _value(text: str, where: str) -> Any:
    if not text.startswith('"'):
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise WikiError(f"Malformed front matter value in {where}") from exc


def _parse(data: bytes, where: str) -> tuple[dict[str, Any], list[str], str]:
    """Split a candidate into its front matter fields, the raw front matter lines and the body text."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WikiError(f"Candidate is not valid UTF-8: {where}") from exc
    if not text.startswith("---\n"):
        raise WikiError(f"Candidate has no front matter: {where}")
    end = text.find("\n---\n", 3)
    if end == -1:
        raise WikiError(f"Candidate front matter is not closed: {where}")
    head, body = text[4 : end + 1], text[end + 5 :]
    if "\r" in head:
        raise WikiError(f"Candidate front matter must use LF line endings: {where}")
    lines = head.splitlines()
    fields: dict[str, Any] = {}
    listing: str | None = None
    for line in lines:
        if line.startswith("- "):
            if listing is None:
                raise WikiError(f"Malformed front matter in {where}")
            fields[listing].append(line[2:].strip())
            continue
        key, separator, value = line.partition(":")
        if not separator or not key or key != key.strip() or " " in key:
            raise WikiError(f"Malformed front matter in {where}")
        if key in fields:
            raise WikiError(f"Duplicate front matter field {key} in {where}")
        value = value.strip()
        listing = key if not value else None
        fields[key] = [] if not value else _value(value, where)
    if fields.get("type") != "candidate":
        raise WikiError(f"Not a candidate: {where}")
    status = fields.get("status")
    if status not in PENDING + DECIDED:
        raise WikiError(f"Unsupported candidate status {status!r}: {where}")
    if status in PENDING and any(field in fields for field in DECISION_FIELDS):
        raise WikiError(f"Undecided candidate already carries decision fields: {where}")
    return fields, lines, body


def _title(body: str, fallback: str) -> str:
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or fallback
    return fallback


def _session(wiki: Wiki, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value:
        return None
    root = wiki.directory.resolve()
    target = Path(os.path.normpath(root / value))
    inside = target.is_relative_to(root) and target.resolve().is_relative_to(root)
    return {"path": value, "exists": inside and target.is_file()}


def review(wiki: Wiki, raw: str) -> dict[str, Any]:
    """Everything needed to judge one candidate; reads only."""
    path, relative = _locate(wiki, raw)
    fields, _, body = _parse(path.read_bytes(), relative)
    sources = fields.get("sources", [])
    if not isinstance(sources, list):
        sources = [sources]
    return {
        "path": relative,
        "status": fields["status"],
        "decided": fields["status"] in DECIDED,
        "project": fields.get("project"),
        "created": fields.get("created"),
        "title": _title(body, path.stem),
        "sources": [source for source in sources if source != NO_SOURCES],
        "session": _session(wiki, fields.get("session")),
        "front_matter": fields,
        "body": body,
    }


def pending(wiki: Wiki, project: str | None) -> list[dict[str, Any]]:
    """Candidates still awaiting a decision, in the shared area and the given (or every) project."""
    found = []
    for scope in candidate_scopes(wiki, project):
        directory = scope / "inbox" / "candidates"
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            if path.is_symlink():
                continue
            meta = record_metadata(path)
            if meta["type"] == "candidate" and meta["status"] in PENDING:
                found.append({"path": path.relative_to(wiki.directory).as_posix(), "status": meta["status"], "created": meta["created"]})
    return found


def decide(wiki: Wiki, raw: str, decision: str, decided_by: str, reason: str, now: datetime | None = None) -> dict[str, Any]:
    """Record an accept or reject decision in the candidate's front matter."""
    if decision not in DECIDED:
        raise WikiError(f"Unknown decision: {decision}")
    require_writable(wiki)
    require_no_rebase(wiki)
    if decision == "accepted" and decided_by != "owner":
        raise WikiError("accept requires --decided-by owner.")
    path, relative = _locate(wiki, raw)
    fields, lines, body = _parse(path.read_bytes(), relative)
    previous = fields["status"]
    if previous in DECIDED:
        raise WikiError(f"이미 결정된 후보입니다: {relative} ({previous})")
    who, why = _clean_single(decided_by), _clean_single(reason)
    if not who:
        raise WikiError("A decision needs a non-empty --decided-by.")
    if not why:
        raise WikiError("A decision needs a non-empty --reason.")
    when = now or _now()
    stamp = when.isoformat(timespec="seconds")
    head = [f"status: {decision}" if line.partition(":")[0] == "status" else line for line in lines]
    head += [f"decided_by: {json.dumps(who, ensure_ascii=False)}", f"decided_at: {stamp}", f"reason: {json.dumps(why, ensure_ascii=False)}"]
    replace_atomic(path, "---\n" + "".join(f"{line}\n" for line in head) + "---\n" + body, wiki.directory / TMP_DIR)
    project = fields.get("project") if isinstance(fields.get("project"), str) else "shared"
    append_log(wiki, when, "accept" if decision == "accepted" else "reject", _clean_single(project), _clean_single(_title(body, path.stem)), path)
    return {"path": relative, "status": decision, "previous": previous, "decided_by": who, "decided_at": stamp, "reason": why}
