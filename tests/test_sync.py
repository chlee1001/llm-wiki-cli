from __future__ import annotations

import json
import os
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_wiki import cli
from llm_wiki.config import WikiError, discover, load_wiki_at
from llm_wiki.init import init_central, init_wiki, register_project
from llm_wiki.records import promote, wrap
from llm_wiki.sync import SyncError, SyncState, plan_sync, sync_wiki

from gitwiki import GitWikiCase, assert_immutable, read_all, snapshot

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"  # assembled so this file itself carries no key-shaped literal


def state(**overrides: object) -> SyncState:
    base = dict(toplevel_ok=True, has_remote=True, rebase_in_progress=False, unmerged=False, has_upstream=True, dirty=True, unpushed=0, unborn=False)
    base.update(overrides)
    return SyncState(**base)  # type: ignore[arg-type]


class PlanSyncTests(unittest.TestCase):
    def test_guards_reject(self) -> None:
        for field, code in (("toplevel_ok", "guard"), ("has_remote", "guard"), ("rebase_in_progress", "guard"), ("unmerged", "guard")):
            value = field in {"rebase_in_progress", "unmerged"}
            with self.assertRaises(SyncError) as caught:
                plan_sync(state(**{field: value}))
            self.assertEqual(caught.exception.code, code, field)

    def test_dirty_tree_is_allowed_and_commits(self) -> None:
        self.assertEqual(plan_sync(state(dirty=True)), ["scan", "identity", "add", "recheck", "commit", "pull", "push"])

    def test_clean_tree_skips_commit_but_scans_unpushed(self) -> None:
        self.assertEqual(plan_sync(state(dirty=False, unpushed=2)), ["scan", "pull", "push"])

    def test_clean_and_nothing_unpushed_still_pulls_and_pushes(self) -> None:
        self.assertEqual(plan_sync(state(dirty=False, unpushed=0)), ["pull", "push"])

    def test_unborn_with_changes_uses_first_push(self) -> None:
        self.assertEqual(plan_sync(state(unborn=True, has_upstream=False, dirty=True)), ["scan", "identity", "add", "recheck", "commit", "push-first"])

    def test_no_upstream_pushes_first(self) -> None:
        self.assertEqual(plan_sync(state(has_upstream=False, dirty=False, unpushed=1)), ["scan", "push-first"])

    def test_pull_only_never_writes(self) -> None:
        self.assertEqual(plan_sync(state(dirty=True), pull_only=True), ["fetch", "skip-merge-dirty"])
        self.assertEqual(plan_sync(state(dirty=False), pull_only=True), ["fetch", "merge-ff"])
        self.assertEqual(plan_sync(state(dirty=False, has_upstream=False), pull_only=True), ["fetch"])
        self.assertEqual(plan_sync(state(dirty=False, has_upstream=False, unborn=True, remote_has_branch=True), pull_only=True), ["fetch", "adopt-merge"])


class SyncCase(GitWikiCase):
    def seed(self, name: str = "m3") -> Path:
        """First machine: empty clone of an empty remote turned into a central wiki and pushed."""
        repo = self.clone(name)
        init_central(repo, "company")
        sync_wiki(repo)
        return repo

    def make_wiki(self, repo: Path):
        return load_wiki_at(repo)

    def put(self, repo: Path, relative: str, text: str) -> Path:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def write_session(self, repo: Path, topic: str = "work") -> Path:
        wiki = self.make_wiki(repo)
        path, _ = wrap(wiki, topic, "task", "done", None, None, None, "gjc", None, [], repo)
        return path


