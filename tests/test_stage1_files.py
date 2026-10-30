from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from llm_wiki.config import Wiki, WikiError, discover, load_wiki_at
from llm_wiki.init import init_central, init_wiki
from llm_wiki.records import create_exclusive, promote, wrap, replace_atomic

from gitwiki import assert_immutable, read_all, snapshot


class CreateExclusiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name).resolve()
        self.tmpdir = self.dir / ".llm-wiki-tmp"

    def test_creates_file_and_leaves_no_temp(self) -> None:
        target = self.dir / "a" / "one.md"
        create_exclusive(target, "hello", self.tmpdir)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello")
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_existing_file_is_never_overwritten(self) -> None:
        target = self.dir / "one.md"
        target.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            create_exclusive(target, "new", self.tmpdir)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_target_never_visible_partially(self) -> None:
        target = self.dir / "big.md"
        payload = "x" * 2_000_000
        seen: list[int] = []
        done = threading.Event()

        def watch() -> None:
            while not done.is_set():
                try:
                    seen.append(target.stat().st_size)
                except FileNotFoundError:
                    pass

        thread = threading.Thread(target=watch)
        thread.start()
        create_exclusive(target, payload, self.tmpdir)
        done.set()
        thread.join()
        self.assertTrue(all(size == len(payload) for size in seen), set(seen))

    def test_concurrent_creates_have_exactly_one_winner(self) -> None:
        target = self.dir / "race.md"
        results: list[str] = []
        lock = threading.Lock()

        def run(value: str) -> None:
            try:
                create_exclusive(target, value, self.tmpdir)
                outcome = "won"
            except FileExistsError:
                outcome = "lost"
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=run, args=(str(i),)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("won"), 1)
        self.assertEqual(results.count("lost"), 7)

    def test_replace_atomic_swaps_content_and_cleans_up(self) -> None:
        target = self.dir / "c.md"
        target.write_text("old", encoding="utf-8")
        replace_atomic(target, "new", self.tmpdir)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertEqual(list(self.tmpdir.iterdir()), [])


class RecordRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        init_wiki(self.root, "single", "demo", False, False)
        self.wiki = discover(self.root)

    def test_same_second_wraps_make_distinct_files(self) -> None:
        paths: list[Path] = []
        lock = threading.Lock()

        def run() -> None:
            path, _ = wrap(self.wiki, "same topic", "t", "c", None, None, None, "gjc", None, [], self.root)
            with lock:
                paths.append(path)

        threads = [threading.Thread(target=run) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(paths)), 6)
        for path in paths:
            self.assertTrue(path.is_file())

    def test_same_second_promotes_make_distinct_files(self) -> None:
        fixed = datetime(2026, 1, 2, 3, 4, 5).astimezone()
        paths: list[Path] = []
        lock = threading.Lock()

        def run() -> None:
            path = promote(self.wiki, "same title", "body", [], "demo", now=fixed)
            with lock:
                paths.append(path)

        threads = [threading.Thread(target=run) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(set(paths)), 6)
        self.assertEqual(len({path.read_text(encoding="utf-8") for path in paths}), 1)

    def test_log_keeps_every_entry_under_concurrency(self) -> None:
        threads = [threading.Thread(target=lambda: wrap(self.wiki, "log topic", "t", "c", None, None, None, "gjc", None, [], self.root)) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((self.wiki.directory / "log.md").read_text(encoding="utf-8").count("] wrap | demo |"), 5)


class InitLayoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_init_writes_gitattributes_and_gitignore(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        directory = self.root / "llm-wiki"
        self.assertEqual((directory / ".gitattributes").read_text(encoding="utf-8").splitlines(), ["log.md merge=union"])
        self.assertEqual((directory / ".gitignore").read_text(encoding="utf-8").splitlines(), [".llm-wiki-tmp/"])

    def test_init_preserves_existing_gitattributes_without_duplicates(self) -> None:
        directory = self.root / "llm-wiki"
        directory.mkdir()
        (directory / ".gitattributes").write_text("*.png binary\n", encoding="utf-8")
        (directory / ".gitignore").write_text("node_modules/\n.llm-wiki-tmp/\n", encoding="utf-8")
        init_wiki(self.root, "single", "demo", False, False)
        init_wiki(self.root, "single", "demo", False, False)
        self.assertEqual((directory / ".gitattributes").read_text(encoding="utf-8").splitlines(), ["*.png binary", "log.md merge=union"])
        self.assertEqual((directory / ".gitignore").read_text(encoding="utf-8").splitlines(), ["node_modules/", ".llm-wiki-tmp/"])

    def test_init_still_refuses_git_dir_named_like_wiki_without_config(self) -> None:
        (self.root / "llm-wiki" / ".git").mkdir(parents=True)
        with self.assertRaisesRegex(WikiError, "git repository"):
            init_wiki(self.root, "single", "demo", False, False)

    def test_init_allows_git_dir_that_already_has_wiki_toml(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        (self.root / "llm-wiki" / ".git").mkdir()
        result = init_wiki(self.root, "single", "demo", False, False)
        self.assertIn(str(self.root / "llm-wiki" / "wiki.toml"), result["existing"])


class CentralInitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def test_central_init_in_empty_git_clone_creates_layout_without_managed_files(self) -> None:
        clone = self.base / "company"
        (clone / ".git").mkdir(parents=True)
        result = init_central(clone, "company")
        for name in ("wiki.toml", "index.md", "log.md", ".gitattributes", ".gitignore", "shared/knowledge", "shared/inbox/candidates"):
            self.assertTrue((clone / name).exists(), name)
        self.assertFalse((clone / "AGENTS.md").exists())
        self.assertFalse((clone / ".gjc").exists())
        self.assertFalse((clone / "README.md").exists())
        self.assertEqual(result["mode"], "umbrella")

    def test_central_init_refuses_nonempty_unconfigured_git_dir(self) -> None:
        clone = self.base / "company"
        (clone / ".git").mkdir(parents=True)
        (clone / "notes.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(WikiError, "empty"):
            init_central(clone, "company")

    def test_central_init_is_idempotent(self) -> None:
        clone = self.base / "company"
        (clone / ".git").mkdir(parents=True)
        init_central(clone, "company")
        before = snapshot(clone)
        again = init_central(clone, "company")
        self.assertEqual(snapshot(clone), before)
        self.assertIn(str(clone / "wiki.toml"), again["existing"])

    def test_central_project_dirs_come_from_add_project(self) -> None:
        clone = self.base / "company"
        (clone / ".git").mkdir(parents=True)
        init_central(clone, "company")
        wiki = load_wiki_at(clone)
        self.assertEqual(wiki.directory, clone)
        self.assertEqual(wiki.root, clone.parent)
        self.assertEqual(wiki.mode, "umbrella")


class LoadWikiAtTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()

    def test_loads_any_directory_name_that_has_wiki_toml(self) -> None:
        clone = self.base / "company"
        clone.mkdir()
        init_central(clone, "company")
        wiki = load_wiki_at(clone)
        self.assertEqual(wiki.directory, clone)
        self.assertEqual(wiki.name, "company")

    def test_missing_wiki_toml_is_an_error(self) -> None:
        clone = self.base / "plain"
        clone.mkdir()
        with self.assertRaisesRegex(WikiError, "wiki.toml"):
            load_wiki_at(clone)

    def test_root_flag_and_env_load_directly_when_wiki_toml_present(self) -> None:
        clone = self.base / "company"
        clone.mkdir()
        init_central(clone, "company")
        self.assertEqual(discover(clone).directory, clone)
        previous = os.environ.get("LLM_WIKI_ROOT")
        os.environ["LLM_WIKI_ROOT"] = str(clone)
        try:
            self.assertEqual(discover().directory, clone)
        finally:
            if previous is None:
                os.environ.pop("LLM_WIKI_ROOT", None)
            else:
                os.environ["LLM_WIKI_ROOT"] = previous

    def test_cwd_upward_walk_keeps_the_name_check(self) -> None:
        clone = self.base / "company"
        clone.mkdir()
        init_central(clone, "company")
        inner = clone / "shared"
        with self.assertRaisesRegex(WikiError, "No llm-wiki found"):
            discover(inner / "knowledge")

    def test_unconfigured_start_still_falls_back_to_upward_search(self) -> None:
        init_wiki(self.base, "single", "demo", False, False)
        nested = self.base / "deep" / "er"
        nested.mkdir(parents=True)
        self.assertEqual(discover(nested).directory, self.base / "llm-wiki")


class ImmutabilityHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name).resolve()
        (self.dir / "projects/p/sessions/2026/01").mkdir(parents=True)
        (self.dir / "projects/p/inbox/candidates").mkdir(parents=True)
        (self.dir / "projects/p/handoffs").mkdir(parents=True)
        self.session = self.dir / "projects/p/sessions/2026/01/s.md"
        self.session.write_text("session\n", encoding="utf-8")
        self.candidate = self.dir / "projects/p/inbox/candidates/c.md"
        self.candidate.write_text("---\nstatus: needs-review\n---\n\n# T\n\nbody\n", encoding="utf-8")
        self.log = self.dir / "log.md"
        self.log.write_text("# Log\n\n## a\n", encoding="utf-8")
        self.before = snapshot(self.dir)
        self.old = read_all(self.dir)

    def judge(self, union: bool = False) -> None:
        assert_immutable(self.before, snapshot(self.dir), self.dir, self.old, after_union=union)

    def test_allows_log_append_new_file_and_candidate_front_matter_change(self) -> None:
        self.log.write_text(self.log.read_text(encoding="utf-8") + "## b\n", encoding="utf-8")
        (self.dir / "projects/p/sessions/2026/01/new.md").write_text("n", encoding="utf-8")
        self.candidate.write_text("---\nstatus: accepted\ndecided_by: owner\n---\n\n# T\n\nbody\n", encoding="utf-8")
        self.judge()

    def test_rejects_candidate_body_change(self) -> None:
        self.candidate.write_text("---\nstatus: needs-review\n---\n\n# T\n\nbodY\n", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "candidate body"):
            self.judge()

    def test_rejects_session_modification(self) -> None:
        self.session.write_text("session!\n", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "immutable"):
            self.judge()

    def test_rejects_log_middle_insertion(self) -> None:
        self.log.write_text("# Log\n\n## inserted\n\n## a\n", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "append-only"):
            self.judge()

    def test_union_merge_may_interleave_but_not_lose_lines(self) -> None:
        self.log.write_text("# Log\n\n## other-machine\n\n## a\n", encoding="utf-8")
        self.judge(union=True)
        self.log.write_text("# Log\n", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "lost lines"):
            self.judge(union=True)

    def test_rejects_removed_file(self) -> None:
        self.session.unlink()
        with self.assertRaisesRegex(AssertionError, "removed"):
            self.judge()


if __name__ == "__main__":
    unittest.main()
