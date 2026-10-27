"""Creation and maintenance of the on-disk wiki layout."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .config import Wiki, WikiError, add_project, discover, has_config, is_legacy_dir, is_wiki_dir_name, validate_project_name, wiki_dirs, write_config
from .templates import GJC_FRONT_MATTER, agents_block, gjc_rule, index, wiki_readme


def _write_missing(path: Path, content: str, created: list[str], existing: list[str]) -> None:
    if path.exists():
        existing.append(str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    created.append(str(path))


def _replace_marked(
    path: Path,
    block: str,
    created: list[str],
    existing: list[str],
    append_if_missing: bool = False,
    prefix: str = "",
    begin: str = "<!-- llm-wiki:begin -->",
    end: str = "<!-- llm-wiki:end -->",
) -> bool | None:
    block = block.rstrip("\n")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prefix + block + "\n", encoding="utf-8")
        created.append(str(path))
        return True
    content = path.read_text(encoding="utf-8")
    start = content.find(begin)
    finish_start = content.find(end, start + len(begin)) if start >= 0 else -1
    if (start < 0) != (content.find(end) < 0) or (start >= 0 and (finish_start < 0 or content.find(begin, start + len(begin)) >= 0 or content.find(end, finish_start + len(end)) >= 0)):
        raise WikiError(f"Unpaired llm-wiki markers in {path}.")
    if start >= 0:
        updated = content[:start] + block + content[finish_start + len(end):]
        if updated != content:
            path.write_text(updated, encoding="utf-8")
        existing.append(str(path))
        return updated != content
    elif append_if_missing:
        separator = "" if content.endswith("\n") else "\n"
        path.write_text(content + separator + "\n" + block + "\n", encoding="utf-8")
        existing.append(str(path))
        return True
    else:
        existing.append(str(path))
        return None


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
        write_config(wiki)
        created.append(str(directory / "wiki.toml"))
    for path in (directory / "shared" / "knowledge", directory / "shared" / "inbox" / "candidates"):
        if not path.exists():
            path.mkdir(parents=True)
            created.append(str(path))
    for project in wiki.projects:
        ensure_project_layout(wiki, project, created)
    _write_missing(directory / "README.md", wiki_readme(wiki.name), created, existing)
    _write_missing(directory / "index.md", index(wiki.projects), created, existing)
    _write_missing(directory / "log.md", "# LLM Wiki Log\n", created, existing)
    if include_agents:
        _replace_marked(root / "AGENTS.md", agents_block(directory.name), created, existing, append_if_missing=True)
    if include_gjc:
        _replace_marked(root / ".gjc" / "rules" / "llm-wiki.md", gjc_rule(directory.name), created, existing, prefix=GJC_FRONT_MATTER)
    result: dict[str, object] = {"root": str(root), "mode": wiki.mode, "projects": wiki.projects, "created": created, "existing": existing}
    if wiki.mode == "umbrella":
        result["skipped"] = skipped
    if mode is not None and mode != wiki.mode:
        result["note"] = f"Existing wiki mode is {wiki.mode}; requested {mode}."
    return result


def register_project(wiki: Wiki, name: str, path: str | None) -> tuple[Wiki, str | None]:
    project_path = path or name
    updated = add_project(wiki, name, project_path)
    ensure_project_layout(updated, name)
    projects = index(updated.projects)
    project_block_start = projects.index("<!-- llm-wiki:projects:begin -->")
    project_block_end = projects.index("<!-- llm-wiki:projects:end -->") + len("<!-- llm-wiki:projects:end -->")
    changed = _replace_marked(
        updated.directory / "index.md",
        projects[project_block_start:project_block_end],
        [],
        [],
        begin="<!-- llm-wiki:projects:begin -->",
        end="<!-- llm-wiki:projects:end -->",
    )
    return updated, "index.md has no llm-wiki project markers; left unchanged." if changed is None else None