class SyncTests(SyncCase):
    # 8: empty remote, unborn branch
    def test_first_sync_on_empty_remote_pushes_with_upstream(self) -> None:
        repo = self.clone("m3")
        init_central(repo, "company")
        result = sync_wiki(repo)
        self.assertTrue(result["committed"])
        self.assertTrue(result["pushed"])
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), self.head(repo))
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "@{u}", cwd=repo), "origin/main")

    # 1: nothing to commit
    def test_no_changes_makes_no_commit(self) -> None:
        repo = self.seed()
        before = self.head(repo)
        result = sync_wiki(repo)
        self.assertFalse(result["committed"])
        self.assertEqual(self.head(repo), before)
        self.assertEqual(result["status"], "ok")

    # 2: one commit for everything inside the wiki
    def test_changes_become_a_single_commit_with_the_documented_message(self) -> None:
        repo = self.seed()
        count = self.commit_count(repo)
        self.write_session(repo, "one")
        self.write_session(repo, "two")
        promote(self.make_wiki(repo), "cand", "body", [], None, True)
        result = sync_wiki(repo)
        self.assertTrue(result["committed"])
        self.assertEqual(self.commit_count(repo), count + 1)
        message = self.git("log", "-1", "--format=%s", cwd=repo)
        self.assertRegex(message, r"^llm-wiki sync: \S+ \d{4}-\d{2}-\d{2}T")
        self.assertEqual(self.git("status", "--porcelain", cwd=repo), "")
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), self.head(repo))

    def test_files_outside_the_wiki_directory_are_untouched(self) -> None:
        parent_repo = self.tmp / "outer"
        parent_repo.mkdir()
        self.git("init", "-q", cwd=parent_repo)
        wiki_dir = parent_repo / "inner"
        wiki_dir.mkdir()
        (parent_repo / "outside.txt").write_text("not mine", encoding="utf-8")
        self.git("remote", "add", "origin", str(self.remote), cwd=parent_repo)
        init_central(wiki_dir, "inner")
        with self.assertRaises(SyncError) as caught:
            sync_wiki(wiki_dir)
        self.assertEqual(caught.exception.code, "guard")
        self.assertEqual(self.git("status", "--porcelain", "--", "outside.txt", cwd=parent_repo), "?? outside.txt")

    # 3: secret before add
    def test_secret_in_new_file_stops_before_add(self) -> None:
        repo = self.seed()
        count = self.commit_count(repo)
        leak = repo / "shared" / "knowledge" / "leak.md"
        leak.write_text(f"key {AWS}\n", encoding="utf-8")
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        error = caught.exception
        self.assertEqual(error.code, "secret_detected")
        self.assertEqual(self.commit_count(repo), count)
        self.assertEqual(self.git("diff", "--cached", "--name-only", cwd=repo), "")
        payload = json.dumps(error.payload)
        self.assertNotIn(AWS, payload)
        self.assertIn("shared/knowledge/leak.md", payload)
        self.assertTrue(error.payload["recovery"])
        self.assertIn("owner_action_required", error.payload)

    # TOCTOU: file changes to a secret between the scan and the add
    def test_secret_introduced_after_scan_is_caught_by_cached_recheck(self) -> None:
        repo = self.seed()
        count = self.commit_count(repo)
        target = repo / "shared" / "knowledge" / "late.md"
        target.write_text("clean at scan time\n", encoding="utf-8")

        def poison() -> None:
            target.write_text(f"now {AWS}\n", encoding="utf-8")

        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo, after_scan=poison)
        self.assertEqual(caught.exception.code, "secret_detected")
        self.assertEqual(self.commit_count(repo), count)
        self.assertEqual(self.git("diff", "--cached", "--name-only", cwd=repo), "")
        self.assertIn(AWS, target.read_text(encoding="utf-8"))  # working tree is left for the owner

    # 10: unpushed commit with a secret
    def test_unpushed_commit_with_secret_blocks_the_push(self) -> None:
        repo = self.seed()
        leak = repo / "shared" / "knowledge" / "old.md"
        leak.write_text(f"{AWS}\n", encoding="utf-8")
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "local leak", cwd=repo)
        remote_before = self.git("rev-parse", "main", cwd=self.remote)
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual(caught.exception.code, "secret_detected")
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), remote_before)
        self.assertTrue(any("history" in line.lower() or "owner" in line.lower() for line in caught.exception.payload["recovery"]))
        self.assertTrue(caught.exception.payload["owner_action_required"])

    def test_first_push_scans_the_whole_history(self) -> None:
        repo = self.clone("m3")
        init_central(repo, "company")
        (repo / "shared" / "knowledge" / "x.md").write_text(f"{AWS}\n", encoding="utf-8")
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual(caught.exception.code, "secret_detected")
        self.assertEqual(self.git("branch", "-r", cwd=repo), "")

    # 5 and 9: push rejected, local HEAD kept, retry succeeds, idempotent
    def test_rejected_push_keeps_local_commit_and_retry_succeeds(self) -> None:
        repo = self.seed()
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho no >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        self.write_session(repo)
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual(caught.exception.code, "push_rejected")
        kept = self.head(repo)
        self.assertNotEqual(self.git("rev-parse", "main", cwd=self.remote), kept)
        hook.unlink()
        result = sync_wiki(repo)
        self.assertFalse(result["committed"])
        self.assertTrue(result["pushed"])
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), kept)

    def test_two_syncs_in_a_row_are_idempotent(self) -> None:
        repo = self.seed()
        self.write_session(repo)
        sync_wiki(repo)
        head = self.head(repo)
        again = sync_wiki(repo)
        self.assertFalse(again["committed"])
        self.assertEqual(self.head(repo), head)

    # 6: both machines wrap, sync merges, log.md union
    def test_two_machines_merge_with_log_union(self) -> None:
        first = self.seed("m3")
        second = self.clone("vm")
        a = self.write_session(first, "from m3")
        b = self.write_session(second, "from vm")
        sync_wiki(first)
        before = read_all(second)
        before_snap = snapshot(second)
        sync_wiki(second)
        sync_wiki(first)
        for repo in (first, second):
            names = {p.name for p in (repo / "shared").rglob("*") if p.is_file()}
            self.assertTrue(any(p.name == a.name for p in repo.rglob("*.md")))
            self.assertTrue(any(p.name == b.name for p in repo.rglob("*.md")))
            log = (repo / "log.md").read_text(encoding="utf-8")
            self.assertIn("from m3", log)
            self.assertIn("from vm", log)
        self.assertEqual(snapshot(first), snapshot(second))
        assert_immutable(before_snap, snapshot(second), second, before, after_union=True)

    # 7: same-file conflict stops the rebase and reports files
    def test_conflict_stops_rebase_and_reports_files(self) -> None:
        first = self.seed("m3")
        second = self.clone("vm")
        target = "shared/knowledge/note.md"
        self.put(first, target, "from first\n")
        sync_wiki(first)
        self.put(second, target, "from second\n")
        with self.assertRaises(SyncError) as caught:
            sync_wiki(second)
        error = caught.exception
        self.assertEqual(error.code, "conflict")
        self.assertEqual(error.payload["conflicts"], [target])
        self.assertTrue(error.payload["rebase_in_progress"])
        self.assertTrue((second / ".git" / "rebase-merge").exists() or (second / ".git" / "rebase-apply").exists())
        self.assertIn("<<<<<<<", (second / target).read_text(encoding="utf-8"))
        # a second call must refuse, not stack another operation on top
        with self.assertRaises(SyncError) as again:
            sync_wiki(second)
        self.assertEqual(again.exception.code, "guard")

    # 11: add/add across machines with the same file name
    def test_add_add_conflict_across_machines_is_reported(self) -> None:
        first = self.seed("m3")
        second = self.clone("vm")
        target = "projects-x/session-20260101-000000-gjc-same.md"
        for repo, text in ((first, "first\n"), (second, "second\n")):
            (repo / "projects-x").mkdir(exist_ok=True)
            (repo / target).write_text(text, encoding="utf-8")
        sync_wiki(first)
        with self.assertRaises(SyncError) as caught:
            sync_wiki(second)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(caught.exception.payload["conflicts"], [target])

    # guards
    def test_not_a_git_repo_is_a_guard_error(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        init_central(plain, "plain")
        with self.assertRaises(SyncError) as caught:
            sync_wiki(plain)
        self.assertEqual(caught.exception.code, "guard")

    def test_missing_remote_is_a_guard_error(self) -> None:
        repo = self.tmp / "local"
        repo.mkdir()
        self.git("init", "-q", cwd=repo)
        init_central(repo, "local")
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual(caught.exception.code, "guard")

    # temp files are never committed
    def test_leftover_temp_files_are_not_committed(self) -> None:
        repo = self.seed()
        (repo / ".llm-wiki-tmp").mkdir(exist_ok=True)
        (repo / ".llm-wiki-tmp" / "stale.tmp").write_text("partial", encoding="utf-8")
        self.write_session(repo)
        sync_wiki(repo)
        self.assertNotIn(".llm-wiki-tmp", self.git("ls-files", cwd=repo))

    # write commands refuse during a rebase
    def test_write_commands_refuse_during_rebase(self) -> None:
        first = self.seed("m3")
        second = self.clone("vm")
        target = "shared/knowledge/note.md"
        self.put(first, target, "a\n")
        sync_wiki(first)
        self.put(second, target, "b\n")
        with self.assertRaises(SyncError):
            sync_wiki(second)
        wiki = self.make_wiki(second)
        with self.assertRaisesRegex(WikiError, "rebase"):
            wrap(wiki, "t", "t", "c", None, None, None, "gjc", None, [], second)
        with self.assertRaisesRegex(WikiError, "rebase"):
            promote(wiki, "t", "b", [], None, True)

    # pull-only
    def test_pull_only_brings_remote_files_and_never_commits_or_pushes(self) -> None:
        first = self.seed("m3")
        second = self.clone("vm")
        self.write_session(first, "fresh")
        sync_wiki(first)
        remote_before = self.git("rev-parse", "main", cwd=self.remote)
        count = self.commit_count(second)
        result = sync_wiki(second, pull_only=True)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["committed"])
        self.assertFalse(result["pushed"])
        self.assertEqual(self.commit_count(second), count + 1)  # fast-forward brought the remote commit
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), remote_before)
        self.assertTrue(any(p.name.endswith("fresh.md") for p in second.rglob("*.md")))

    def test_pull_only_skips_merge_when_dirty_and_says_so(self) -> None:
        first = self.seed("m3")
        second = self.clone("vm")
        self.write_session(first, "fresh")
        sync_wiki(first)
        self.put(second, "shared/knowledge/wip.md", "wip\n")
        head = self.head(second)
        result = sync_wiki(second, pull_only=True)
        self.assertEqual(result["merged"], False)
        self.assertEqual(result["reason"], "dirty")
        self.assertEqual(self.head(second), head)
        self.assertTrue((second / "shared" / "knowledge" / "wip.md").exists())

    def test_pull_only_reports_unreachable_remote_instead_of_failing_hard(self) -> None:
        repo = self.seed()
        self.git("remote", "set-url", "origin", str(self.tmp / "nowhere.git"), cwd=repo)
        result = sync_wiki(repo, pull_only=True)
        self.assertEqual(result["status"], "offline")
        self.assertFalse(result["pushed"])


