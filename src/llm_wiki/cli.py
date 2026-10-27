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


class ParseError(Exception):
    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        self.parser = parser
        self.message = message
        super().__init__(message)


class InputError(Exception):
    pass


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ParseError(self, message)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", help="Start wiki discovery at this directory.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")


def parser() -> ArgumentParser:
    command = ArgumentParser(prog="llm-wiki", description="Local Markdown wiki for LLM agents")
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
    session.add_argument("--input-json", metavar="PATH", help="Read wrap fields from JSON stdin (-) or a file.")
    session.add_argument("--topic")
    session.add_argument("--task")
    session.add_argument("--completed")
    session.add_argument("--verification")
    session.add_argument("--handoff")
    session.add_argument("--not-tested")
    session.add_argument("--agent")
    session.add_argument("--project")
    session.add_argument("--candidate", action="append")

    candidate = subcommands.add_parser("promote", help="Write a candidate note")
    _common(candidate)
    candidate.add_argument("--input-json", metavar="PATH", help="Read promote fields from JSON stdin (-) or a file.")
    candidate.add_argument("--title")
    candidate.add_argument("--body")
    candidate.add_argument("--source", action="append")
    where = candidate.add_mutually_exclusive_group()
    where.add_argument("--project")
    where.add_argument("--shared", action="store_true")
    return command


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _parse_error_message(message: str) -> str:
    if message == "the following arguments are required: command":
        return "a command is required"
    if message.startswith("the following arguments are required:"):
        return "required command arguments are missing"
    if "invalid choice:" in message:
        return "invalid command or option value"
    return "invalid command arguments"


def _read_json_input(path: str) -> dict[str, Any]:
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InputError("could not read JSON input") from exc

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InputError("JSON input contains duplicate keys")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=object_pairs)
    except json.JSONDecodeError as exc:
        raise InputError("JSON input is malformed") from exc
    if not isinstance(value, dict):
        raise InputError("JSON input must be an object")
    return value


def _json_fields(args: argparse.Namespace, fields: tuple[str, ...]) -> None:
    if any(
        getattr(args, field) is not None and (field != "shared" or getattr(args, field))
        for field in fields
    ):
        raise InputError("--input-json cannot be combined with payload options")
    data = _read_json_input(args.input_json)
    if not set(data).issubset(fields):
        raise InputError("JSON input contains unknown fields")
    for field in fields:
        if field not in data:
            continue
        value = data[field]
        if field in ("candidate", "source"):
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise InputError(f"JSON field {field} must be a list of strings")
        elif field == "shared":
            if type(value) is not bool:
                raise InputError("JSON field shared must be a boolean")
        elif not isinstance(value, str):
            raise InputError(f"JSON field {field} must be a string")
        setattr(args, field, value)


def _normalize_input(args: argparse.Namespace) -> None:
    if args.command == "wrap":
        fields = ("topic", "task", "completed", "verification", "handoff", "not_tested", "agent", "project", "candidate")
        if args.input_json is not None:
            _json_fields(args, fields)
        missing = [field for field in ("topic", "task", "completed") if getattr(args, field) is None]
        if missing:
            if args.input_json is not None:
                raise InputError("JSON input is missing required fields")
            raise ParseError(parser(), f"the following arguments are required: {', '.join(f'--{field}' for field in missing)}")
        if args.agent is None:
            args.agent = "gjc"
        if args.candidate is None:
            args.candidate = []
    elif args.command == "promote":
        fields = ("title", "body", "source", "project", "shared")
        if args.input_json is not None:
            _json_fields(args, fields)
        missing = [field for field in ("title", "body") if getattr(args, field) is None]
        if missing:
            if args.input_json is not None:
                raise InputError("JSON input is missing required fields")
            raise ParseError(parser(), f"the following arguments are required: {', '.join(f'--{field}' for field in missing)}")
        if args.source is None:
            args.source = []
        if args.project is not None and args.shared:
            raise InputError("project and shared cannot be used together")


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
    argv = sys.argv[1:] if argv is None else argv
    machine = "--json" in argv
    command = parser()
    try:
        args = command.parse_args(argv)
        _normalize_input(args)
    except ParseError as exc:
        if machine:
            _emit({"error": _parse_error_message(exc.message)})
        else:
            exc.parser.print_usage(sys.stderr)
            print(f"{exc.parser.prog}: error: {exc.message}", file=sys.stderr)
        return 2
    except InputError as exc:
        if machine:
            _emit({"error": str(exc)})
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
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
