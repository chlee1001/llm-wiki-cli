"""Markdown search with an optional QMD backend."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .config import Wiki, WikiError, current_project
from .qmd import Runner, collection_name, collections, qmd_search, run_command


def search_scope(wiki: Wiki, cwd: Path, project: str | None, all_projects: bool, category: str | None = None) -> tuple[list[Path], list[str]]:
    if wiki.legacy:
        return [wiki.directory], []
    if category is not None:
        members = [name for name in sorted(wiki.projects) if wiki.category(name) == category]
        if not members:
            raise WikiError(f"No project has category: {category}")
        return [wiki.directory / "projects" / name for name in members], members
    selected = current_project(wiki, cwd, project)
    if all_projects:
        return [wiki.directory], ["shared", *wiki.projects]
    paths = [wiki.directory / "shared"]
    names = ["shared"]
    if selected is not None:
        paths.append(wiki.directory / "projects" / selected)
        names.append(selected)
    return paths, names


def text_search(query: str, directories: list[Path], root: Path, limit: int) -> list[dict[str, Any]]:
    terms = [term.casefold() for term in query.split()]
    if not terms:
        raise WikiError("Search query must not be empty.")
    results: list[dict[str, Any]] = []
    for directory in directories:
        if not directory.exists():
            continue
        for path in directory.rglob("*.md"):
            if "handoffs" in path.relative_to(directory).parts:
                continue  # the session's own Handoff section is indexed; the file would duplicate it
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            folded = content.casefold()
            if not all(term in folded for term in terms):
                continue
            score = sum(folded.count(term) for term in terms)
            snippets = [line.strip() for line in content.splitlines() if any(term in line.casefold() for term in terms)][:2]
            results.append({"path": path.relative_to(root).as_posix(), "score": score, "snippets": snippets})
    return sorted(results, key=lambda item: (-item["score"], item["path"]))[:limit]


RECORD_FIELDS = ("type", "status", "created")


def record_metadata(path: Path) -> dict[str, str | None]:
    """Read type/status/created from a note's front matter; missing or unreadable values stay None."""
    metadata: dict[str, str | None] = dict.fromkeys(RECORD_FIELDS)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return metadata
    if not lines or lines[0].strip() != "---":
        return metadata
    found = {}
    for line in lines[1:]:
        if line.strip() == "---":
            metadata.update(found)
            break
        key, separator, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not separator or key not in metadata or not value:
            continue
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        found[key] = value
    return metadata


def search(
    wiki: Wiki,
    query: str,
    cwd: Path,
    project: str | None = None,
    all_projects: bool = False,
    engine: str = "auto",
    limit: int = 10,
    runner: Runner | None = None,
    qmd_available: bool | None = None,
    category: str | None = None,
) -> tuple[list[dict[str, Any]], str, str | None]:
    """Search notes; each result also carries the note's recorded type, status and created time.

    ``category`` limits the search to the projects with that category (shared notes are not included).
    """
    results, used, note = _search(wiki, query, cwd, project, all_projects, engine, limit, runner, qmd_available, category)
    for result in results:
        result.update(record_metadata(wiki.root / result["path"]))
    return results, used, note


def _search(
    wiki: Wiki,
    query: str,
    cwd: Path,
    project: str | None,
    all_projects: bool,
    engine: str,
    limit: int,
    runner: Runner | None,
    qmd_available: bool | None,
    category: str | None = None,
) -> tuple[list[dict[str, Any]], str, str | None]:
    directories, names = search_scope(wiki, cwd, project, all_projects, category)
    if engine not in {"auto", "qmd", "text"}:
        raise WikiError("Search engine must be auto, qmd, or text.")
    available = shutil.which("qmd") is not None if qmd_available is None else qmd_available
    if engine == "text":
        return text_search(query, directories, wiki.root, limit), "text", None
    if not available:
        if engine == "qmd":
            raise WikiError("qmd is not installed.")
        return text_search(query, directories, wiki.root, limit), "text", None
    if wiki.legacy:
        if engine == "qmd":
            raise WikiError("QMD search is unavailable for a legacy wiki.")
        return text_search(query, directories, wiki.root, limit), "text", None
    try:
        qmd_runner = runner or run_command
        existing = collections(qmd_runner)
        if all(collection_name(wiki, name) in existing for name in names):
            return qmd_search(query, names, wiki, limit, qmd_runner), "qmd", None
        if engine == "qmd":
            raise WikiError("Required qmd collections do not exist. Run qmd-sync first.")
        return text_search(query, directories, wiki.root, limit), "text", "QMD collections are missing; used text search."
    except WikiError as exc:
        if engine == "qmd":
            raise
        return text_search(query, directories, wiki.root, limit), "text", f"QMD failed; used text search: {exc}"
