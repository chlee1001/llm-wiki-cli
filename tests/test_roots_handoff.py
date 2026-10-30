from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from llm_wiki import cli
from llm_wiki.config import WikiError, discover_for, load_wiki_at, read_roots
from llm_wiki.handoff import handoff_state, scan_handoffs, take
from llm_wiki.init import init_central, register_project
from llm_wiki.records import wrap
from llm_wiki.search import search

from gitwiki import read_all, snapshot


@contextmanager
def in_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        patcher = patch.dict(os.environ, {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config")})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("LLM_WIKI_ROOT", None)
        self.clone = self.base / "company"
        self.clone.mkdir()
        init_central(self.clone, "company")
        self.wiki = load_wiki_at(self.clone)
        register_project(self.wiki, "api", None)
        register_project(load_wiki_at(self.clone), "web", None)
        self.wiki = load_wiki_at(self.clone)
        self.workspace = self.base / "work" / "api"
        self.workspace.mkdir(parents=True)

    @property
    def roots_file(self) -> Path:
        return self.home / ".config" / "llm-wiki" / "roots.json"

    def write_roots(self, roots: dict, version: int = 1, extra: dict | None = None) -> None:
        self.roots_file.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": version, "roots": roots}
        data.update(extra or {})
        self.roots_file.write_text(json.dumps(data), encoding="utf-8")

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, json.loads(out.getvalue())


class RootsTests(Base):
    def test_maps_a_workspace_to_clone_and_project(self) -> None:
        self.write_roots({str(self.workspace): {"wiki": str(self.clone), "project": "api"}})
        wiki, project = discover_for(self.workspace / "src", None)
        self.assertEqual(wiki.directory, self.clone)
        self.assertEqual(project, "api")

    def test_longest_component_prefix_wins(self) -> None:
        other = self.base / "work" / "api" / "web"
        other.mkdir(parents=True)
        self.write_roots({
            str(self.workspace): {"wiki": str(self.clone), "project": "api"},
            str(other): {"wiki": str(self.clone), "project": "web"},
        })
        self.assertEqual(discover_for(other / "deep", None)[1], "web")
        self.assertEqual(discover_for(self.workspace, None)[1], "api")

    def test_prefix_is_matched_by_path_component_not_string(self) -> None:
        sibling = self.base / "work" / "apix"
        sibling.mkdir(parents=True)
        self.write_roots({str(self.workspace): {"wiki": str(self.clone), "project": "api"}})
        with self.assertRaisesRegex(WikiError, "No llm-wiki found"):
            discover_for(sibling, None)

    def test_root_flag_and_env_outrank_roots_json(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        init_central(elsewhere, "elsewhere")
        self.write_roots({str(self.workspace): {"wiki": str(self.clone), "project": "api"}})
        self.assertEqual(discover_for(self.workspace, elsewhere)[0].directory, elsewhere)
        with patch.dict(os.environ, {"LLM_WIKI_ROOT": str(elsewhere)}):
            self.assertEqual(discover_for(self.workspace, None)[0].directory, elsewhere)

    def test_roots_json_outranks_upward_search(self) -> None:
        from llm_wiki.init import init_wiki

        init_wiki(self.workspace, "single", "api", False, False)
        self.write_roots({str(self.workspace): {"wiki": str(self.clone), "project": "api"}})
        self.assertEqual(discover_for(self.workspace, None)[0].directory, self.clone)

    def test_falls_back_to_upward_search_without_a_mapping(self) -> None:
        from llm_wiki.init import init_wiki

        init_wiki(self.workspace, "single", "api", False, False)
        wiki, project = discover_for(self.workspace, None)
        self.assertEqual(wiki.directory, self.workspace / "llm-wiki")
        self.assertIsNone(project)

    def test_unregistered_project_is_refused_without_writing(self) -> None:
        self.write_roots({str(self.workspace): {"wiki": str(self.clone), "project": "ghost"}})
        before = snapshot(self.clone)
        with self.assertRaisesRegex(WikiError, "등록되지 않은 프로젝트: ghost; add-project를 먼저 실행"):
            discover_for(self.workspace, None)
        self.assertEqual(snapshot(self.clone), before)

    def test_clone_name_does_not_need_to_be_llm_wiki(self) -> None:
        self.write_roots({str(self.workspace): {"wiki": str(self.clone), "project": "api"}})
        self.assertNotEqual(self.clone.name.casefold(), "llm-wiki")
        self.assertEqual(discover_for(self.workspace, None)[0].name, "company")

    def test_tilde_is_expanded_in_keys_and_wiki(self) -> None:
        home_clone = self.home / "company"
        self.clone.rename(home_clone)
        mapped = self.home / "work" / "api"
        mapped.mkdir(parents=True)
        self.write_roots({"~/work/api": {"wiki": "~/company", "project": "api"}})
        self.assertEqual(discover_for(mapped, None)[0].directory, home_clone)

    def test_invalid_files_are_clear_errors(self) -> None:
        cases = {
            "broken json": "{not json",
            "not an object": "[]",
            "unknown top key": json.dumps({"version": 1, "roots": {}, "extra": 1}),
            "wrong version": json.dumps({"version": 2, "roots": {}}),
            "relative key": json.dumps({"version": 1, "roots": {"rel/path": {"wiki": str(self.clone), "project": "api"}}}),
            "relative wiki": json.dumps({"version": 1, "roots": {str(self.workspace): {"wiki": "rel", "project": "api"}}}),
            "unknown entry key": json.dumps({"version": 1, "roots": {str(self.workspace): {"wiki": str(self.clone), "project": "api", "x": 1}}}),
            "missing project": json.dumps({"version": 1, "roots": {str(self.workspace): {"wiki": str(self.clone)}}}),
        }
        for label, text in cases.items():
            self.roots_file.parent.mkdir(parents=True, exist_ok=True)
            self.roots_file.write_text(text, encoding="utf-8")
            with self.subTest(label), self.assertRaises(WikiError):
                read_roots()

    def test_an_unready_clone_does_not_break_other_mappings(self) -> None:
        fresh = self.base / "fresh-clone"
        fresh.mkdir()  # cloned from an empty remote: no wiki.toml yet
        other = self.base / "work" / "other"
        other.mkdir(parents=True)
        self.write_roots({
            str(self.workspace): {"wiki": str(self.clone), "project": "api"},
            str(other): {"wiki": str(fresh), "project": "api"},
        })
        self.assertEqual(discover_for(self.workspace, None)[1], "api")
        with self.assertRaisesRegex(WikiError, "wiki.toml"):
            discover_for(other, None)

    def test_a_mapping_to_a_missing_directory_only_fails_when_it_is_the_one_used(self) -> None:
        other = self.base / "work" / "other"
        other.mkdir(parents=True)
        self.write_roots({
            str(self.workspace): {"wiki": str(self.clone), "project": "api"},
            str(other): {"wiki": str(self.base / "gone"), "project": "api"},
        })
        self.assertEqual(discover_for(self.workspace, None)[1], "api")
        with self.assertRaisesRegex(WikiError, "does not exist"):
            discover_for(other, None)

    def test_duplicate_keys_after_normalization_are_refused(self) -> None:
        self.write_roots({
            str(self.workspace): {"wiki": str(self.clone), "project": "api"},
            str(self.workspace) + "/": {"wiki": str(self.clone), "project": "web"},
        })
        with self.assertRaisesRegex(WikiError, "duplicate"):
            read_roots()

    def test_missing_file_means_no_mapping(self) -> None:
        self.assertEqual(read_roots(), {})

    def test_cli_wrap_uses_mapped_project_and_not_cwd_relative_to_root(self) -> None:
        self.write_roots({str(self.workspace): {"wiki": str(self.clone), "project": "api"}})
        with in_directory(self.workspace):
            code, data = self.run_cli("wrap", "--topic", "mapped", "--task", "t", "--completed", "c", "--json")
        self.assertEqual(code, 0)
        self.assertIn("/projects/api/sessions/", data["path"])
        self.assertTrue(Path(data["path"]).is_file())

    def test_cli_promote_and_search_follow_the_mapping(self) -> None:
        self.write_roots({str(self.workspace): {"wiki": str(self.clone), "project": "api"}})
        with in_directory(self.workspace):
            code, promoted = self.run_cli("promote", "--title", "mapped note", "--body", "zebra-needle", "--json")
            self.assertEqual(code, 0)
            self.assertIn("/projects/api/inbox/candidates/", promoted["path"])
            code, found = self.run_cli("search", "zebra-needle", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(len(found["results"]), 1)

    def test_cli_explicit_project_flag_still_overrides_mapping(self) -> None:
        self.write_roots({str(self.workspace): {"wiki": str(self.clone), "project": "api"}})
        with in_directory(self.workspace):
            code, data = self.run_cli("wrap", "--topic", "t", "--task", "t", "--completed", "c", "--project", "web", "--json")
        self.assertEqual(code, 0)
        self.assertIn("/projects/web/sessions/", data["path"])

    def test_cli_status_reports_mapped_project(self) -> None:
        self.write_roots({str(self.workspace): {"wiki": str(self.clone), "project": "api"}})
        with in_directory(self.workspace):
            code, data = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(data["current_project"], "api")

    def test_cli_unmapped_directory_and_no_wiki_is_an_error(self) -> None:
        stray = self.base / "stray"
        stray.mkdir()
        with in_directory(stray):
            code, data = self.run_cli("status", "--json")
        self.assertEqual(code, 1)
        self.assertIn("error", data)


class HandoffTests(Base):
    def make_session(self, topic: str = "topic", handoff: str | None = "do the next thing") -> Path:
        path, _ = wrap(self.wiki, topic, "t", "c", None, handoff, None, "gjc", "api", [], self.workspace, handoff_file=True)
        return path

    def test_wrap_with_handoff_writes_a_linked_handoff_file(self) -> None:
        session = self.make_session()
        handoff = session.parent.parent.parent.parent / "handoffs" / f"{session.stem}.md"
        self.assertTrue(handoff.is_file())
        text = handoff.read_text(encoding="utf-8")
        self.assertNotIn("status:", text)
        self.assertIn("do the next thing", text)
        self.assertIn(session.relative_to(self.clone).as_posix(), text)
        session_text = session.read_text(encoding="utf-8")
        self.assertIn("## Handoff", session_text)
        self.assertIn("do the next thing", session_text)
        self.assertIn(handoff.relative_to(self.clone).as_posix(), session_text)

    def test_no_handoff_file_when_text_is_none_or_flag_absent(self) -> None:
        none_session = self.make_session("none", None)
        self.assertFalse((none_session.parent.parent.parent.parent / "handoffs").exists())
        plain, _ = wrap(self.wiki, "plain", "t", "c", None, "something", None, "gjc", "api", [], self.workspace)
        self.assertFalse((plain.parent.parent.parent.parent / "handoffs" / f"{plain.stem}.md").exists())

    def test_none_dot_text_makes_no_file(self) -> None:
        session = self.make_session("dot", "None.")
        self.assertFalse((session.parent.parent.parent.parent / "handoffs").exists())

    def test_open_state_until_taken(self) -> None:
        session = self.make_session()
        state = handoff_state(self.wiki, "api")
        self.assertEqual([item["id"] for item in state["open"]], [session.stem])
        take(self.wiki, "api", session.stem, "claude")
        state = handoff_state(self.wiki, "api")
        self.assertEqual(state["open"], [])
        self.assertEqual([item["id"] for item in state["taken"]], [session.stem])

    def test_take_adds_a_file_and_leaves_every_existing_file_untouched(self) -> None:
        session = self.make_session()
        before = read_all(self.clone)
        before_snap = snapshot(self.clone)
        taken = take(self.wiki, "api", session.stem, "claude code")
        after = snapshot(self.clone)
        for relative, digest in before_snap.items():
            self.assertEqual(after[relative], digest, relative)
        added = set(after) - set(before_snap)
        self.assertEqual({Path(p).name for p in added if "handoffs" in p}, {taken.name})
        self.assertRegex(taken.name, rf"^{session.stem}\.taken\.claude-code\.[a-z0-9-]+$")
        self.assertNotEqual(read_all(self.clone), {})

    def test_same_agent_cannot_take_twice(self) -> None:
        session = self.make_session()
        take(self.wiki, "api", session.stem, "claude")
        with self.assertRaisesRegex(WikiError, rf"이미 가져간 인수인계입니다: {session.stem} \(claude\)"):
            take(self.wiki, "api", session.stem, "claude")

    def test_other_agent_may_take_and_it_is_flagged_as_duplicate(self) -> None:
        session = self.make_session()
        take(self.wiki, "api", session.stem, "claude")
        take(self.wiki, "api", session.stem, "codex")
        state = handoff_state(self.wiki, "api")
        self.assertEqual([item["id"] for item in state["taken"]], [session.stem])
        self.assertTrue(state["taken"][0]["duplicate_take"])
        self.assertEqual(sorted(state["taken"][0]["agents"]), ["claude", "codex"])

    def test_unknown_id_and_unsafe_ids_are_refused(self) -> None:
        for bad in ("nope", "../x", "a/b", ""):
            with self.subTest(bad), self.assertRaises(WikiError):
                take(self.wiki, "api", bad, "claude")

    def test_unknown_project_is_refused(self) -> None:
        session = self.make_session()
        with self.assertRaisesRegex(WikiError, "Unknown project"):
            take(self.wiki, "ghost", session.stem, "claude")

    def test_concurrent_takes_by_the_same_agent_have_one_winner(self) -> None:
        session = self.make_session()
        outcomes: list[str] = []
        lock = threading.Lock()

        def run() -> None:
            try:
                take(self.wiki, "api", session.stem, "claude")
                result = "won"
            except WikiError:
                result = "lost"
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=run) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("won"), 1)

    def test_secret_in_handoff_text_is_redacted_in_both_files(self) -> None:
        key = "AKIA" + "ABCDEFGHIJKLMNOP"
        session = self.make_session("secret", f"use {key} later")
        handoff = session.parent.parent.parent.parent / "handoffs" / f"{session.stem}.md"
        self.assertNotIn(key, handoff.read_text(encoding="utf-8"))
        self.assertNotIn(key, session.read_text(encoding="utf-8"))

    def test_search_does_not_index_handoff_files_twice(self) -> None:
        self.make_session("needle topic", "findable-handoff-phrase")
        results, _, _ = search(self.wiki, "findable-handoff-phrase", self.workspace, "api", False, "text", 10)
        self.assertEqual(len(results), 1)
        self.assertIn("/sessions/", results[0]["path"])

    def test_cli_wrap_handoff_flag_and_take(self) -> None:
        self.write_roots({str(self.workspace): {"wiki": str(self.clone), "project": "api"}})
        with in_directory(self.workspace):
            code, data = self.run_cli("wrap", "--topic", "cli", "--task", "t", "--completed", "c", "--handoff", "carry on", "--json")
            self.assertEqual(code, 0)
            self.assertTrue(data["handoff"].endswith(".md"))
            session_id = Path(data["path"]).stem
            code, taken = self.run_cli("take", session_id, "--agent", "claude", "--json")
            self.assertEqual(code, 0)
            self.assertIn(".taken.claude.", taken["path"])
            code, again = self.run_cli("take", session_id, "--agent", "claude", "--json")
        self.assertEqual(code, 1)
        self.assertIn("이미 가져간 인수인계입니다", again["error"])

    def write_roots(self, roots: dict) -> None:
        self.roots_file.parent.mkdir(parents=True, exist_ok=True)
        self.roots_file.write_text(json.dumps({"version": 1, "roots": roots}), encoding="utf-8")



class SharedHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clone = Path(self.temp.name).resolve() / "company"
        self.clone.mkdir()
        init_central(self.clone, "company")
        register_project(load_wiki_at(self.clone), "api", None)
        self.wiki = load_wiki_at(self.clone)

    def test_a_shared_handoff_can_be_taken(self) -> None:
        session, _ = wrap(self.wiki, "shared work", "t", "c", None, "next step", None, "gjc", None, [], self.clone, handoff_file=True)
        handoff_id = session.stem
        source = self.clone / "shared" / "handoffs" / session.name
        self.assertTrue(source.is_file())
        before = source.read_bytes()
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["take", handoff_id, "--agent", "claude", "--root", str(self.clone), "--json"])
        self.assertEqual(code, 0, out.getvalue())
        marker = Path(json.loads(out.getvalue())["path"])
        self.assertEqual(marker.parent, source.parent)
        self.assertEqual(source.read_bytes(), before)
        state = scan_handoffs(source.parent, self.clone)
        self.assertEqual((state["open"], [entry["id"] for entry in state["taken"]]), ([], [handoff_id]))
        with self.assertRaisesRegex(WikiError, "이미 가져간"):
            take(self.wiki, None, handoff_id, "claude")

    def test_take_marker_redacts_the_agent_name(self) -> None:
        session, _ = wrap(self.wiki, "w", "t", "c", None, "next", None, "gjc", "api", [], self.clone, handoff_file=True)
        secret = "AKIA" + "ABCDEFGHIJKLMNOP"
        marker = take(self.wiki, "api", session.stem, f"bot {secret}")
        self.assertNotIn(secret, marker.read_text(encoding="utf-8"))
        self.assertNotIn(secret, marker.name)


    def test_a_mapped_project_still_finds_a_shared_handoff(self) -> None:
        session, _ = wrap(self.wiki, "shared work", "t", "c", None, "next", None, "gjc", None, [], self.clone, handoff_file=True)
        marker = take(self.wiki, "api", session.stem, "gjc")
        self.assertEqual(marker.parent, self.clone / "shared" / "handoffs")

    def test_a_project_handoff_wins_over_a_shared_one_with_the_same_id(self) -> None:
        shared = self.clone / "shared" / "handoffs"
        project = self.clone / "projects" / "api" / "handoffs"
        for directory in (shared, project):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "same-id.md").write_text("# x\n", encoding="utf-8")
        self.assertEqual(take(self.wiki, "api", "same-id", "gjc").parent, project)
        self.assertEqual(take(self.wiki, None, "same-id", "gjc").parent, shared)


if __name__ == "__main__":
    unittest.main()