class RegressionFromReviewTests(GitWikiCase):
    def seeded(self, name: str = "m3") -> Path:
        repo = self.clone(name)
        init_central(repo, "company")
        sync_wiki(repo)
        return repo

    # review P2: invalid UTF-8 must not hide a secret
    def test_secret_in_file_with_invalid_utf8_is_still_caught(self) -> None:
        repo = self.seeded()
        target = repo / "shared" / "knowledge"
        target.mkdir(parents=True, exist_ok=True)
        (target / "x.md").write_bytes(f"token {AWS}\n".encode() + b"\xff\xfe tail\n")
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual(caught.exception.code, "secret_detected")

    def test_real_binary_with_nul_is_skipped(self) -> None:
        repo = self.seeded()
        target = repo / "shared" / "knowledge"
        target.mkdir(parents=True, exist_ok=True)
        (target / "image.bin").write_bytes(b"\x89PNG\x00\x00" + AWS.encode())
        result = sync_wiki(repo)
        self.assertTrue(result["committed"])

    # review P2: rename, deletion and spaces in names
    def test_rename_delete_and_spaced_names_sync_cleanly(self) -> None:
        repo = self.seeded()
        kn = repo / "shared" / "knowledge"
        kn.mkdir(parents=True, exist_ok=True)
        (kn / "old name.md").write_text("one\n", encoding="utf-8")
        (kn / "gone.md").write_text("two\n", encoding="utf-8")
        sync_wiki(repo)
        (kn / "old name.md").rename(kn / "new name.md")
        (kn / "gone.md").unlink()
        result = sync_wiki(repo)
        self.assertTrue(result["committed"])
        self.assertEqual(self.git("status", "--porcelain", cwd=repo), "")
        (kn / "new name.md").write_text(f"{AWS}\n", encoding="utf-8")
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertIn("shared/knowledge/new name.md", caught.exception.payload["paths"])

    # review P3: the recheck reset must really clear the index, also on an unborn branch
    def test_recheck_failure_on_unborn_branch_leaves_index_empty(self) -> None:
        repo = self.clone("m3")
        init_central(repo, "company")
        target = repo / "shared" / "knowledge"
        target.mkdir(parents=True, exist_ok=True)
        late = target / "late.md"
        late.write_text("fine\n", encoding="utf-8")

        def poison() -> None:
            late.write_text(f"{AWS}\n", encoding="utf-8")

        with self.assertRaises(SyncError):
            sync_wiki(repo, after_scan=poison)
        self.assertEqual(self.git("diff", "--cached", "--name-only", cwd=repo), "")
        self.assertEqual(self.git("ls-files", cwd=repo), "")

    # review P3: a machine that cloned the empty remote before the first push must catch up, not loop on push_rejected
    def test_clone_made_before_first_push_catches_up_and_merges_log(self) -> None:
        late = self.clone("vm")  # cloned while the remote was still empty
        first = self.seeded("m3")
        wrap(load_wiki_at(first), "from first", "t", "c", None, None, None, "gjc", None, [], first)
        sync_wiki(first)
        init_central(late, "company")
        wrap(load_wiki_at(late), "from late", "t", "c", None, None, None, "gjc", None, [], late)
        result = sync_wiki(late)
        self.assertTrue(result["pushed"])
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), self.head(late))
        remote_log = self.git("show", "main:log.md", cwd=self.remote)
        self.assertIn("from first", remote_log)
        self.assertIn("from late", remote_log)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "@{u}", cwd=late), "origin/main")

    # review P2: wrap/promote must not be blocked by an unrelated outer repository or a missing git
    def test_outer_repository_merge_does_not_block_a_nested_wiki(self) -> None:
        outer = self.tmp / "project"
        outer.mkdir()
        self.git("init", "-q", cwd=outer)
        (outer / "f.txt").write_text("a\n", encoding="utf-8")
        self.git("add", "-A", cwd=outer)
        self.git("commit", "-q", "-m", "base", cwd=outer)
        (outer / ".git" / "MERGE_HEAD").write_text(self.head(outer) + "\n", encoding="utf-8")
        init_wiki(outer, "single", "demo", False, False)
        wiki = discover(outer)
        path, _ = wrap(wiki, "t", "t", "c", None, None, None, "gjc", None, [], outer)
        self.assertTrue(path.is_file())

    def test_wrap_works_when_git_is_not_installed(self) -> None:
        plain = self.tmp / "nogit"
        plain.mkdir()
        init_wiki(plain, "single", "demo", False, False)
        wiki = discover(plain)
        with patch.dict(os.environ, {"PATH": ""}):
            path, _ = wrap(wiki, "t", "t", "c", None, None, None, "gjc", None, [], plain)
        self.assertTrue(path.is_file())


