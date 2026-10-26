"""Command-line interface for llm-wiki."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import WikiError, current_project, discover, require_writable
from .init import init_wiki, register_project
from .qmd import qmd_sync
from .records import promote, wrap
from .search import search


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", help="Start wiki discovery at this directory.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="llm-wiki", description="Local Markdown wiki for LLM agents")
    subcommands = command.add_subparsers(dest="command", required=True)

    init = subcommands.add_parser("init", help="Create a wiki")
    init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    init.add_argument("--mode", choices=("single", "umbrella"))
    init.add_argument("--name")
    init.add_argument("--path", default=".")
    init.add_argument("--no-agents", action="store_true")
    init.add_argument("--no-gjc", action="store_true")

    add = subcommands.add_parser("add-project", help="Register an umbrella project")
    _common(add)
    add.add_argument("name")
    add.add_argument("--path")

    status = subcommands.add_parser("status", help="Show wiki status")
    _common(status)

    find = subcommands.add_parser("search", help="Search Markdown")
    _common(find)
    find.add_argument("query")
    scope = find.add_mutually_exclusive_group()
    scope.add_argument("--project")
    scope.add_argument("--all-projects", action="store_true")
    find.add_argument("--engine", choices=("auto", "qmd", "text"), default="auto")
    find.add_argument("-n", type=int, default=10)

    sync = subcommands.add_parser("qmd-sync", help="Synchronize QMD collections")
    _common(sync)
    sync.add_argument("--embed", action="store_true")

    session = subcommands.add_parser("wrap", help="Record a completed session")
    _common(session)
    session.add_argument("--topic", required=True)
    session.add_argument("--task", required=True)
    session.add_argument("--completed", required=True)
    session.add_argument("--verification")
    session.add_argument("--handoff")
    session.add_argument("--not-tested")
    session.add_argument("--agent", default="gjc")
    session.add_argument("--project")
    session.add_argument("--candidate", action="append", default=[])

    candidate = subcommands.add_parser("promote", help="Write a candidate note")
    _common(candidate)
    candidate.add_argument("--title", required=True)
    candidate.add_argument("--body", required=True)
    candidate.add_argument("--source", action="append", default=[])
    where = candidate.add_mutually_exclusive_group()
    where.add_argument("--project")
    where.add_argument("--shared", action="store_true")
    return command


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _count(directory: Path) -> int:
    return sum(1 for _ in directory.rglob("*.md")) if directory.exists() else 0


def _status(wiki, cwd: Path) -> dict[str, Any]:
    projects = []
    for name in sorted(wiki.projects):
        base = wiki.directory / "projects" / name
        projects.append({"name": name, "path": wiki.projects[name], "knowledge": _count(base / "knowledge"), "sessions": _count(base / "sessions"), "candidates": _count(base / "inbox" / "candidates")})
    return {"root": str(wiki.root), "mode": wiki.mode, "current_project": current_project(wiki, cwd), "projects": projects, "qmd_available": shutil.which("qmd") is not None}


def execute(args: argparse.Namespace) -> tuple[int, Any]:
    cwd = Path.cwd()
    if args.command == "init":
        result = init_wiki(Path(args.path), args.mode, args.name, not args.no_agents, not args.no_gjc)
        return 0, result
    wiki = discover(Path(args.root) if args.root else None)
    try:
        cwd.resolve().relative_to(wiki.root)
    except ValueError:
        cwd = wiki.root
    if args.command == "add-project":
        require_writable(wiki)
        updated, note = register_project(wiki, args.name, args.path)
        result: dict[str, Any] = {"project": args.name, "path": updated.projects[args.name]}
        if note:
            result["note"] = note
        return 0, result
    if args.command == "status":
        return 0, _status(wiki, cwd)
    if args.command == "search":
        if args.n < 1:
            raise WikiError("-n must be at least 1.")
        results, engine, note = search(wiki, args.query, cwd, args.project, args.all_projects, args.engine, args.n)
        return 0, {"engine": engine, "note": note, "results": results}
    if args.command == "qmd-sync":
        require_writable(wiki)
        qmd_sync(wiki, args.embed)
        return 0, {}
    if args.command == "wrap":
        path, candidates = wrap(wiki, args.topic, args.task, args.completed, args.verification, args.handoff, args.not_tested, args.agent, args.project, args.candidate, cwd)
        return 0, {"path": str(path), "candidates": [str(item) for item in candidates]}
    if args.command == "promote":
        require_writable(wiki)
        selected = current_project(wiki, cwd, args.project) if not args.shared else None
        path = promote(wiki, args.title, args.body, args.source, selected, args.shared)
        return 0, {"path": str(path)}
    raise AssertionError(f"Unhandled command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        code, result = execute(args)
    except FileNotFoundError as exc:
        if args.json:
            _emit({"error": str(exc)})
        else:
            print(str(exc), file=sys.stderr)
        return 2
    except (WikiError, OSError, ValueError) as exc:
        if args.json:
            _emit({"error": str(exc)})
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    _emit(result)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
