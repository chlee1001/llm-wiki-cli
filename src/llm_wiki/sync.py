"""Synchronize a wiki directory with its git remote without ever resolving conflicts for the user."""
from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Wiki, WikiError
from .redact import redact

GIT_TIMEOUT = 120


class SyncError(WikiError):
    """A sync failure with a stable code and a JSON-safe payload (never containing secret values)."""

    def __init__(self, code: str, message: str, **payload: Any) -> None:
        super().__init__(message)
        self.code = code
        self.payload: dict[str, Any] = payload

    def to_json(self) -> dict[str, Any]:
        return {"error": str(self), "code": self.code, **self.payload}


@dataclass(frozen=True)
class SyncState:
    toplevel_ok: bool
    has_remote: bool
    rebase_in_progress: bool
    unmerged: bool
    has_upstream: bool
    dirty: bool
    unpushed: int
    unborn: bool
    remote_has_branch: bool = False


def plan_sync(state: SyncState, pull_only: bool = False) -> list[str]:
    """Return the ordered actions for a git state snapshot, or raise SyncError for a refused state."""
    if not state.toplevel_ok:
        raise SyncError("guard", "The wiki directory is not the top level of a git repository.", reason="toplevel")
    if not state.has_remote:
        raise SyncError("guard", "The repository has no remote to sync with.", reason="no_remote")
    if state.rebase_in_progress or state.unmerged:
        raise SyncError("guard", REBASE_GUARD, reason="rebase_in_progress", rebase_in_progress=state.rebase_in_progress)
    if pull_only:
        actions = ["fetch"]
        if state.has_upstream and not state.unborn:
            actions.append("skip-merge-dirty" if state.dirty else "merge-ff")
        elif state.remote_has_branch:
            actions.append("skip-merge-dirty" if state.dirty else "adopt-merge")
        return actions
    actions: list[str] = []
    committing = state.dirty
    if committing or state.unpushed or not state.has_upstream:
        actions.append("scan")
    if committing:
        actions += ["identity", "add", "recheck", "commit"]
    if state.has_upstream and not state.unborn:
        actions += ["pull", "push"]
    elif state.remote_has_branch:
        actions += ["adopt-remote", "pull", "push"]
    else:
        actions.append("push-first")
    return actions


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"})
    return env