class IdentityEnvTests(GitWikiCase):
    # review P3: a host-derived automatic identity must not satisfy the identity check
    def test_automatic_identity_is_not_accepted(self) -> None:
        for name in ("user", "email"):
            self.git("config", "--global", "--unset", f"user.{name}", cwd=self.tmp, check=False)
        config = Path(os.environ["GIT_CONFIG_GLOBAL"])
        config.write_text(config.read_text(encoding="utf-8").replace("[user]\n\tname = Test\n\temail = test@example.com\n", ""), encoding="utf-8")
        repo = self.clone("m3")
        init_central(repo, "company")
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual(caught.exception.code, "no_identity")


class CentralCliTests(GitWikiCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_init_central_flag_creates_a_central_layout_in_an_empty_clone(self) -> None:
        repo = self.clone("company")
        code, out = self.run_cli("init", "--central", "--path", str(repo), "--name", "company", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["mode"], "umbrella")
        self.assertTrue((repo / "wiki.toml").is_file())
        self.assertFalse((repo / "AGENTS.md").exists())
        self.assertFalse((repo / "llm-wiki").exists())
        code, _ = self.run_cli("sync", "--root", str(repo), "--json")
        self.assertEqual(code, 0)

    def test_central_conflicts_with_agent_flags(self) -> None:
        repo = self.clone("company")
        code, _ = self.run_cli("init", "--central", "--no-agents", "--path", str(repo), "--json")
        self.assertEqual(code, 2)

    def test_central_refuses_a_nonempty_directory(self) -> None:
        repo = self.clone("company")
        (repo / "notes.txt").write_text("x", encoding="utf-8")
        code, out = self.run_cli("init", "--central", "--path", str(repo), "--json")
        self.assertEqual(code, 1)
        self.assertIn("error", json.loads(out))


class IdentityTests(GitWikiCase):
    identity = False

    def test_missing_identity_stops_before_any_commit(self) -> None:
        repo = self.clone("m3")
        init_central(repo, "company")
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual(caught.exception.code, "no_identity")
        self.assertEqual(self.git("rev-list", "--all", "--count", cwd=repo), "0")
        self.assertEqual(self.git("diff", "--cached", "--name-only", cwd=repo), "")


class CliSyncTests(GitWikiCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_json_success_shape(self) -> None:
        repo = self.clone("m3")
        init_central(repo, "company")
        code, out = self.run_cli("sync", "--root", str(repo), "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data), {"status", "committed", "commit", "pushed"})
        self.assertEqual(data["commit"], self.head(repo))

    def test_json_error_uses_exit_one_and_codes(self) -> None:
        repo = self.clone("m3")
        init_central(repo, "company")
        (repo / "shared" / "knowledge" / "x.md").write_text(AWS, encoding="utf-8")
        code, out = self.run_cli("sync", "--root", str(repo), "--json")
        self.assertEqual(code, 1)
        data = json.loads(out)
        self.assertEqual(data["code"], "secret_detected")
        self.assertIn("error", data)
        self.assertNotIn(AWS, out)

    def test_pull_only_works_in_a_fresh_clone_that_has_no_wiki_toml_yet(self) -> None:
        fresh = self.clone("vm")  # cloned while the remote was still empty
        first = self.clone("m3")
        init_central(first, "company")
        sync_wiki(first)
        code, out = self.run_cli("sync", "--root", str(fresh), "--pull-only", "--json")
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["merged"])
        self.assertTrue((fresh / "wiki.toml").is_file())
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "@{u}", cwd=fresh), "origin/main")
        self.assertEqual(self.head(fresh), self.head(first))

    def test_conflicted_wiki_toml_still_reports_the_rebase_guard(self) -> None:
        early = self.clone("vm")  # cloned while the remote was still empty
        first = self.clone("m3")
        init_central(first, "company")
        sync_wiki(first)
        init_central(early, "company")
        register_project(load_wiki_at(early), "api", None)
        with self.assertRaises(SyncError) as caught:
            sync_wiki(early)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertIn("wiki.toml", caught.exception.payload["conflicts"])
        for argv in (("sync", "--pull-only"), ("sync",)):
            with self.subTest(argv=argv):
                code, out = self.run_cli(*argv, "--root", str(early), "--json")
                self.assertEqual(code, 1, out)
                data = json.loads(out)
                self.assertEqual((data["code"], data["reason"]), ("guard", "rebase_in_progress"))

    def test_pull_only_from_a_mapped_workspace_sets_up_a_fresh_clone(self) -> None:
        fresh = self.clone("vm")
        first = self.clone("m3")
        init_central(first, "company")
        register_project(load_wiki_at(first), "api", None)
        sync_wiki(first)
        workspace = fresh.parent / "ws-api"
        workspace.mkdir()
        config = fresh.parent / "config"
        (config / "llm-wiki").mkdir(parents=True)
        (config / "llm-wiki" / "roots.json").write_text(json.dumps({"version": 1, "roots": {str(workspace): {"wiki": str(fresh), "project": "api"}}}), encoding="utf-8")
        previous = Path.cwd()
        os.chdir(workspace)
        self.addCleanup(os.chdir, previous)
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}):
            os.environ.pop("LLM_WIKI_ROOT", None)
            code, out = self.run_cli("brief", "--json")
            self.assertEqual(code, 1)
            self.assertIn("sync --pull-only", json.loads(out)["error"])
            code, out = self.run_cli("sync", "--pull-only", "--json")
            self.assertEqual(code, 0, out)
            code, out = self.run_cli("status", "--json")
            self.assertEqual((code, json.loads(out)["current_project"]), (0, "api"))

    def test_bad_arguments_stay_exit_two(self) -> None:
        code, out = self.run_cli("sync", "--bogus", "--json")
        self.assertEqual(code, 2)

    def test_pull_only_flag_is_accepted(self) -> None:
        repo = self.clone("m3")
        init_central(repo, "company")
        sync = self.run_cli("sync", "--root", str(repo), "--json")
        code, out = self.run_cli("sync", "--root", str(repo), "--pull-only", "--json")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["pushed"])


