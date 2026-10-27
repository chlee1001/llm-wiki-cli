"""Optional QMD integration."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Wiki, WikiError

Runner = Callable[[list[str], int | None], subprocess.CompletedProcess[str]]
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
COLLECTION_LINE = re.compile(r"^(\S+) \(qmd://")


def run_command(command: list[str], timeout: int | None = 60) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)
    except (subprocess.SubprocessError, OSError) as exc:
        raise WikiError(str(exc) or "qmd command failed") from exc


def collection_name(wiki: Wiki, name: str) -> str:
    directory_hash = hashlib.sha256(str(wiki.directory.resolve()).encode()).hexdigest()[:8]
    raw = f"{wiki.name}-{directory_hash}-{name}".lower()
    return re.sub(r"[^a-z0-9]+", "-", raw).strip("-")


def collections(runner: Runner = run_command) -> set[str]:
    completed = runner(["qmd", "collection", "list"], 60)
    if completed.returncode:
        raise WikiError(completed.stderr.strip() or "qmd collection list failed")
    return {
        match.group(1).lower()
        for line in ANSI_ESCAPE.sub("", completed.stdout).splitlines()
        if (match := COLLECTION_LINE.match(line))
    }


def collection_targets(wiki: Wiki) -> list[tuple[str, Path, str]]:
    targets = [("shared", wiki.directory / "shared", "shared wiki")]
    targets.extend((name, wiki.directory / "projects" / name, f"{name} project wiki") for name in wiki.projects)
    return targets


def qmd_sync(wiki: Wiki, embed: bool = False, runner: Runner = run_command, executable: str | None = None) -> list[list[str]]:
    if executable is None:
        executable = shutil.which("qmd")
    if not executable:
        raise FileNotFoundError("qmd is not installed. Install it with 'npm install -g @tobilu/qmd' or see https://github.com/tobi/qmd")
    targets = collection_targets(wiki)
    target_names: dict[str, str] = {}
    for name, _, _ in targets:
        coll = collection_name(wiki, name)
        previous = target_names.setdefault(coll, name)
        if previous != name:
            raise WikiError(f"QMD collection name collision: projects '{previous}' and '{name}' both map to '{coll}'.")
    existing = collections(runner)
    executed: list[list[str]] = []
    for name, directory, description in targets:
        coll = collection_name(wiki, name)
        if coll not in existing:
            command = ["qmd", "collection", "add", str(directory), "--name", coll, "--mask", "**/*.md"]
            result = runner(command, 60)
            executed.append(command)
            if result.returncode:
                raise WikiError(result.stderr.strip() or "qmd collection add failed")
        command = ["qmd", "context", "add", f"qmd://{coll}", description]
        result = runner(command, 60)
        executed.append(command)
        if result.returncode:
            raise WikiError(result.stderr.strip() or "qmd context add failed")
    for command in (["qmd", "update"], *( [["qmd", "embed"]] if embed else [] )):
        result = runner(command, None)
        executed.append(command)
        if result.returncode:
            raise WikiError(result.stderr.strip() or "qmd command failed")
    return executed


def qmd_search(query: str, names: list[str], wiki: Wiki, limit: int, runner: Runner = run_command) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    directories = {name: directory for name, directory, _ in collection_targets(wiki)}
    for name in names:
        coll = collection_name(wiki, name)
        command = ["qmd", "search", query, "--json", "-n", str(limit), "-c", coll]
        completed = runner(command, 60)
        if completed.returncode:
            raise WikiError(completed.stderr.strip() or "qmd search failed")
        try:
            records = json.loads(completed.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise WikiError("Invalid qmd search response.") from exc
        if not isinstance(records, list):
            raise WikiError("Unexpected qmd search response.")
        prefix = f"qmd://{coll}/"
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("file"), str) or not record["file"].startswith(prefix):
                raise WikiError("Unexpected qmd search response.")
            path = (directories[name] / record["file"].removeprefix(prefix)).relative_to(wiki.root).as_posix()
            try:
                score = float(record.get("score", 0))
            except (TypeError, ValueError):
                raise WikiError("Unexpected qmd search response.")
            snippets = [record["snippet"]] if isinstance(record.get("snippet"), str) else []
            found.append({
                "path": path,
                "score": score,
                "snippets": snippets,
            })
    return sorted(found, key=lambda item: item["score"], reverse=True)[:limit]
