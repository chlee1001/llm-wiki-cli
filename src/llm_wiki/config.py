"""Wiki configuration, discovery, and project selection."""
from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

PROJECT_NAME = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")


class WikiError(RuntimeError):
    pass


@dataclass(frozen=True)
class Wiki:
    root: Path
    directory: Path
    mode: str
    name: str
    projects: dict[str, str]
    legacy: bool = False


def validate_project_name(name: str) -> str:
    if not PROJECT_NAME.fullmatch(name):
        raise WikiError("Project name must start with A-Z, a-z, 0-9, underscore, or dash and use only A-Z, a-z, 0-9, dot, underscore, or dash.")
    if name.casefold() == "shared":
        raise WikiError("Project name 'shared' is reserved.")
    return name


def is_wiki_dir_name(directory: Path) -> bool:
    return directory.name.casefold() == "llm-wiki"


def is_legacy_dir(directory: Path) -> bool:
    return is_wiki_dir_name(directory) and os.path.isdir(directory) and not has_config(directory) and (
        os.path.isfile(directory / "SCHEMA.md") or os.path.isfile(directory / "index.md")
    )


def has_config(directory: Path) -> bool:
    # os.path checks return False on permission errors; Path methods raise them before Python 3.14.
    return os.path.isfile(directory / "wiki.toml")


def wiki_dirs(parent: Path) -> list[Path]:
    """Return child directories named llm-wiki in any case, configured and exact names first."""
    try:
        matches = [child for child in parent.iterdir() if is_wiki_dir_name(child) and os.path.isdir(child)]
    except (FileNotFoundError, NotADirectoryError):
        return []
    except PermissionError:
        # Searchable but unlistable: fall back to the exact name, which only needs search permission.
        exact = parent / "llm-wiki"
        return [exact] if os.path.isdir(exact) else []
    return sorted(matches, key=lambda child: (not has_config(child), child.name != "llm-wiki", child.name))


def _load(directory: Path, legacy: bool = False) -> Wiki:
    root = directory.parent
    if legacy:
        return Wiki(root, directory, "legacy", directory.parent.name, {}, True)
    try:
        data = tomllib.loads((directory / "wiki.toml").read_text(encoding="utf-8"))
        wiki = data["wiki"]
        projects = data.get("projects", {})
        mode, name = str(wiki["mode"]), str(wiki["name"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise WikiError(f"Invalid wiki configuration: {directory / 'wiki.toml'}") from exc
    if mode not in {"single", "umbrella"}:
        raise WikiError("wiki.mode must be single or umbrella.")
    if not isinstance(projects, dict):
        raise WikiError("projects must be a table.")
    parsed: dict[str, str] = {}
    for project_name, entry in projects.items():
        validate_project_name(project_name)
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise WikiError(f"Project {project_name} needs a path.")
        parsed[project_name] = entry["path"]
    return Wiki(root, directory, mode, name, parsed)


def discover(start: Path | None = None) -> Wiki:
    """Find the nearest wiki root while walking from start upward."""
    requested = start or Path(os.environ.get("LLM_WIKI_ROOT", Path.cwd()))
    current = requested.expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        for directory in wiki_dirs(candidate):
            if has_config(directory):
                return _load(directory)
            if is_legacy_dir(directory):
                return _load(directory, legacy=True)
    raise WikiError("No llm-wiki found. Run 'llm-wiki init' first.")


def require_writable(wiki: Wiki) -> None:
    if wiki.legacy:
        raise WikiError("Legacy wiki is read-only. Run init in a different location or add wiki.toml manually.")


def current_project(wiki: Wiki, cwd: Path | None = None, override: str | None = None) -> str | None:
    if override is not None:
        if override not in wiki.projects:
            raise WikiError(f"Unknown project: {override}")
        return override
    if wiki.legacy:
        return None
    if wiki.mode == "single":
        return next(iter(wiki.projects), None)
    here = (cwd or Path.cwd()).resolve()
    try:
        relative = here.relative_to(wiki.root).as_posix()
    except ValueError:
        return None
    matches: list[tuple[int, str]] = []
    for name, project_path in wiki.projects.items():
        normalized = project_path.strip("/") or "."
        if normalized == "." or relative == normalized or relative.startswith(normalized + "/"):
            matches.append((0 if normalized == "." else len(normalized), name))
    return max(matches)[1] if matches else None


def write_config(wiki: Wiki) -> None:
    values = [wiki.mode, wiki.name, *wiki.projects.keys(), *wiki.projects.values()]
    if any(any(ord(character) < 32 or ord(character) == 127 for character in value) for value in values):
        raise WikiError("Wiki configuration values must not contain control characters.")
    lines = ["[wiki]", "version = 1", f"mode = {json.dumps(wiki.mode, ensure_ascii=False)}", f"name = {json.dumps(wiki.name, ensure_ascii=False)}", ""]
    for name in sorted(wiki.projects):
        lines.extend((f"[projects.{json.dumps(name, ensure_ascii=False)}]", f"path = {json.dumps(wiki.projects[name], ensure_ascii=False)}", ""))
    content = "\n".join(lines)
    try:
        content.encode("utf-8")  # reject lone surrogates before anything touches the disk
        tomllib.loads(content)
    except (ValueError, TypeError) as exc:
        raise WikiError("Unable to write valid wiki configuration.") from exc
    wiki.directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=wiki.directory, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        os.replace(temporary, wiki.directory / "wiki.toml")
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def add_project(wiki: Wiki, name: str, path: str) -> Wiki:
    require_writable(wiki)
    if wiki.mode == "single":
        raise WikiError("Cannot add a project in single mode.")
    validate_project_name(name)
    if any(name.casefold() == existing.casefold() for existing in wiki.projects):
        raise WikiError(f"Project already exists: {name}")
    supplied = Path(path)
    if supplied.is_absolute():
        raise WikiError("Project path must be relative to the wiki root.")
    root_resolved = wiki.root.resolve()
    try:
        clean_path = (root_resolved / supplied).resolve().relative_to(root_resolved).as_posix()
    except ValueError:
        raise WikiError("Project path must be relative to the wiki root.")
    if clean_path in wiki.projects.values():
        raise WikiError(f"Project path already exists: {clean_path}")
    projects = dict(wiki.projects)
    projects[name] = clean_path
    updated = Wiki(wiki.root, wiki.directory, wiki.mode, wiki.name, projects)
    write_config(updated)
    return updated