class RedactIdempotenceTests(unittest.TestCase):
    def test_redact_is_idempotent(self) -> None:
        from llm_wiki.redact import redact

        samples = [f"token={AWS} and password: hunter2", "Authorization: Basic abcdef==", "Bearer abc.def.ghi", "plain text"]
        for sample in samples:
            once = redact(sample)[0]
            self.assertEqual(redact(once)[0], once, sample)



class CohortGenerationOneTests(SyncCase):
    """Regressions found by the boundary review of the whole change set."""

    def test_records_the_tool_already_redacted_do_not_block_sync(self) -> None:
        from llm_wiki.redact import redact
        from llm_wiki.sync import _pattern_kinds

        samples = ['curl -H "Cookie: sid=abc123" https://x', "<password>hunter2</password>", "password=hunter2", f"key {AWS}", "Authorization: Basic dXNlcjpwYXNz"]
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(_pattern_kinds(sample), ["secret"])
                self.assertEqual(_pattern_kinds(redact(sample)[0]), [])
        repo = self.seed()
        wiki = self.make_wiki(repo)
        wrap(wiki, 'Cookie: sid=abc123 debugging', "task", "done", 'curl -H "Cookie: sid=abc123" https://x', "<password>hunter2</password>", None, "gjc", None, [], repo)
        promote(wiki, "tag", "<password>hunter2</password>", [], None, shared=True)
        result = sync_wiki(repo)
        self.assertTrue(result["pushed"])
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), self.head(repo))
        self.assertNotIn("hunter2", self.git("log", "-p", "main", cwd=self.remote))

    def test_secret_added_only_by_a_merge_commit_blocks_the_push(self) -> None:
        repo = self.seed()
        other = self.clone("vm")
        self.put(other, "shared/knowledge/vm.md", "from vm\n")
        self.git("add", "-A", cwd=other)
        self.git("commit", "-q", "-m", "vm", cwd=other)
        self.git("push", "-q", cwd=other)
        self.put(repo, "shared/knowledge/m3.md", "from m3\n")
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "m3", cwd=repo)
        self.git("fetch", "-q", cwd=repo)
        self.git("merge", "-q", "--no-commit", "--no-ff", "origin/main", cwd=repo)
        self.put(repo, "shared/knowledge/merged.md", f"{AWS}\n")
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "merge", cwd=repo)
        self.assertEqual(self.git("status", "--porcelain", cwd=repo), "")
        remote_before = self.git("rev-parse", "main", cwd=self.remote)
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual(caught.exception.code, "secret_detected")
        self.assertIn("shared/knowledge/merged.md", caught.exception.payload["paths"])
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), remote_before)

    def test_unreadable_history_blob_stops_instead_of_skipping_it(self) -> None:
        repo = self.seed()
        self.put(repo, "shared/knowledge/lost.md", "content that cannot be read back\n")
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "local", cwd=repo)
        blob = self.git("rev-parse", "HEAD:shared/knowledge/lost.md", cwd=repo)
        (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        remote_before = self.git("rev-parse", "main", cwd=self.remote)
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual((caught.exception.code, caught.exception.payload["command"]), ("git_failed", "show"))
        self.assertEqual(self.git("rev-parse", "main", cwd=self.remote), remote_before)

    def test_rebase_guards_pass_only_when_git_is_missing(self) -> None:
        from llm_wiki.sync import require_finished_rebase

        repo = self.seed()
        wiki = self.make_wiki(repo)
        before = snapshot(repo)
        stalled = SyncError("git_timeout", "git rev-parse timed out after 120s.", command="rev-parse")
        with patch("llm_wiki.sync.owns_repository", side_effect=stalled):
            with self.assertRaises(SyncError) as caught:
                wrap(wiki, "t", "t", "c", None, None, None, "gjc", None, [], repo)
            self.assertEqual(caught.exception.code, "git_timeout")
            with self.assertRaises(SyncError):
                require_finished_rebase(repo)
        self.assertEqual(snapshot(repo), before)
        with self.assertRaises(SyncError) as caught:
            require_finished_rebase(self.tmp / "missing")
        self.assertEqual((caught.exception.code, caught.exception.payload["reason"]), ("guard", "missing_directory"))


class CohortGenerationTwoTests(SyncCase):
    """Regressions found by the second boundary review generation."""

    REDACTED_SHAPES = [
        '{"password":"x","user":"y"}',
        'f(token="x",y=1)',
        "token=abc",
        'curl -H "Cookie: sid=abc123" https://x',
        "<password>hunter2</password>",
        "password=hunter2.",
        "see password=hunter2, then go",
    ]

    def test_redaction_is_idempotent_on_the_shapes_the_tool_writes(self) -> None:
        from llm_wiki.redact import redact
        from llm_wiki.sync import _pattern_kinds

        for sample in self.REDACTED_SHAPES:
            once = redact(sample)[0]
            for written in (once, json.dumps(once, ensure_ascii=False), f"reason: {json.dumps(once)}\nnext: 1"):
                with self.subTest(sample=sample, written=written):
                    self.assertEqual(redact(written)[0], written)
                    self.assertEqual(_pattern_kinds(written), [])
        for real in ("password=[REDACTED_SECRET]hunter2", '{"password":"hunter2"}', 'f(token="x",y=1)', f"key={AWS}"):
            with self.subTest(real=real):
                self.assertEqual(_pattern_kinds(real), ["secret"])

    def test_redacted_json_and_decision_reasons_still_sync(self) -> None:
        from llm_wiki.curate import decide

        repo = self.seed()
        wiki = self.make_wiki(repo)
        wrap(wiki, "json", "task", "done", 'POST {"password":"hunter2","user":"y"} and f(token="abc",y=1)', None, None, "gjc", None, [], repo)
        candidate = promote(wiki, "c", "body", [], None, shared=True)
        decide(wiki, str(candidate), "rejected", 'bob token=abc', "token=abc leaked", )
        result = sync_wiki(repo)
        self.assertTrue(result["pushed"])
        remote = self.git("log", "-p", "main", cwd=self.remote)
        self.assertNotIn("hunter2", remote)
        self.assertNotIn("token=abc", remote)

    def test_index_blob_failure_stops_before_commit(self) -> None:
        import llm_wiki.sync as sync_module

        repo = self.seed()
        self.put(repo, "shared/knowledge/staged.md", "fine\n")
        count = self.commit_count(repo)
        real_blob = sync_module._blob

        def failing(directory: Path, spec: str) -> bytes:
            if spec.startswith(":"):
                raise SyncError("git_failed", "git show failed; the content could not be checked for secrets.", command="show")
            return real_blob(directory, spec)

        with patch("llm_wiki.sync._blob", side_effect=failing), self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual((caught.exception.code, caught.exception.payload["command"]), ("git_failed", "show"))
        self.assertEqual(self.commit_count(repo), count)

    def test_blob_read_timeout_is_reported_as_git_timeout(self) -> None:
        import subprocess as real_subprocess

        repo = self.seed()
        self.put(repo, "shared/knowledge/slow.md", "fine\n")
        count = self.commit_count(repo)
        original = real_subprocess.run

        def slow_show(argv, *args, **kwargs):
            if argv[:2] == ["git", "show"]:
                raise real_subprocess.TimeoutExpired(argv, 120)
            return original(argv, *args, **kwargs)

        with patch("llm_wiki.sync.subprocess.run", side_effect=slow_show), self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual((caught.exception.code, caught.exception.payload["command"]), ("git_timeout", "show"))
        self.assertEqual(self.commit_count(repo), count)

    def test_a_file_turned_into_a_symlink_is_scanned(self) -> None:
        repo = self.seed()
        target = self.put(repo, "shared/knowledge/link.md", "plain\n")
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "file", cwd=repo)
        self.git("push", "-q", cwd=repo)
        target.unlink()
        os.symlink(f"/tmp/{AWS}", target)
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "type change", cwd=repo)
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual(caught.exception.code, "secret_detected")
        self.assertIn("shared/knowledge/link.md", caught.exception.payload["paths"])


