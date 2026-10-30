"""Wiki configuration, discovery, and project selection."""
from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field, replace
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
    # Free-form label per project for grouping in search and status; folders stay flat. Missing means "".
    categories: dict[str, str] = field(default_factory=dict)

    def category(self, project: str) -> str:
        return self.categories.get(project, "")


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
    categories: dict[str, str] = {}
    for project_name, entry in projects.items():
        validate_project_name(project_name)
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise WikiError(f"Project {project_name} needs a path.")
        category = entry.get("category", "")
        if not isinstance(category, str):
            raise WikiError(f"Project {project_name} category must be a string.")
        parsed[project_name] = entry["path"]
        categories[project_name] = category
    return Wiki(root, directory, mode, name, parsed, categories=categories)


def load_wiki_at(directory: Path) -> Wiki:
    """Load the wiki whose wiki.toml sits directly in directory, whatever the directory is called."""
    directory = directory.expanduser().resolve()
    if not has_config(directory):
        raise WikiError(f"No wiki.toml in {directory}.")
    return _load(directory)


def discover(start: Path | None = None) -> Wiki:
    """Find the wiki for start (or LLM_WIKI_ROOT, or the cwd).

    An explicit start that holds a wiki.toml directly is loaded as is, so a central clone works under any
    name. Otherwise the nearest llm-wiki directory is searched while walking upward.
    """
    requested = start or Path(os.environ.get("LLM_WIKI_ROOT", Path.cwd()))
    current = requested.expanduser().resolve()
    if current.is_file():
        current = current.parent
    if (start is not None or "LLM_WIKI_ROOT" in os.environ) and has_config(current):
        return _load(current)
    for candidate in (current, *current.parents):
        for directory in wiki_dirs(candidate):
            if has_config(directory):
                return _load(directory)
            if is_legacy_dir(directory):
                return _load(directory, legacy=True)
    raise WikiError("No llm-wiki found. Run 'llm-wiki init' first.")


ROOTS_ENTRY_KEYS = {"wiki", "project"}


def roots_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "llm-wiki" / "roots.json"


