"""Creation and maintenance of the on-disk wiki layout."""
from __future__ import annotations

import os
import shlex
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from .config import Wiki, WikiError, add_project, discover, has_config, set_category, validate_category, write_root, is_legacy_dir, is_wiki_dir_name, load_wiki_at, validate_project_name, wiki_dirs, write_config
from .templates import GJC_FRONT_MATTER, SKILL_FRONT_MATTER, SKILL_NAME, agents_block, gjc_rule, gjc_skill, index, legacy_wiki_readme, wiki_readme

BEGIN = "<!-- llm-wiki:begin -->"
END = "<!-- llm-wiki:end -->"


def _write_missing(path: Path, content: str, created: list[str], existing: list[str]) -> None:
    if path.exists():
        existing.append(str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    created.append(str(path))


GITATTRIBUTES_LINE = "log.md merge=union"
GITIGNORE_LINE = ".llm-wiki-tmp/"


def _ensure_line(path: Path, line: str, created: list[str], existing: list[str], updated: list[str] | None = None) -> None:
    """Make sure path contains line once, keeping every other line as it is."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(line + "\n", encoding="utf-8")
        created.append(str(path))
        return
    existing.append(str(path))
    content = path.read_text(encoding="utf-8")
    if line in content.splitlines():
        return
    path.write_text(content + ("" if content.endswith("\n") or not content else "\n") + line + "\n", encoding="utf-8")
    if updated is not None:
        updated.append(str(path))


def _marked_span(content: str, path: Path, begin: str = BEGIN, end: str = END) -> tuple[int, int] | None:
    count = content.count(begin)
    start = content.find(begin)
    if count != content.count(end) or count > 1 or (count == 1 and content.find(end) < start + len(begin)):
        raise WikiError(f"Unpaired llm-wiki markers in {path}.")
    return None if count == 0 else (start, content.find(end) + len(end))


def _splice(content: str, span: tuple[int, int] | None, block: str) -> str:
    if span is None:
        return content + ("" if content.endswith("\n") else "\n") + "\n" + block + "\n"
    return content[: span[0]] + block + content[span[1]:]


class Managed(NamedTuple):
    group: str
    path: Path
    block: str
    prefix: str = ""
    append: bool = False
    legacy: str | None = None

    @property
    def create(self) -> str:
        return self.prefix + self.block + "\n"


def managed_files(wiki: Wiki, workspace: Path | None = None) -> list[Managed]:
    """Files whose llm-wiki marker block init keeps in sync with this CLI's templates.

    A workspace mapped in roots.json gets only its agent guidance, pointing at the mapped wiki; the central
    clone itself carries no managed files.
    """
    base, d = (wiki.root, wiki.directory.name) if workspace is None else (workspace, None)
    files = [
        Managed("agents", base / "AGENTS.md", agents_block(d).rstrip("\n"), append=True),
        Managed("gjc", base / ".gjc" / "rules" / "llm-wiki.md", gjc_rule(d).rstrip("\n"), GJC_FRONT_MATTER),
        Managed("gjc", base / ".gjc" / "skills" / SKILL_NAME / "SKILL.md", gjc_skill(d).rstrip("\n"), SKILL_FRONT_MATTER),
    ]
    if workspace is None:
        files.append(Managed("wiki", wiki.directory / "README.md", wiki_readme(wiki.name).rstrip("\n"), legacy=legacy_wiki_readme(wiki.name)))
    return files


def _inspect(spec: Managed) -> tuple[str, str | None, tuple[int, int] | None]:
    try:
        raw = spec.path.read_bytes()
    except FileNotFoundError:
        return "missing", None, None
    except OSError:
        return "unreadable", None, None
    if spec.legacy is not None and raw == spec.legacy.encode("utf-8"):
        return "legacy", None, None
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "unreadable", None, None
    try:
        span = _marked_span(content, spec.path)
    except WikiError:
        return "invalid", content, None
    if span is None:
        return "unmarked", content, None
    return ("current" if content[span[0] : span[1]] == spec.block else "stale"), content, span


def managed_status(wiki: Wiki, workspace: Path | None = None) -> list[dict[str, str]]:
    if wiki.legacy:
        return []
    base = wiki.root if workspace is None else workspace
    entries = []
    for spec in managed_files(wiki, workspace):
        state, content, span = _inspect(spec)
        entry = {"path": spec.path.relative_to(base).as_posix(), "state": state}
        if spec.prefix and content is not None and span is not None:
            entry["front_matter"] = "current" if content[: span[0]] == spec.prefix else "differs"
        entries.append(entry)
    return entries


def refresh_command(wiki: Wiki, entries: list[dict[str, str]], workspace: Path | None = None) -> str | None:
    states = [entry["state"] for entry in entries]
    if not any(state in {"stale", "legacy"} for state in states) or any(state in {"invalid", "unreadable"} for state in states):
        return None
    absent = {"missing", "unmarked"}
    groups = [spec.group for spec in managed_files(wiki, workspace)]
    flags = []
    if all(state in absent for group, state in zip(groups, states) if group == "agents"):
        flags.append("--no-agents")
    if all(state in absent for group, state in zip(groups, states) if group == "gjc"):
        flags.append("--no-gjc")
    return shlex.join(["llm-wiki", "init", "--path", str(wiki.root if workspace is None else workspace), *flags])


_FIXES = {
    "agents": "fix it by hand, or skip it with --no-agents",
    "gjc": "fix it by hand, or skip it with --no-gjc",
    "wiki": "fix or delete it, then re-run init",
}


def project_paths(wiki: Wiki, name: str) -> list[Path]:
    base = wiki.directory / "projects" / name
    now = datetime.now()
    return [base / "knowledge", base / "sessions" / now.strftime("%Y") / now.strftime("%m"), base / "inbox" / "candidates"]


def ensure_project_layout(wiki: Wiki, name: str, created: list[str] | None = None) -> None:
    for path in project_paths(wiki, name):
        if not path.exists():
            path.mkdir(parents=True)
            if created is not None:
                created.append(str(path))


def _inspect_writable(specs: list[Managed], groups: set[str], base: Path) -> list[tuple[Managed, str, str | None, tuple[int, int] | None]]:
    inspected = [(spec, *_inspect(spec)) for spec in specs if spec.group in groups]
    blocked = [f"{spec.path.relative_to(base).as_posix()} ({state}); {_FIXES[spec.group]}" for spec, state, _, _ in inspected if state in {"invalid", "unreadable"}]
    if blocked:
        raise WikiError("Cannot update " + "; ".join(blocked) + ".")
    return inspected


def _write_managed(inspected: list[tuple[Managed, str, str | None, tuple[int, int] | None]], created: list[str], existing: list[str], updated: list[str]) -> None:
    for spec, state, content, span in inspected:
        if state == "missing":
            spec.path.parent.mkdir(parents=True, exist_ok=True)
            spec.path.write_bytes(spec.create.encode("utf-8"))
            created.append(str(spec.path))
            continue
        existing.append(str(spec.path))
        if state == "legacy":
            spec.path.write_bytes(spec.create.encode("utf-8"))
            updated.append(str(spec.path))
        elif state == "stale" or (state == "unmarked" and spec.append):
            spec.path.write_bytes(_splice(content, span, spec.block).encode("utf-8"))
            updated.append(str(spec.path))


def _guidance(wiki: Wiki, workspace: Path, include_agents: bool, include_gjc: bool) -> list[tuple[Managed, str, str | None, tuple[int, int] | None]]:
    groups = ({"agents"} if include_agents else set()) | ({"gjc"} if include_gjc else set())
    return _inspect_writable(managed_files(wiki, workspace), groups, workspace)


def init_workspace(workspace: Path, clone: Path, project: str, include_agents: bool, include_gjc: bool) -> dict[str, object]:
    """Write the agent guidance of a workspace that roots.json maps to (clone, project); no local wiki is made."""
    wiki = load_wiki_at(clone)
    if project not in wiki.projects:
        raise WikiError(f"등록되지 않은 프로젝트: {project}; add-project를 먼저 실행")
    inspected = _guidance(wiki, workspace, include_agents, include_gjc)
    created: list[str] = []
    existing: list[str] = []
    updated: list[str] = []
    _write_managed(inspected, created, existing, updated)
    return {"root": str(workspace), "wiki": str(wiki.directory), "project": project, "created": created, "existing": existing, "updated": updated}


def link_workspace(workspace: Path, clone: Path, project: str, category: str | None, include_agents: bool, include_gjc: bool) -> dict[str, object]:
    """Set up a workspace against a central clone in one step: register the project, map it in roots.json, write guidance.

    Everything is checked before the first write. Registering a project or changing its category edits the
    clone, which other machines see only after ``llm-wiki sync``.
    """
    clone = clone.expanduser().resolve()
    wiki = load_wiki_at(clone)
    validate_project_name(project)
    if category is not None:
        validate_category(category)
    _guidance(wiki, workspace, include_agents, include_gjc)
    registered = project not in wiki.projects
    changed = registered or (category is not None and wiki.category(project) != category)
    if registered:
        wiki, _ = register_project(wiki, project, None, category or "")
    elif changed:
        wiki = set_category(wiki, project, category)
    previous = write_root(workspace, clone, project)
    result = init_workspace(workspace, clone, project, include_agents, include_gjc)
    result.update(registered=registered, previous=previous, next="llm-wiki sync --json" if changed else None)
    return result


def init_wiki(root: Path, mode: str | None, name: str | None, include_agents: bool, include_gjc: bool) -> dict[str, object]:
    root = root.resolve()
    found = wiki_dirs(root)
    directory = found[0] if found else root / "llm-wiki"
    created: list[str] = []
    existing: list[str] = []
    skipped: list[dict[str, str]] = []
    if mode is not None and mode not in {"single", "umbrella"}:
        raise WikiError("Mode must be single or umbrella.")
    if any(is_legacy_dir(candidate) for candidate in (root, *root.parents)) or any(is_legacy_dir(child) for child in found):
        raise WikiError("Cannot initialize over a legacy wiki. Add wiki.toml manually or choose another root.")
    inside_wiki = next((candidate for candidate in (root, *root.parents) if is_wiki_dir_name(candidate) and has_config(candidate)), None)
    if inside_wiki:
        raise WikiError(f"Cannot initialize inside an existing wiki directory: {inside_wiki}.")
    configured = has_config(directory)
    # A directory that already holds wiki.toml may be a git clone; one without it must not be turned into a wiki.
    if not configured and os.path.exists(directory / ".git"):
        raise WikiError(f"Cannot initialize over a git repository: {directory}.")
    if configured:
        wiki = discover(root)
        existing.append(str(directory / "wiki.toml"))
    else:
        wiki_name = name or root.name
        projects: dict[str, str]
        if mode == "umbrella":
            projects = {}
            for child in sorted(root.iterdir() if root.exists() else [], key=lambda item: item.name):
                if is_wiki_dir_name(child) or not os.path.isdir(child) or not os.path.exists(child / ".git"):
                    continue
                try:
                    validate_project_name(child.name)
                    if any(child.name.casefold() == project.casefold() for project in projects):
                        raise WikiError(f"Project already exists: {child.name}")
                except WikiError as exc:
                    skipped.append({"name": child.name, "reason": str(exc)})
                    continue
                projects[child.name] = child.name
        else:
            validate_project_name(wiki_name)
            projects = {wiki_name: "."}
        wiki = Wiki(root, directory, mode or "single", wiki_name, projects)
    groups = {"wiki"} | ({"agents"} if include_agents else set()) | ({"gjc"} if include_gjc else set())
    inspected = _inspect_writable(managed_files(wiki), groups, wiki.root)
    if not configured:
        write_config(wiki)
        created.append(str(directory / "wiki.toml"))
    for path in (directory / "shared" / "knowledge", directory / "shared" / "inbox" / "candidates"):
        if not path.exists():
            path.mkdir(parents=True)
            created.append(str(path))
    for project in wiki.projects:
        ensure_project_layout(wiki, project, created)
    updated: list[str] = []
    _write_managed(inspected, created, existing, updated)
    _write_missing(directory / "index.md", index(wiki.projects), created, existing)
    _write_missing(directory / "log.md", "# LLM Wiki Log\n", created, existing)
    _ensure_line(directory / ".gitattributes", GITATTRIBUTES_LINE, created, existing, updated)
    _ensure_line(directory / ".gitignore", GITIGNORE_LINE, created, existing, updated)
    result: dict[str, object] = {"root": str(root), "mode": wiki.mode, "projects": wiki.projects, "created": created, "existing": existing, "updated": updated}
    if wiki.mode == "umbrella":
        result["skipped"] = skipped
    if mode is not None and mode != wiki.mode:
        result["note"] = f"Existing wiki mode is {wiki.mode}; requested {mode}."
    return result


def init_central(directory: Path, name: str | None = None) -> dict[str, object]:
    """Turn a central clone into a wiki whose root directory is the repository itself.

    Only wiki files are created: no AGENTS.md, .gjc files, or wiki README, which belong to project workspaces.
    The directory must already hold wiki.toml or contain nothing but an optional .git.
    """
    directory = directory.expanduser().resolve()
    created: list[str] = []
    existing: list[str] = []
    updated: list[str] = []
    if not directory.is_dir():
        raise WikiError(f"Central wiki directory does not exist: {directory}.")
    configured = has_config(directory)
    if not configured:
        stray = [child.name for child in directory.iterdir() if child.name != ".git"]
        if stray:
            raise WikiError(f"Central wiki directory must be empty apart from .git: {directory}.")
        wiki = Wiki(directory.parent, directory, "umbrella", name or directory.name, {})
        write_config(wiki)
        created.append(str(directory / "wiki.toml"))
    else:
        wiki = load_wiki_at(directory)
        existing.append(str(directory / "wiki.toml"))
    for path in (directory / "shared" / "knowledge", directory / "shared" / "inbox" / "candidates"):
        if not path.exists():
            path.mkdir(parents=True)
            created.append(str(path))
    for project in wiki.projects:
        ensure_project_layout(wiki, project, created)
    _write_missing(directory / "index.md", index(wiki.projects), created, existing)
    _write_missing(directory / "log.md", "# LLM Wiki Log\n", created, existing)
    _ensure_line(directory / ".gitattributes", GITATTRIBUTES_LINE, created, existing, updated)
    _ensure_line(directory / ".gitignore", GITIGNORE_LINE, created, existing, updated)
    return {"root": str(directory), "mode": wiki.mode, "projects": wiki.projects, "created": created, "existing": existing, "updated": updated}


def register_project(wiki: Wiki, name: str, path: str | None, category: str = "") -> tuple[Wiki, str | None]:
    begin, end = "<!-- llm-wiki:projects:begin -->", "<!-- llm-wiki:projects:end -->"
    index_path = wiki.directory / "index.md"
    content = index_path.read_bytes().decode("utf-8") if index_path.exists() else None
    span = None if content is None else _marked_span(content, index_path, begin, end)
    updated = add_project(wiki, name, path or name, category)
    ensure_project_layout(updated, name)
    projects = index(updated.projects)
    block = projects[projects.index(begin) : projects.index(end) + len(end)]
    if content is None:
        index_path.write_bytes((block + "\n").encode("utf-8"))
    elif span is None:
        return updated, "index.md has no llm-wiki project markers; left unchanged."
    elif (spliced := _splice(content, span, block)) != content:
        index_path.write_bytes(spliced.encode("utf-8"))
    return updated, None