class CohortGenerationThreeTests(SyncCase):
    """Regressions found by the third boundary review generation."""

    def test_records_the_tool_writes_from_separate_redacted_fields_still_sync(self) -> None:
        from llm_wiki.curate import decide
        from llm_wiki.handoff import take

        repo = self.seed()
        wiki = self.make_wiki(repo)
        # Each field is redacted alone; a key at the end of one field must not read the next field
        # (title -> body, log heading -> link line) or the closing quote of its json.dumps header.
        promote(wiki, "Where to find the DB password:", "In the vault", [], None, shared=True)
        first = promote(wiki, "plain", "body", [], None, shared=True)
        decide(wiki, str(first), "rejected", "bot password:", "needs GITHUB_TOKEN=")
        second = promote(wiki, "plain2", "body", [], None, shared=True)
        ghp = "ghp_" + "a" * 36
        decide(wiki, str(second), "rejected", "bob", f'dup of curl -H "Cookie: sid=abc" and set "GITHUB_TOKEN={ghp}" see "token=eyJa.b.c"')
        session, _ = wrap(wiki, "set GITHUB_TOKEN=", "t", "c", None, "next", None, "bot password:", None, [], repo, handoff_file=True)
        take(wiki, None, session.stem, "bot password:")
        third = promote(wiki, "plain3", "body", [], None, shared=True)
        decide(wiki, str(third), "rejected", "bob", "dup of curl -H 'Cookie: sid=abc'")
        from llm_wiki.sync import _pattern_kinds

        for path in sorted(p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts):
            with self.subTest(path=path.relative_to(repo).as_posix()):
                self.assertEqual(_pattern_kinds(path.read_text(encoding="utf-8")), [])
        self.assertTrue(sync_wiki(repo)["pushed"])
        self.assertNotIn(ghp, self.git("log", "-p", "main", cwd=self.remote))

    def test_text_glued_to_a_placeholder_is_still_a_secret(self) -> None:
        from llm_wiki.sync import _pattern_kinds

        for text in (
            '{"env":"GITHUB_TOKEN=[REDACTED_TOKEN]\\nDB_PASS=hunter2"}',
            'password=[REDACTED_AWS_ACCESS_KEY]",hunter2',
            "Cookie: [REDACTED_AUTH]; auth=hunter2",
            "password=[REDACTED_SECRET]\\nhunter2",
        ):
            with self.subTest(text=text):
                self.assertEqual(_pattern_kinds(text), ["secret"])

    def test_documented_pass_throughs(self) -> None:
        from llm_wiki.redact import redact

        # Same as the first pass on password="x",hunter2: text after a whole masked value is kept.
        self.assertEqual(redact("password=[REDACTED_SECRET],hunter2")[0], "password=[REDACTED_SECRET],hunter2")
        # Masking is line-local: a value that starts on the next line is not read (README).
        self.assertEqual(redact("password:\n  hunter2")[0], "password:\n  hunter2")
        self.assertEqual(redact("Bearer\nabc")[0], "Bearer\nabc")

    def test_header_values_are_joined_before_they_are_redacted(self) -> None:
        from llm_wiki.records import _clean_single

        self.assertEqual(_clean_single("needs GITHUB_TOKEN=\nhunter2"), "needs GITHUB_TOKEN= [REDACTED_SECRET]")

    def test_a_staged_file_turned_into_a_symlink_is_scanned(self) -> None:
        repo = self.seed()
        target = self.put(repo, "shared/knowledge/link.md", "plain\n")
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-q", "-m", "file", cwd=repo)
        self.git("push", "-q", cwd=repo)
        count = self.commit_count(repo)
        target.unlink()
        os.symlink(f"/tmp/{AWS}", target)
        with self.assertRaises(SyncError) as caught:
            sync_wiki(repo)
        self.assertEqual(caught.exception.code, "secret_detected")
        self.assertIn("shared/knowledge/link.md", caught.exception.payload["paths"])
        self.assertEqual(self.commit_count(repo), count)


