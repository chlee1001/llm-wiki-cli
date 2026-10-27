"""Session and candidate Markdown records."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .config import Wiki, current_project, require_writable
from .redact import redact


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
    return re.sub(r"\s+", " ", _clean(value)).strip()


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
    when = now or _now()
    target, project_name = _target(wiki, project, shared)
    title = _clean_single(title)
    body = _clean(body)
    cleaned_sources = [_clean_single(source) for source in sources if _clean_single(source)]
    status = "candidate-with-evidence" if cleaned_sources else "needs-review"
    candidate_dir = target / "inbox" / "candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    filename = f"candidate-{when.strftime('%Y%m%d-%H%M%S')}-{slug(title)}.md"
    path = candidate_dir / filename
    suffix = 1
    while path.exists():
        path = candidate_dir / f"candidate-{when.strftime('%Y%m%d-%H%M%S')}-{slug(title)}-{suffix}.md"
        suffix += 1
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
    path.write_text(content, encoding="utf-8")
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
) -> tuple[Path, list[Path]]:
    require_writable(wiki)
    selected = current_project(wiki, cwd, project) if project is not None else current_project(wiki, cwd)
    when = _now()
    target, project_name = _target(wiki, selected)
    session_dir = target / "sessions" / when.strftime("%Y") / when.strftime("%m")
    session_dir.mkdir(parents=True, exist_ok=True)
    clean_topic, clean_task, clean_completed = (_clean_single(topic), _clean(task), _clean(completed))
    filename = f"session-{when.strftime('%Y%m%d-%H%M%S')}-{slug(agent, 'agent')}-{slug(clean_topic)}.md"
    path = session_dir / filename
    suffix = 1
    while path.exists():
        path = session_dir / f"session-{when.strftime('%Y%m%d-%H%M%S')}-{slug(agent, 'agent')}-{slug(clean_topic)}-{suffix}.md"
        suffix += 1
    content = f"""---
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

{_clean(handoff or 'None.')}

## Not tested

{_clean(not_tested or 'None.')}
"""
    path.write_text(content, encoding="utf-8")
    candidate_paths = [promote(wiki, candidate, candidate, [], selected, now=when, session=path) for candidate in candidates]
    append_log(wiki, when, "wrap", project_name, clean_topic, path)
    return path, candidate_paths