def _git(directory: Path, *args: str, check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    if not directory.is_dir():
        raise SyncError("guard", f"The wiki directory does not exist: {directory}", reason="missing_directory")
    try:
        result = subprocess.run(
            ["git", *args], cwd=directory, capture_output=True, text=True, env=_env(), timeout=GIT_TIMEOUT, input=stdin,
        )
    except FileNotFoundError as exc:
        raise SyncError("guard", "git is not installed.", reason="no_git") from exc
    except subprocess.TimeoutExpired as exc:
        raise SyncError("git_timeout", f"git {args[0]} timed out after {GIT_TIMEOUT}s.", command=args[0]) from exc
    if check and result.returncode != 0:
        raise SyncError("git_failed", f"git {args[0]} failed.", command=args[0], detail=_scrub(result.stderr))
    return result


def _blob(directory: Path, spec: str) -> bytes:
    """Read one object's bytes; a blob that cannot be read stops the sync instead of going unscanned."""
    try:
        result = subprocess.run(["git", "show", spec], cwd=directory, capture_output=True, env=_env(), timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise SyncError("git_timeout", f"git show timed out after {GIT_TIMEOUT}s.", command="show") from exc
    if result.returncode != 0:
        raise SyncError("git_failed", "git show failed; the content could not be checked for secrets.", command="show", detail=_scrub(result.stderr.decode("utf-8", "replace")))
    return result.stdout


def _scrub(text: str) -> str:
    return redact(text.strip())[0][:500]


def repository_git_dir(directory: Path) -> Path | None:
    result = _git(directory, "rev-parse", "--absolute-git-dir", check=False)
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def rebase_in_progress(directory: Path) -> bool:
    git_dir = repository_git_dir(directory)
    if git_dir is None:
        return False
    return any((git_dir / name).exists() for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD"))


def owns_repository(directory: Path) -> bool:
    top = _git(directory, "rev-parse", "--show-toplevel", check=False)
    return top.returncode == 0 and Path(top.stdout.strip()).resolve() == directory.resolve()


REBASE_GUARD = "A rebase or merge is in progress; resolve it first."


def _owned_unless_no_git(directory: Path) -> bool:
    """Whether directory is its own repository's top level; a machine without git counts as not owned.

    Every other failure (a timeout, a missing directory) propagates so a guard never passes by accident.
    """
    try:
        return owns_repository(directory)
    except SyncError as exc:
        if exc.code == "guard" and exc.payload.get("reason") == "no_git":
            return False
        raise


def require_finished_rebase(directory: Path) -> None:
    """Stop a sync of a clone whose own rebase or merge is unfinished, before its wiki.toml is read.

    A conflict can leave markers in wiki.toml itself, so this check cannot wait for the wiki to load.
    """
    if not _owned_unless_no_git(directory):
        return
    if rebase_in_progress(directory):
        raise SyncError("guard", REBASE_GUARD, reason="rebase_in_progress", rebase_in_progress=True)


def require_no_rebase(wiki: Wiki) -> None:
    """Refuse a write while a rebase or merge is unfinished in the wiki's own git repository.

    A wiki nested in another repository, a wiki outside git, and a machine without git are not affected.
    """
    if _owned_unless_no_git(wiki.directory) and rebase_in_progress(wiki.directory):
        raise WikiError("A git rebase or merge is in progress in the wiki; finish or abort it before writing records.")


def _snapshot(directory: Path) -> SyncState:
    top = _git(directory, "rev-parse", "--show-toplevel", check=False)
    toplevel_ok = top.returncode == 0 and Path(top.stdout.strip()).resolve() == directory.resolve()
    if not toplevel_ok:
        return SyncState(False, False, False, False, False, False, 0, False)
    remotes = _git(directory, "remote").stdout.split()
    head = _git(directory, "rev-parse", "--verify", "-q", "HEAD", check=False)
    unborn = head.returncode != 0
    upstream = _git(directory, "rev-parse", "--abbrev-ref", "@{u}", check=False)
    has_upstream = upstream.returncode == 0
    unmerged = bool(_git(directory, "diff", "--name-only", "--diff-filter=U").stdout.strip())
    dirty = bool(_git(directory, "status", "--porcelain", "-z", "-uall", "--", ".").stdout)
    unpushed = 0
    if has_upstream and not unborn:
        unpushed = int(_git(directory, "rev-list", "--count", "@{u}..HEAD").stdout.strip() or 0)
    elif not unborn:
        unpushed = int(_git(directory, "rev-list", "--count", "HEAD").stdout.strip() or 0)
    remote_has_branch = False
    if remotes and not has_upstream:
        branch = _git(directory, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip() or "main"
        listed = _git(directory, "ls-remote", "--heads", remotes[0], f"refs/heads/{branch}", check=False)
        remote_has_branch = listed.returncode == 0 and bool(listed.stdout.strip())
    return SyncState(True, bool(remotes), rebase_in_progress(directory), unmerged, has_upstream, dirty, unpushed, unborn, remote_has_branch)


def _pattern_kinds(text: str) -> list[str]:
    # Judge by whether redaction would change the text: some patterns re-match their own placeholders,
    # so a substitution count would flag records the tool already redacted when it wrote them.
    return ["secret"] if redact(text)[0] != text else []


def _text(data: bytes) -> str | None:
    """Return text to scan, or None for real binaries (a NUL byte, the same test git uses).

    Bytes that are not valid UTF-8 are replaced rather than skipped, so one stray byte cannot hide a secret.
    """
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _flagged(paths_and_text: list[tuple[str, bytes]]) -> list[str]:
    flagged = []
    for path, data in paths_and_text:
        text = _text(data)
        if text is not None and _pattern_kinds(text):
            flagged.append(path)
    return flagged


def _secret_error(paths: list[str], where: str, in_history: bool) -> SyncError:
    recovery = [
        "Edit the listed files so the value is removed or masked, then run sync again.",
        "Session and handoff files are immutable: the owner must delete the file and record a cleaned copy.",
    ]
    if in_history:
        recovery.append("The value is already in a local commit that was not pushed: the owner must clean the history (for example reset or rewrite it); this tool never rewrites history.")
    recovery.append("There is no override flag; fix the content instead.")
    return SyncError(
        "secret_detected",
        f"Likely secrets found in {where}; nothing was committed or pushed.",
        paths=sorted(set(paths)), kinds=["secret"], recovery=recovery, owner_action_required=True,
    )


def _scan_worktree(directory: Path) -> list[str]:
    raw = _git(directory, "status", "--porcelain", "-z", "-uall", "--", ".").stdout
    entries = [entry for entry in raw.split("\0") if entry]
    paths: list[str] = []
    skip = False
    for entry in entries:
        if skip:  # the old name of a rename follows its new name
            skip = False
            continue
        code, name = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            skip = True
        if "D" in code and not (directory / name).exists():
            continue
        paths.append(name)
    found = []
    for name in paths:
        target = directory / name
        if target.is_file():
            found.append((name, target.read_bytes()))
    return _flagged(found)


def _scan_unpushed(directory: Path, has_upstream: bool) -> list[str]:
    rev_range = ["@{u}..HEAD"] if has_upstream else ["HEAD"]
    commits = _git(directory, "rev-list", *rev_range).stdout.split()
    flagged: list[str] = []
    for commit in commits:
        # -c also lists what a merge commit itself introduced (paths differing from every parent).
        names = _git(directory, "diff-tree", "-c", "--root", "-r", "--no-commit-id", "--name-only", "--diff-filter=ACMRT", "-z", commit).stdout.split("\0")
        for name in filter(None, names):
            flagged += _flagged([(name, _blob(directory, f"{commit}:{name}"))])
    return flagged


def _scan_index(directory: Path) -> list[str]:
    names = _git(directory, "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMRT").stdout.split("\0")
    found = []
    for name in filter(None, names):
        found.append((name, _blob(directory, f":{name}")))
    return _flagged(found)


def _require_identity(directory: Path) -> None:
    result = _git(directory, "-c", "user.useConfigOnly=true", "var", "GIT_COMMITTER_IDENT", check=False)
    if result.returncode != 0:
        raise SyncError(
            "no_identity", "git has no user.name/user.email; set them before syncing.",
            recovery=["Run: git config user.name \"<name>\" && git config user.email \"<email>\" in the wiki repository."],
        )


def _conflict_error(directory: Path) -> SyncError:
    names = sorted(filter(None, _git(directory, "diff", "--name-only", "--diff-filter=U").stdout.splitlines()))
    return SyncError(
        "conflict", "Rebase stopped on conflicts; nothing was resolved automatically.",
        conflicts=names, rebase_in_progress=rebase_in_progress(directory),
        recovery=["Resolve the listed files, run git add, then git rebase --continue (or git rebase --abort), then sync again."],
    )


def _stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sync_wiki(directory: Path, pull_only: bool = False, after_scan: Callable[[], None] | None = None) -> dict[str, Any]:
    """Commit everything in the wiki directory, rebase on the remote, and push.

    ``after_scan`` is a test hook that runs between the secret scan and ``git add``.
    """
    directory = directory.expanduser().resolve()
    state = _snapshot(directory)
    actions = plan_sync(state, pull_only)
    result: dict[str, Any] = {"status": "ok", "committed": False, "commit": None, "pushed": False}
    if pull_only:
        return _run_pull_only(directory, state, actions, result)
    for action in actions:
        if action == "scan":
            flagged = _scan_worktree(directory)
            if flagged:
                raise _secret_error(flagged, "files that would be committed", False)
            unpushed = _scan_unpushed(directory, state.has_upstream and not state.unborn) if state.unpushed else []
            if unpushed:
                raise _secret_error(unpushed, "commits that have not been pushed", True)
            if after_scan is not None:
                after_scan()
        elif action == "identity":
            _require_identity(directory)
        elif action == "add":
            _git(directory, "add", "-A", "--", ".")
        elif action == "recheck":
            flagged = _scan_index(directory)
            if flagged:
                _git(directory, "reset", "-q", "--", ".")
                raise _secret_error(flagged, "files staged for commit", False)
        elif action == "commit":
            if not _git(directory, "diff", "--cached", "--name-only").stdout.strip():
                continue
            message = f"llm-wiki sync: {socket.gethostname().split('.')[0]} {_stamp()}"
            _git(directory, "-c", "commit.gpgsign=false", "commit", "--no-verify", "-q", "-m", message)
            result["committed"] = True
        elif action == "adopt-remote":
            remote = _git(directory, "remote").stdout.split()[0]
            branch = _git(directory, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
            _git(directory, "fetch", "-q", remote, branch)
            _git(directory, "branch", "--set-upstream-to", f"{remote}/{branch}", branch)
        elif action == "pull":
            pulled = _git(directory, "pull", "--rebase", "-q", check=False)
            if pulled.returncode != 0:
                if rebase_in_progress(directory) or _git(directory, "diff", "--name-only", "--diff-filter=U").stdout.strip():
                    raise _conflict_error(directory)
                raise SyncError("pull_failed", "git pull --rebase failed.", detail=_scrub(pulled.stderr))
        elif action in {"push", "push-first"}:
            args = ["push", "-q", "--no-verify"] if action == "push" else ["push", "-q", "--no-verify", "-u", "origin", "HEAD"]
            pushed = _git(directory, *args, check=False)
            if pushed.returncode != 0:
                raise SyncError("push_rejected", "git push was rejected; the local commit is kept and the next sync retries.", detail=_scrub(pushed.stderr))
            result["pushed"] = True
    head = _git(directory, "rev-parse", "--verify", "-q", "HEAD", check=False)
    result["commit"] = head.stdout.strip() or None
    return result


def _run_pull_only(directory: Path, state: SyncState, actions: list[str], result: dict[str, Any]) -> dict[str, Any]:
    fetched = _git(directory, "fetch", "-q", check=False)
    if fetched.returncode != 0:
        result.update(status="offline", detail=_scrub(fetched.stderr))
        return result
    result["merged"] = False
    if "merge-ff" in actions:
        merged = _git(directory, "merge", "--ff-only", "-q", "@{u}", check=False)
        if merged.returncode != 0:
            raise SyncError("merge_failed", "The local branch cannot fast-forward to the remote; run a full sync.", detail=_scrub(merged.stderr))
        result["merged"] = True
    elif "adopt-merge" in actions:
        # A clone made before the first push: fast-forward onto the remote branch, then track it.
        remote = _git(directory, "remote").stdout.split()[0]
        branch = _git(directory, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
        merged = _git(directory, "merge", "--ff-only", "-q", f"{remote}/{branch}", check=False)
        if merged.returncode != 0:
            raise SyncError("merge_failed", "The clone cannot fast-forward to the remote branch.", detail=_scrub(merged.stderr))
        _git(directory, "branch", "--set-upstream-to", f"{remote}/{branch}", branch)
        result["merged"] = True
    elif "skip-merge-dirty" in actions:
        result["reason"] = "dirty"
    head = _git(directory, "rev-parse", "--verify", "-q", "HEAD", check=False)
    result["commit"] = head.stdout.strip() or None
    return result