class CohortGenerationFourTests(SyncCase):
    """Regressions found by the fourth boundary review generation."""

    def test_any_whitespace_inside_a_line_still_separates_a_key_and_its_value(self) -> None:
        from llm_wiki.redact import redact

        for raw in ("password:\xa0hunter2", "api_key =\u3000hunter2", "secret:\x0bhunter2", "Authorization: Bearer\xa0hunter2", "Authorization: Basic\xa0hunter2"):
            with self.subTest(raw=raw):
                self.assertNotIn("hunter2", redact(raw)[0])

    def test_crlf_lines_end_like_lf_lines(self) -> None:
        from llm_wiki.sync import _pattern_kinds

        for text in ("Cookie: [REDACTED_AUTH]\r\nx\r\n", 'token=[REDACTED_TOKEN]"\r\n', "password=[REDACTED_SECRET]\r\n"):
            with self.subTest(text=text):
                self.assertEqual(_pattern_kinds(text), [])
        for text in ("password=hunter2\r\n", "Cookie: [REDACTED_AUTH]\rauth=hunter2\n"):
            with self.subTest(text=text):
                self.assertEqual(_pattern_kinds(text), ["secret"])

    def test_tags_and_session_keys_split_across_fields_are_not_secrets(self) -> None:
        from llm_wiki.curate import decide
        from llm_wiki.redact import redact
        from llm_wiki.sync import _pattern_kinds

        repo = self.seed()
        wiki = self.make_wiki(repo)
        promote(wiki, "Use the <password> element", "Close it with </password>.", [], None, shared=True)
        candidate = promote(wiki, "plain", "body", [], None, shared=True)
        decide(wiki, str(candidate), "rejected", "bot sid=", "needs SESSION_ID=")
        from llm_wiki.handoff import take

        session, _ = wrap(wiki, "t", "t", "c", None, "next", None, "bot sid=", None, [], repo, handoff_file=True)
        take(wiki, None, session.stem, "bot sid=")
        promote(wiki, "Parse the <secret> element", "b", ["see <token>", "and </token>"], None, shared=True)
        promote(wiki, "Handle a missing </secret> tag", "b", [], None, shared=True)
        for path in sorted(p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts):
            with self.subTest(path=path.relative_to(repo).as_posix()):
                self.assertEqual(_pattern_kinds(path.read_text(encoding="utf-8")), [])
        self.assertEqual(redact("<password>hunter2</password>")[0], "<password>[REDACTED_SECRET]</password>")

    def test_fuzzed_fields_written_like_the_tool_are_fixed_points(self) -> None:
        import random

        from llm_wiki.records import _clean, _clean_single
        from llm_wiki.redact import redact

        atoms = ["password", "token", "api_key", "secret", "xsid", "Cookie", "Bearer", "Authorization: Basic", ":", "=", ":=", "=>",
                 " ", "\n", "\t", '"', "'", "\\", ",", ";", ")", "]", "}", ".", "[REDACTED_SECRET]", "[REDACTED_TOKEN]", "[REDACTED_AUTH]",
                 "ghp_" + "a" * 36, AWS, "eyJa.b.c", "x", "hunter2", "-H ", "\r", "\xa0", "redis://u:p@h", "<password>", "</password>",
                 "<password", ">", "\x1b", "\x08", "\x01", "\x7f", "earer x", "pi_key=v", "://u:p@h", "sid", " = "]
        rng = random.Random(5)
        field = lambda: "".join(rng.choice(atoms) for _ in range(rng.randint(1, 8)))
        bad = []
        for _ in range(3000):
            header, body = _clean_single(field()), _clean(field())
            other = _clean_single(field())
            for written in (
                f"reason: {json.dumps(header, ensure_ascii=False)}\nnext: 1\n",
                f"# {header}\n\n{body}\n",
                f"\n## [t] promote | demo | {header}\n\n- `x`\n\n## [t] promote | demo | {other}\n\n- `y`\n",
                f"## Sources\n\n- {header}\n- {other}\n",
                f"# {header}\r\n\r\n{body}\r\n",
            ):
                if redact(written)[0] != written:
                    bad.append(written)
        self.assertEqual(bad[:3], [])