def _absolute(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise WikiError(f"roots.json: {label} must be a non-empty string.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise WikiError(f"roots.json: {label} must be an absolute path: {value}")
    return Path(os.path.normpath(path))


def read_roots() -> dict[Path, tuple[Path, str]]:
    """Read the per-machine map from a project directory to (wiki clone, project name).

    The file is ``{"version": 1, "roots": {"/abs/project": {"wiki": "/abs/clone", "project": "name"}}}``.
    A missing file means no mapping; anything malformed is an error rather than a silent fallback.
    """
    path = roots_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise WikiError(f"Cannot read {path}.") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WikiError(f"roots.json is not valid JSON: {path}") from exc
    if not isinstance(data, dict) or set(data) != {"version", "roots"}:
        raise WikiError("roots.json must be an object with exactly version and roots.")
    if data["version"] != 1 or type(data["version"]) is not int:
        raise WikiError("roots.json version must be 1.")
    if not isinstance(data["roots"], dict):
        raise WikiError("roots.json roots must be an object.")
    result: dict[Path, tuple[Path, str]] = {}
    for key, entry in data["roots"].items():
        project_dir = _absolute(key, f"root key {key!r}")
        if not isinstance(entry, dict) or set(entry) != ROOTS_ENTRY_KEYS:
            raise WikiError(f"roots.json entry for {key} must have exactly wiki and project.")
        clone = _absolute(entry["wiki"], f"wiki for {key}")
        if not isinstance(entry["project"], str):
            raise WikiError(f"roots.json project for {key} must be a string.")
        validate_project_name(entry["project"])
        if project_dir in result:
            raise WikiError(f"roots.json has a duplicate root after normalization: {project_dir}")
        result[project_dir] = (clone, entry["project"])
    return result


def write_root(workspace: Path, clone: Path, project: str) -> dict[str, str] | None:
    """Map workspace to (clone, project) in roots.json, keeping every other entry; return the entry it replaced."""
    path = roots_path()
    read_roots()  # refuse to rewrite a malformed file
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {"version": 1, "roots": {}}
    # Reuse a key written another way (~/..., a symlinked path) so the file never maps one directory twice.
    key = next((name for name in data["roots"] if Path(name).expanduser().resolve() == workspace), str(workspace))
    previous = data["roots"].get(key)
    data["roots"][key] = {"wiki": str(clone), "project": validate_project_name(project)}
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".roots-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    except BaseException:
        os.unlink(temporary)
        raise
    return previous


def mapped_workspace(cwd: Path) -> tuple[Path, Path, str] | None:
    """The roots.json entry covering cwd as (workspace directory, wiki clone, project); the longest prefix wins."""
    here = Path(os.path.normpath(cwd.expanduser().resolve()))
    best: tuple[Path, Path, str] | None = None
    for project_dir, (clone, project) in read_roots().items():
        try:
            resolved = Path(os.path.normpath(project_dir.resolve()))
            here.relative_to(resolved)
        except ValueError:
            continue
        if best is None or len(resolved.parts) > len(best[0].parts):
            best = (resolved, clone, project)
    return best


def _mapped(cwd: Path) -> tuple[Path, str] | None:
    found = mapped_workspace(cwd)
    return (found[1], found[2]) if found else None


def sync_directory(cwd: Path, root: Path | None) -> Path | None:
    """The clone a sync would act on, found without reading its wiki.toml: --root, LLM_WIKI_ROOT, or roots.json."""
    if root is not None:
        return root.expanduser()
    if "LLM_WIKI_ROOT" in os.environ:
        return Path(os.environ["LLM_WIKI_ROOT"]).expanduser()
    mapped = _mapped(cwd)
    return mapped[0] if mapped is not None and os.path.isdir(mapped[0]) else None


def discover_for(cwd: Path, root: Path | None) -> tuple[Wiki, str | None]:
    """Resolve the wiki and, when roots.json supplies one, the project for a working directory.

    Order: explicit ``root``, then LLM_WIKI_ROOT, then roots.json, then the upward llm-wiki search.
    """
    if root is not None or "LLM_WIKI_ROOT" in os.environ:
        return discover(root), None
    mapped = _mapped(cwd)
    if mapped is not None:
        # Only the mapping in use must be ready: a clone that is not set up yet must not break the others.
        if not os.path.isdir(mapped[0]):
            raise WikiError(f"roots.json wiki directory does not exist: {mapped[0]}")
        if not has_config(mapped[0]):
            raise WikiError(f"roots.json wiki directory has no wiki.toml: {mapped[0]}; run 'llm-wiki sync --pull-only' or 'init --central' there.")
        wiki = load_wiki_at(mapped[0])
        if mapped[1] not in wiki.projects:
            raise WikiError(f"등록되지 않은 프로젝트: {mapped[1]}; add-project를 먼저 실행")
        return wiki, mapped[1]
    return discover(cwd), None


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
    values = [wiki.mode, wiki.name, *wiki.projects.keys(), *wiki.projects.values(), *(wiki.category(name) for name in wiki.projects)]
    if any(any(ord(character) < 32 or ord(character) == 127 for character in value) for value in values):
        raise WikiError("Wiki configuration values must not contain control characters.")
    lines = ["[wiki]", "version = 1", f"mode = {json.dumps(wiki.mode, ensure_ascii=False)}", f"name = {json.dumps(wiki.name, ensure_ascii=False)}", ""]
    for name in sorted(wiki.projects):
        lines.extend((
            f"[projects.{json.dumps(name, ensure_ascii=False)}]",
            f"path = {json.dumps(wiki.projects[name], ensure_ascii=False)}",
            f"category = {json.dumps(wiki.category(name), ensure_ascii=False)}",
            "",
        ))
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


MAX_CATEGORY = 64


def validate_category(value: object) -> str:
    if not isinstance(value, str):
        raise WikiError("Category must be a string.")
    value = value.strip()
    if len(value) > MAX_CATEGORY:
        raise WikiError(f"Category must be at most {MAX_CATEGORY} characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise WikiError("Category must not contain control characters.")
    return value


def set_category(wiki: Wiki, project: str, category: str) -> Wiki:
    require_writable(wiki)
    if project not in wiki.projects:
        raise WikiError(f"Unknown project: {project}")
    categories = {name: wiki.category(name) for name in wiki.projects}
    categories[project] = validate_category(category)
    updated = replace(wiki, categories=categories)
    write_config(updated)
    return updated


def add_project(wiki: Wiki, name: str, path: str, category: str = "") -> Wiki:
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
    categories = {project: wiki.category(project) for project in wiki.projects}
    categories[name] = validate_category(category)
    updated = replace(wiki, projects=projects, categories=categories)
    write_config(updated)
    return updated
