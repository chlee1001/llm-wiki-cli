"""Session and candidate Markdown records."""
from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .config import Wiki, current_project, require_writable
from .redact import redact
from .sync import require_no_rebase


def slug(value: str, fallback: str = "session") -> str:
    clean, _ = redact(value or "")
    clean = re.sub(r"[^a-z0-9가-힣]+", "-", clean.lower())
    return re.sub(r"-+", "-", clean).strip("-")[:60] or fallback


def _now() -> datetime:
    # Local time with offset, matching the session folders init creates and the log.md stamps.
    return datetime.now().astimezone()


def _clean(value: str) -> str:
    return redact(value)[0].strip()


def _clean_single(value: str) -> str:
    # Join lines and turn whitespace and ASCII control runs into one space before redacting, so a
    # key and its value never meet only after masking, and json.dumps never writes C0 controls as
    # escapes (\u001b, \b) whose letters can join the following text into a new key, scheme or Bearer.
    return _clean(re.sub(r"[\s\x00-\x1f\x7f]+", " ", value))


TMP_DIR = ".llm-wiki-tmp"


def _temporary(directory: Path, content: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=directory, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    return temporary


def create_exclusive(path: Path, content: str, tmp_dir: Path) -> None:
    """Publish content at path only if nothing is there, never exposing a partial file.

    The bytes are written and synced to a private temporary file first, then hard-linked into place;
    ``os.link`` fails with FileExistsError instead of replacing. The guarantee is local-filesystem only.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary(tmp_dir, content)
    try:
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def replace_atomic(path: Path, content: str, tmp_dir: Path) -> None:
    """Swap path's content in one step so readers see the old or the new file, never a mix."""
    temporary = _temporary(tmp_dir, content)
    try:
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _create_unique(directory: Path, stem: str, content: str | Callable[[Path], str], tmp_dir: Path) -> Path:
    """Create ``<stem>.md`` or the first free ``<stem>-N.md``; losing a race moves on to the next name.

    ``content`` may be a function of the chosen path, for records that must link to their own final name.
    """
    suffix = 0
    while True:
        path = directory / (f"{stem}.md" if suffix == 0 else f"{stem}-{suffix}.md")
        try:
            create_exclusive(path, content(path) if callable(content) else content, tmp_dir)
            return path
        except FileExistsError:
            suffix += 1


def _target(wiki: Wiki, project: str | None, shared: bool = False) -> tuple[Path, str]:
    if shared or project is None:
        return wiki.directory / "shared", "shared"
    return wiki.directory / "projects" / project, project


def append_log(wiki: Wiki, when: datetime, action: str, project: str, topic: str, path: Path) -> None:
    """Append a log entry; callers pass an already redacted single-line topic."""
    stamp = when.strftime("%Y-%m-%d %H:%M")
    relative = path.relative_to(wiki.directory).as_posix()
    with (wiki.directory / "log.md").open("a", encoding="utf-8") as handle:
        handle.write(f"\n## [{stamp}] {action} | {project} | {topic}\n\n- `{relative}`\n")


def promote(
    wiki: Wiki,
    title: str,
    body: str,
    sources: list[str],
    project: str | None,
    shared: bool = False,
    now: datetime | None = None,
    session: Path | None = None,
) -> Path:
    """Write a candidate; ``session`` links a wrap candidate to its origin record (provenance, not evidence)."""
    require_writable(wiki)
    require_no_rebase(wiki)
    when = now or _now()
    target, project_name = _target(wiki, project, shared)
    title = _clean_single(title)
    body = _clean(body)
    cleaned_sources = [_clean_single(source) for source in sources if _clean_single(source)]
    status = "candidate-with-evidence" if cleaned_sources else "needs-review"
    candidate_dir = target / "inbox" / "candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    stem = f"candidate-{when.strftime('%Y%m%d-%H%M%S')}-{slug(title)}"
    source_lines = "\n".join(f"- {source}" for source in cleaned_sources) or "- None recorded."
    session_line = ""
    if session is not None:
        session_line = f"session: {json.dumps(session.relative_to(wiki.directory).as_posix(), ensure_ascii=False)}\n"
    content = f"""---
type: candidate
status: {status}
project: {json.dumps(project_name, ensure_ascii=False)}
created: {when.isoformat(timespec='seconds')}
{session_line}sources:
{source_lines}
---

# {title}

{body}
"""
    path = _create_unique(candidate_dir, stem, content, wiki.directory / TMP_DIR)
    append_log(wiki, when, "promote", project_name, title, path)
    return path


def wrap(
    wiki: Wiki,
    topic: str,
    task: str,
    completed: str,
    verification: str | None,
    handoff: str | None,
    not_tested: str | None,
    agent: str,
    project: str | None,
    candidates: list[str],
    cwd: Path,
    handoff_file: bool = False,
) -> tuple[Path, list[Path]]:
    """Record a session; ``handoff_file`` also writes a linked ``handoffs/<id>.md`` when there is handoff text."""
    require_writable(wiki)
    require_no_rebase(wiki)
    selected = current_project(wiki, cwd, project) if project is not None else current_project(wiki, cwd)
    when = _now()
    target, project_name = _target(wiki, selected)
    session_dir = target / "sessions" / when.strftime("%Y") / when.strftime("%m")
    session_dir.mkdir(parents=True, exist_ok=True)
    clean_topic, clean_task, clean_completed = (_clean_single(topic), _clean(task), _clean(completed))
    stem = f"session-{when.strftime('%Y%m%d-%H%M%S')}-{slug(agent, 'agent')}-{slug(clean_topic)}"
    clean_handoff = _clean(handoff or "None.")
    wants_handoff = handoff_file and clean_handoff != "None."
    handoffs = target / "handoffs"

    def render(session_path: Path) -> str:
        link = ""
        if wants_handoff:
            link = f"\nHandoff file: `{(handoffs / session_path.name).relative_to(wiki.directory).as_posix()}`\n"
        return f"""---
type: session
status: draft
project: {json.dumps(project_name, ensure_ascii=False)}
agent: {json.dumps(_clean_single(agent), ensure_ascii=False)}
created: {when.isoformat(timespec='seconds')}
---

# {clean_topic}

## Task

{clean_task}

## Completed

{clean_completed}

## Verification

{_clean(verification or 'Not recorded.')}

## Handoff

{clean_handoff}
{link}
## Not tested

{_clean(not_tested or 'None.')}
"""

    path = _create_unique(session_dir, stem, render, wiki.directory / TMP_DIR)
    if wants_handoff:
        from .handoff import render_handoff

        relative = path.relative_to(wiki.directory).as_posix()
        create_exclusive(handoffs / path.name, render_handoff(project_name, relative, when, clean_handoff, clean_topic), wiki.directory / TMP_DIR)
    candidate_paths = [promote(wiki, candidate, candidate, [], selected, now=when, session=path) for candidate in candidates]
    append_log(wiki, when, "wrap", project_name, clean_topic, path)
    return path, candidate_paths