class CohortGenerationFiveTests(SyncCase):
    """Regressions found by the fifth boundary review generation."""

    def test_tag_delimiters_split_across_lines_are_not_tags(self) -> None:
        from llm_wiki.redact import redact
        from llm_wiki.sync import _pattern_kinds

        for text in ("<password\n>x</password>", "<password>x</password\n>", "<password\r\n>x</password>"):
            with self.subTest(text=text):
                self.assertEqual(redact(text)[0], text)
                self.assertEqual(_pattern_kinds(text), [])

    def test_header_control_characters_do_not_become_secrets_when_escaped(self) -> None:
        from llm_wiki.curate import decide
        from llm_wiki.handoff import take
        from llm_wiki.sync import _pattern_kinds

        repo = self.seed()
        wiki = self.make_wiki(repo)
        promote(wiki, "Use <password", ">x</password>", [], None, shared=True)
        candidate = promote(wiki, "plain", "body", [], None, shared=True)
        decide(wiki, str(candidate), "rejected", "\x08earer me", "\x1bearer done")
        session, _ = wrap(wiki, "t", "t", "c", None, "next", None, "\x1api_key=v", None, [], repo, handoff_file=True)
        take(wiki, None, session.stem, "\x01://u:p@h")
        for path in sorted(p for p in repo.rglob("*") if p.is_file() and ".git" not in p.parts):
            with self.subTest(path=path.relative_to(repo).as_posix()):
                self.assertEqual(_pattern_kinds(path.read_text(encoding="utf-8")), [])

    def test_a_lone_carriage_return_separates_a_key_and_its_value(self) -> None:
        from llm_wiki.redact import redact

        for raw in ("api_key:\rhunter2", "Bearer\rhunter2", "password\r=\rhunter2"):
            with self.subTest(raw=raw):
                self.assertNotIn("hunter2", redact(raw)[0])


class CohortGenerationSixTests(SyncCase):
    """Regressions found by the sixth boundary review generation."""

    def test_session_keys_allow_space_around_the_equals_sign(self) -> None:
        from llm_wiki.records import _clean_single
        from llm_wiki.redact import redact
        from llm_wiki.sync import _pattern_kinds

        for raw, masked in (
            ("sid= hunter2", "sid= [REDACTED_TOKEN]"),
            ("mysid = hunter2 ok", "mysid = [REDACTED_TOKEN] ok"),
            ("x-session-id =\thunter2", "x-session-id =\t[REDACTED_TOKEN]"),
            ("xsid\r=\rhunter2", "xsid\r=\r[REDACTED_TOKEN]"),
            ("mysid=abc", "mysid=[REDACTED_TOKEN]"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(redact(raw)[0], masked)
                self.assertEqual(redact(masked)[0], masked)
        for kept in ("sid = [REDACTED_TOKEN]", "sidebar = keep", "sid =\nhunter2"):
            with self.subTest(kept=kept):
                self.assertEqual(redact(kept)[0], kept)
                self.assertEqual(_pattern_kinds(kept), [])
        for raw in ("sid=\x1bhunter2", "xsid=\nhunter2", "x-session-id\t=\x00hunter2"):
            with self.subTest(single=raw):
                self.assertNotIn("hunter2", _clean_single(raw))


if __name__ == "__main__":
    unittest.main()
