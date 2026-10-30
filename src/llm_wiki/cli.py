"""Command-line interface for llm-wiki."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .brief import DEFAULT_LIMIT, brief
from .config import WikiError, current_project, discover_for, has_config, mapped_workspace, require_writable, set_category, sync_directory
from .curate import decide, pending, review
from .handoff import take
from .init import init_central, init_wiki, init_workspace, managed_status, refresh_command, register_project
from .qmd import qmd_sync
from .records import promote, wrap
from .search import search
from .sync import SyncError, require_finished_rebase, sync_wiki


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
    init.add_argument("--central", action="store_true", help="Initialize --path itself as a central wiki clone (no managed files).")

    add = subcommands.add_parser("add-project", help="Register an umbrella project")
    _common(add)
    add.add_argument("name")
    add.add_argument("--path")
    add.add_argument("--category", default="")

    status = subcommands.add_parser("status", help="Show wiki status")
    _common(status)

    find = subcommands.add_parser("search", help="Search Markdown")
    _common(find)
    find.add_argument("query")
    scope = find.add_mutually_exclusive_group()
    scope.add_argument("--project")
    scope.add_argument("--all-projects", action="store_true")
    scope.add_argument("--category", help="Search only the projects with this category.")
    find.add_argument("--engine", choices=("auto", "qmd", "text"), default="auto")
    find.add_argument("-n", type=int, default=10)

    qmd = subcommands.add_parser("qmd-sync", help="Synchronize QMD collections")
    _common(qmd)
    qmd.add_argument("--embed", action="store_true")

    central = subcommands.add_parser("sync", help="Commit, rebase, and push the wiki repository")
    _common(central)
    central.add_argument("--pull-only", action="store_true", help="Fetch and fast-forward only; never commit or push.")

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

    grouping = subcommands.add_parser("category", help="Show or set a project's category")
    _common(grouping)
    grouping.add_argument("project")
    grouping.add_argument("value", nargs="?")

    examining = subcommands.add_parser("review", help="Show one candidate's review material, or list the undecided ones")
    _common(examining)
    examining.add_argument("path", nargs="?")
    examining.add_argument("--project")
    for name, summary in (("accept", "Accept a candidate (owner only)"), ("reject", "Reject a candidate without deleting it")):
        deciding = subcommands.add_parser(name, help=summary)
        _common(deciding)
        deciding.add_argument("path")
        deciding.add_argument("--decided-by", default="")
        deciding.add_argument("--reason", required=True)

    starting = subcommands.add_parser("brief", help="Print a size-bounded start-of-session brief")
    _common(starting)
    starting.add_argument("--project")
    starting.add_argument("--max-bytes", type=int, default=DEFAULT_LIMIT)

    taking = subcommands.add_parser("take", help="Mark a handoff as taken by an agent")
    _common(taking)
    taking.add_argument("handoff_id")
    taking.add_argument("--agent", default="gjc")
    taking.add_argument("--project")
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
    elif args.command == "init":
        if args.central and (args.no_agents or args.no_gjc or args.mode is not None):
            raise InputError("--central cannot be combined with --mode, --no-agents, or --no-gjc")
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


def _status(wiki, cwd: Path, mapped_project: str | None = None) -> dict[str, Any]:
    projects = []
    for name in sorted(wiki.projects):
        base = wiki.directory / "projects" / name
        projects.append({"name": name, "path": wiki.projects[name], "knowledge": _count(base / "knowledge"), "sessions": _count(base / "sessions"), "candidates": _count(base / "inbox" / "candidates"), "category": wiki.category(name)})
    # A roots.json mapping puts the agent guidance in the mapped workspace and the wiki in its clone.
    mapping = mapped_workspace(cwd) if mapped_project is not None else None
    workspace = mapping[0] if mapping is not None else None
    managed = managed_status(wiki, workspace)
    result = {"root": str(wiki.root if workspace is None else wiki.directory), "mode": wiki.mode, "current_project": current_project(wiki, cwd, mapped_project), "projects": projects, "qmd_available": shutil.which("qmd") is not None, "managed": managed, "refresh": refresh_command(wiki, managed, workspace)}
    if workspace is not None:
        result["workspace"] = str(workspace)
    return result


def execute(args: argparse.Namespace) -> tuple[int, Any]:
    cwd = Path.cwd()
    if args.command == "init":
        if args.central:
            result = init_central(Path(args.path), args.name)
            return 0, result
        path = Path(args.path).expanduser().resolve()
        mapping = mapped_workspace(path)
        if mapping is not None and mapping[0] == path:
            if args.mode is not None or args.name is not None:
                raise WikiError(f"{path} is mapped in roots.json to {mapping[1]}; --mode and --name do not apply.")
            return 0, init_workspace(path, not args.no_agents, not args.no_gjc)
        result = init_wiki(path, args.mode, args.name, not args.no_agents, not args.no_gjc)
        return 0, result
    if args.command == "sync":
        target = sync_directory(cwd, Path(args.root) if args.root else None)
        if target is not None:
            require_finished_rebase(target)
            if args.pull_only and not has_config(target):
                # A clone made before the first push has no wiki.toml; pulling is exactly what brings it in.
                return 0, sync_wiki(target, pull_only=True)
    wiki, mapped_project = discover_for(cwd, Path(args.root) if args.root else None)
    if mapped_project is None:
        try:
            cwd.resolve().relative_to(wiki.root)
        except ValueError:
            cwd = wiki.root
    if args.command == "add-project":
        require_writable(wiki)
        updated, note = register_project(wiki, args.name, args.path, args.category)
        result: dict[str, Any] = {"project": args.name, "path": updated.projects[args.name]}
        if note:
            result["note"] = note
        return 0, result
    if args.command == "status":
        return 0, _status(wiki, cwd, mapped_project)
    if args.command == "review":
        if args.path is not None:
            return 0, review(wiki, args.path)
        project = args.project if args.project is not None else current_project(wiki, cwd, mapped_project)
        return 0, {"candidates": pending(wiki, project)}
    if args.command in {"accept", "reject"}:
        return 0, decide(wiki, args.path, f"{args.command}ed", args.decided_by, args.reason)
    if args.command == "category":
        if args.value is None:
            if args.project not in wiki.projects:
                raise WikiError(f"Unknown project: {args.project}")
            return 0, {"project": args.project, "category": wiki.category(args.project)}
        updated = set_category(wiki, args.project, args.value)
        return 0, {"project": args.project, "category": updated.category(args.project)}
    if args.command == "brief":
        project = args.project if args.project is not None else current_project(wiki, cwd, mapped_project)
        result = brief(wiki, project, args.max_bytes)
        return 0, result if args.json else result["text"]
    if args.command == "take":
        require_writable(wiki)
        # Without a project the handoff is looked up in shared/, where wrap writes it in that case.
        project = args.project if args.project is not None else current_project(wiki, cwd, mapped_project)
        return 0, {"path": str(take(wiki, project, args.handoff_id, args.agent))}
    if args.command == "search":
        if args.n < 1:
            raise WikiError("-n must be at least 1.")
        scoped = args.project if args.project is not None or args.category is not None else mapped_project
        results, engine, note = search(wiki, args.query, cwd, scoped, args.all_projects, args.engine, args.n, category=args.category)
        return 0, {"engine": engine, "note": note, "results": results}
    if args.command == "sync":
        require_writable(wiki)
        return 0, sync_wiki(wiki.directory, args.pull_only)
    if args.command == "qmd-sync":
        require_writable(wiki)
        qmd_sync(wiki, args.embed)
        return 0, {}
    if args.command == "wrap":
        selected = args.project if args.project is not None else mapped_project
        path, candidates = wrap(wiki, args.topic, args.task, args.completed, args.verification, args.handoff, args.not_tested, args.agent, selected, args.candidate, cwd, handoff_file=True)
        result = {"path": str(path), "candidates": [str(item) for item in candidates]}
        handoff_path = path.parent.parent.parent.parent / "handoffs" / path.name
        if handoff_path.is_file():
            result["handoff"] = str(handoff_path)
        return 0, result
    if args.command == "promote":
        require_writable(wiki)
        selected = None if args.shared else current_project(wiki, cwd, args.project if args.project is not None else mapped_project)
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
    except SyncError as exc:
        if args.json:
            _emit(exc.to_json())
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
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
    if isinstance(result, str):
        sys.stdout.write(result)
    else:
        _emit(result)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
