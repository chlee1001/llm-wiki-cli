"""Shared helpers for tests that drive real git repositories (bare remote plus clones)."""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_IDENTITY = "[user]\n\tname = Test\n\temail = test@example.com\n"
_BASE = "[init]\n\tdefaultBranch = main\n[protocol \"file\"]\n\tallow = always\n"
_SCRUB = ("EMAIL", "XDG_CONFIG_HOME", "LLM_WIKI_ROOT", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


class GitWikiCase(unittest.TestCase):
    """Isolates git from the host: private global config, no system config, no identity env."""

    identity = True

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name).resolve()
        config = self.tmp / "gitconfig"
        config.write_text(_BASE + (_IDENTITY if self.identity else "[user]\n\tuseConfigOnly = true\n"), encoding="utf-8")
        home = self.tmp / "home"
        home.mkdir()
        env = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_SYSTEM": "/dev/null", "HOME": str(home)})
        env.start()
        self.addCleanup(env.stop)
        for name in _SCRUB:
            os.environ.pop(name, None)
        self.remote = self.tmp / "remote.git"
        self.git("init", "--bare", "-q", str(self.remote), cwd=self.tmp)

    def git(self, *args: str, cwd: Path, check: bool = True) -> str:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        if check and result.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
        return result.stdout.strip()

    def clone(self, name: str) -> Path:
        target = self.tmp / name
        self.git("clone", "-q", str(self.remote), str(target), cwd=self.tmp)
        return target

    def head(self, repo: Path) -> str:
        return self.git("rev-parse", "HEAD", cwd=repo)

    def commit_count(self, repo: Path) -> int:
        return int(self.git("rev-list", "--count", "HEAD", cwd=repo))


def snapshot(directory: Path) -> dict[str, str]:
    """Map each file under directory (outside .git and the tmp dir) to its sha256."""
    result: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        relative = path.relative_to(directory)
        if not path.is_file() or relative.parts[0] in {".git", ".llm-wiki-tmp"}:
            continue
        result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _body(data: bytes) -> bytes:
    """Return the bytes after the closing front matter delimiter (the whole text if there is none)."""
    if not data.startswith(b"---\n"):
        return data
    end = data.find(b"\n---\n", 3)
    return data[end + 5 :] if end != -1 else data


def assert_immutable(before: dict[str, str], after: dict[str, str], directory: Path, old: dict[str, bytes], after_union: bool = False) -> None:
    """Judge a wiki change against the immutability rules.

    ``old`` maps every path in ``before`` to its previous bytes. Sessions and handoffs keep their bytes,
    ``log.md`` only grows (prefix, or every old line kept after a union merge), candidates keep their body
    below the first front matter, and new files are always allowed.
    """
    for relative in before:
        if relative not in after:
            raise AssertionError(f"file removed: {relative}")
        if before[relative] == after[relative]:
            continue
        new = (directory / relative).read_bytes()
        if relative.startswith(("sessions/", "handoffs/")) or "/sessions/" in relative or "/handoffs/" in relative:
            raise AssertionError(f"immutable file modified: {relative}")
        if relative == "log.md":
            if after_union:
                lines = new.splitlines()
                if any(line not in lines for line in old[relative].splitlines()) or len(lines) < len(old[relative].splitlines()):
                    raise AssertionError("log.md lost lines")
            elif not new.startswith(old[relative]):
                raise AssertionError("log.md is not append-only")
        elif "/inbox/candidates/" in relative and relative.endswith(".md"):
            if _body(new) != _body(old[relative]):
                raise AssertionError(f"candidate body modified: {relative}")
        else:
            raise AssertionError(f"unexpected modification: {relative}")


def read_all(directory: Path) -> dict[str, bytes]:
    return {relative: (directory / relative).read_bytes() for relative in snapshot(directory)}
