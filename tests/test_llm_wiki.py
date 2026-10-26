from __future__ import annotations

import json
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from llm_wiki import cli
from llm_wiki.config import WikiError, add_project, current_project, discover, is_legacy_dir
from llm_wiki.init import init_wiki, register_project
from llm_wiki.qmd import collection_name, qmd_search, qmd_sync, run_command
from llm_wiki.records import promote, wrap
from llm_wiki.redact import redact
from llm_wiki.search import search


@contextmanager
def in_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class LlmWikiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def collection_list(names: set[str]) -> str:
        details = "\n".join(
            f"\x1b[36m{name}\x1b[0m (qmd://{name}/)\n"
            "  Pattern: **/*.md\n"
            "  Files: 1\n"
            "  Updated: just now"
            for name in sorted(names)
        )
        return f"\x1b[1mCollections ({len(names)}):\x1b[0m\n{details}\n"

    def test_single_init_is_idempotent_and_replaces_agents_marker(self) -> None:
        init_wiki(self.root, "single", None, True, True)
        agents = self.root / "AGENTS.md"
        agents.write_text("before\n<!-- llm-wiki:begin -->\nold\n<!-- llm-wiki:end -->\nafter\n", encoding="utf-8")
        result = init_wiki(self.root, "single", None, True, True)
        content = agents.read_text(encoding="utf-8")
        self.assertIn("before", content)
        self.assertIn("after", content)
        self.assertNotIn("old", content)
        self.assertEqual(content.count("<!-- llm-wiki:begin -->"), 1)
        self.assertIn(str((self.root / "llm-wiki" / "wiki.toml").resolve()), result["existing"])
        self.assertTrue((self.root / "llm-wiki" / "projects" / self.root.name / "knowledge").is_dir())

    def test_init_allows_root_with_index(self) -> None:
        (self.root / "index.md").write_text("site index", encoding="utf-8")
        init_wiki(self.root, "single", None, False, False)
        self.assertTrue((self.root / "llm-wiki" / "wiki.toml").is_file())

    def test_init_allows_non_wiki_ancestor_with_legacy_markers(self) -> None:
        ancestor = self.root / "ancestor"
        root = ancestor / "child"
        root.mkdir(parents=True)
        (ancestor / "index.md").write_text("site index", encoding="utf-8")
        (ancestor / "SCHEMA.md").write_text("site schema", encoding="utf-8")
        init_wiki(root, "single", None, False, False)
        self.assertTrue((root / "llm-wiki" / "wiki.toml").is_file())

    def test_umbrella_init_registers_git_children(self) -> None:
        for name in ("alpha", "beta"):
            (self.root / name / ".git").mkdir(parents=True)
        (self.root / "plain").mkdir()
        result = init_wiki(self.root, "umbrella", "workspace", False, False)
        self.assertEqual(result["projects"], {"alpha": "alpha", "beta": "beta"})
        self.assertEqual(discover(self.root).mode, "umbrella")

    def test_umbrella_init_skips_invalid_git_children(self) -> None:
        for name in ("alpha", "shared", ".hidden"):
            (self.root / name / ".git").mkdir(parents=True)
        result = init_wiki(self.root, "umbrella", "workspace", False, False)
        self.assertEqual(result["projects"], {"alpha": "alpha"})
        self.assertEqual({entry["name"] for entry in result["skipped"]}, {"shared", ".hidden"})
        self.assertEqual(discover(self.root).projects, {"alpha": "alpha"})
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["status", "--root", str(self.root)]), 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "umbrella")

    def test_discovery_nearest_wins_and_longest_project_path(self) -> None:
        init_wiki(self.root, "umbrella", "top", False, False)
        wiki = discover(self.root)
        register_project(wiki, "outer", "apps")
        register_project(discover(self.root), "inner", "apps/service")
        nested = self.root / "apps" / "service" / "src"
        nested.mkdir(parents=True)
        self.assertEqual(current_project(discover(self.root), nested), "inner")
        init_wiki(self.root / "apps" / "service", "single", "local", False, False)
        self.assertEqual(discover(nested).name, "local")

    def test_specific_project_path_beats_root_path(self) -> None:
        init_wiki(self.root, "umbrella", "top", False, False)
        wiki = add_project(discover(self.root), "zz-root", ".")
        wiki = add_project(wiki, "aa-specific", "a")
        self.assertEqual(current_project(wiki, self.root / "a"), "aa-specific")

    def test_add_project_updates_config_and_layout(self) -> None:
        init_wiki(self.root, "umbrella", "top", False, False)
        updated, note = register_project(discover(self.root), "api-v2", "services/api")
        self.assertIsNone(note)
        self.assertEqual(updated.projects["api-v2"], "services/api")
        self.assertTrue((self.root / "llm-wiki" / "projects" / "api-v2" / "inbox" / "candidates").is_dir())
        self.assertIn("api-v2", (self.root / "llm-wiki" / "index.md").read_text(encoding="utf-8"))

    def test_text_search_scopes_current_project_and_shared(self) -> None:
        for name in ("a", "b"):
            (self.root / name / ".git").mkdir(parents=True)
        init_wiki(self.root, "umbrella", "top", False, False)
        wiki = discover(self.root)
        (wiki.directory / "projects" / "a" / "knowledge" / "a.md").write_text("needle alpha", encoding="utf-8")
        (wiki.directory / "projects" / "b" / "knowledge" / "b.md").write_text("needle beta", encoding="utf-8")
        (wiki.directory / "shared" / "knowledge" / "shared.md").write_text("needle shared", encoding="utf-8")
        cwd = self.root / "a"
        results, engine, _ = search(wiki, "needle", cwd, engine="text")
        self.assertEqual(engine, "text")
        paths = {entry["path"] for entry in results}
        self.assertIn("llm-wiki/projects/a/knowledge/a.md", paths)
        self.assertIn("llm-wiki/shared/knowledge/shared.md", paths)
        self.assertNotIn("llm-wiki/projects/b/knowledge/b.md", paths)
        all_results, _, _ = search(wiki, "needle", cwd, all_projects=True, engine="text")
        self.assertIn("llm-wiki/projects/b/knowledge/b.md", {entry["path"] for entry in all_results})

    def test_qmd_sync_and_search_fake_runner(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        wiki = discover(self.root)
        commands: list[list[str]] = []
        existing: set[str] = set()

        def runner(command: list[str], timeout: int):
            commands.append(command)
            if command[1:3] == ["collection", "list"]:
                return subprocess.CompletedProcess(command, 0, self.collection_list(existing), "")
            if command[1:3] == ["collection", "add"]:
                existing.add(command[command.index("--name") + 1])
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[1] == "search":
                collection = command[command.index("-c") + 1]
                return subprocess.CompletedProcess(command, 0, json.dumps([{"file": f"qmd://{collection}/q.md", "score": 4, "snippet": "needle"}]), "")
            return subprocess.CompletedProcess(command, 0, "", "")

        synced = qmd_sync(wiki, runner=runner, executable="qmd")
        self.assertIn(["qmd", "collection", "add", str(wiki.directory / "shared"), "--name", collection_name(wiki, "shared"), "--mask", "**/*.md"], synced)
        self.assertTrue(any(command[:3] == ["qmd", "context", "add"] for command in synced))
        commands.clear()
        qmd_sync(wiki, runner=runner, executable="qmd")
        self.assertFalse(any(command[1:3] == ["collection", "add"] for command in commands))
        results, engine, note = search(wiki, "needle", self.root, engine="auto", runner=runner, qmd_available=True)
        self.assertEqual((engine, note), ("qmd", None))
        self.assertEqual(results[0]["score"], 4.0)
        self.assertEqual(results[0]["path"], "llm-wiki/shared/q.md")
        self.assertEqual(results[0]["snippets"], ["needle"])
        fallback, engine, note = search(wiki, "needle", self.root, engine="auto", qmd_available=False)
        self.assertEqual(engine, "text")
        self.assertIsNone(note)
        self.assertEqual(fallback, [])

    def test_qmd_search_unexpected_shape_falls_back_in_auto(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        wiki = discover(self.root)
        names = {collection_name(wiki, "shared"), collection_name(wiki, "demo")}

        def runner(command: list[str], timeout: int):
            if command[1:3] == ["collection", "list"]:
                return subprocess.CompletedProcess(command, 0, self.collection_list(names), "")
            return subprocess.CompletedProcess(command, 0, json.dumps({"results": []}), "")

        with self.assertRaisesRegex(WikiError, "Unexpected qmd search response"):
            qmd_search("needle", ["shared"], wiki, 10, runner)
        results, engine, note = search(wiki, "needle", self.root, engine="auto", runner=runner, qmd_available=True)
        self.assertEqual((results, engine), ([], "text"))
        self.assertIn("QMD failed; used text search", note)

    def test_qmd_collection_name_collision_is_an_error(self) -> None:
        init_wiki(self.root, "umbrella", "top", False, False)
        wiki = add_project(discover(self.root), "a.b", "one")
        wiki = add_project(wiki, "a-b", "two")

        def runner(command: list[str], timeout: int):
            return subprocess.CompletedProcess(command, 0, self.collection_list(set()), "")

        with self.assertRaisesRegex(WikiError, "QMD collection name collision"):
            qmd_sync(wiki, runner=runner, executable="qmd")

    def test_qmd_sync_reports_existing_collection_add_error(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        wiki = discover(self.root)

        def runner(command: list[str], timeout: int):
            if command[1:3] == ["collection", "list"]:
                return subprocess.CompletedProcess(command, 0, self.collection_list(set()), "")
            if command[1:3] == ["collection", "add"]:
                return subprocess.CompletedProcess(command, 1, "", "Collection 'x' already exists.")
            return subprocess.CompletedProcess(command, 0, "", "")

        with self.assertRaisesRegex(WikiError, "Collection 'x' already exists"):
            qmd_sync(wiki, runner=runner, executable="qmd")

    def test_run_command_wraps_runner_errors(self) -> None:
        for error in (OSError("not found"), subprocess.SubprocessError("failed")):
            with patch("llm_wiki.qmd.subprocess.run", side_effect=error):
                with self.assertRaisesRegex(WikiError, str(error)):
                    run_command(["qmd"])

    def test_cli_reports_qmd_errors_without_traceback(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        output, errors = io.StringIO(), io.StringIO()
        with patch("llm_wiki.cli.qmd_sync", side_effect=WikiError("qmd failed")), redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(cli.main(["qmd-sync", "--root", str(self.root), "--json"]), 1)
        self.assertEqual(json.loads(output.getvalue()), {"error": "qmd failed"})
        self.assertEqual(errors.getvalue(), "")

    def test_redaction_covers_sample_secrets(self) -> None:
        text, count = redact("password=hunter2 Bearer abcdefghijklmnop AKIA1234567890ABCDEF eyJaaa.bbb.ccc https://u:p@example.com")
        self.assertGreaterEqual(count, 5)
        for secret in ("hunter2", "abcdefghijklmnop", "AKIA1234567890ABCDEF", "eyJaaa.bbb.ccc", "u:p@"):
            self.assertNotIn(secret, text)

    def test_wrap_writes_session_log_and_candidate(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        wiki = discover(self.root)
        path, candidates = wrap(wiki, "topic", "password=hunter2", "done", "checked", None, None, "gjc", None, ["possible fact"], self.root)
        self.assertTrue(path.exists())
        self.assertEqual(len(candidates), 1)
        self.assertNotIn("hunter2", path.read_text(encoding="utf-8"))
        self.assertIn("wrap | demo | topic", (wiki.directory / "log.md").read_text(encoding="utf-8"))

    def test_promote_status_and_does_not_touch_knowledge(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        wiki = discover(self.root)
        plain = promote(wiki, "plain", "body", [], "demo")
        evidence = promote(wiki, "evidence", "body", ["code.py:1"], "demo")
        self.assertIn("status: needs-review", plain.read_text(encoding="utf-8"))
        self.assertIn("status: candidate-with-evidence", evidence.read_text(encoding="utf-8"))
        self.assertEqual(list((wiki.directory / "projects" / "demo" / "knowledge").iterdir()), [])

    def test_legacy_wiki_is_read_only(self) -> None:
        legacy_root = self.root / "legacy"
        (legacy_root / "llm-wiki").mkdir(parents=True)
        (legacy_root / "llm-wiki" / "index.md").write_text("needle", encoding="utf-8")
        wiki = discover(legacy_root)
        self.assertTrue(wiki.legacy)
        results, engine, _ = search(wiki, "needle", legacy_root, engine="text")
        self.assertEqual(engine, "text")
        self.assertEqual(len(results), 1)
        with self.assertRaises(WikiError):
            promote(wiki, "title", "body", [], None)

    def test_init_refuses_legacy_without_creating_files(self) -> None:
        legacy = self.root / "llm-wiki"
        legacy.mkdir()
        (legacy / "index.md").write_text("legacy", encoding="utf-8")
        with self.assertRaises(WikiError):
            init_wiki(self.root, "single", None, True, True)
        self.assertFalse((legacy / "wiki.toml").exists())
        self.assertFalse((self.root / "AGENTS.md").exists())
        self.assertFalse((self.root / ".gjc").exists())

    def test_init_refuses_case_insensitive_legacy_dir_without_creating_files(self) -> None:
        legacy = self.root / "LLM-Wiki"
        (legacy / "sub").mkdir(parents=True)
        (legacy / "index.md").write_text("legacy", encoding="utf-8")
        self.assertTrue(is_legacy_dir(legacy))
        original = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with self.assertRaises(WikiError):
            init_wiki(legacy / "sub", "single", None, True, True)
        self.assertEqual(
            {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()},
            original,
        )

    def test_init_refuses_inside_legacy_without_changes(self) -> None:
        legacy = self.root / "legacy" / "llm-wiki"
        (legacy / "sub").mkdir(parents=True)
        (legacy / "index.md").write_text("legacy", encoding="utf-8")
        original = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for root in (legacy, legacy / "sub"):
            with self.assertRaises(WikiError):
                init_wiki(root, "single", None, True, True)
            self.assertEqual(
                {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()},
                original,
            )

    def test_init_refuses_inside_new_wiki_without_changes(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        nested = self.root / "llm-wiki" / "projects" / "demo" / "knowledge"
        original = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(WikiError, "Cannot initialize inside an existing wiki directory"):
            init_wiki(nested, "single", None, True, True)
        self.assertEqual(
            {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()},
            original,
        )

    def test_reinit_mode_mismatch_reports_existing_mode(self) -> None:
        init_wiki(self.root, "umbrella", "workspace", False, False)
        result = init_wiki(self.root, "single", "workspace", False, False)
        self.assertEqual(result["mode"], "umbrella")
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["note"], "Existing wiki mode is umbrella; requested single.")

    def test_reinit_without_mode_keeps_existing_mode_without_note(self) -> None:
        init_wiki(self.root, "umbrella", "workspace", False, False)
        result = init_wiki(self.root, None, "workspace", False, False)
        self.assertEqual(result["mode"], "umbrella")
        self.assertNotIn("note", result)

    def test_init_without_mode_creates_single_wiki(self) -> None:
        result = init_wiki(self.root, None, None, False, False)
        self.assertEqual(result["mode"], "single")
        self.assertEqual(discover(self.root).mode, "single")

    def test_init_agents_and_gjc_are_idempotent(self) -> None:
        first = init_wiki(self.root, "single", "demo", True, True)
        agents = self.root / "AGENTS.md"
        rule = self.root / ".gjc" / "rules" / "llm-wiki.md"
        initial_agents, initial_rule = agents.read_bytes(), rule.read_bytes()
        result = init_wiki(self.root, "single", "demo", True, True)
        self.assertEqual(agents.read_bytes(), initial_agents)
        self.assertEqual(rule.read_bytes(), initial_rule)
        self.assertNotIn(str(agents), result["created"])
        self.assertNotIn(str(rule), result["created"])
        self.assertIn(str(agents.resolve()), result["existing"])
        self.assertIn(str(rule.resolve()), result["existing"])
        self.assertEqual(rule.read_text(encoding="utf-8").count("---\n"), 2)
        self.assertIn(str(agents.resolve()), first["created"])

    def test_add_project_preserves_custom_index_content(self) -> None:
        init_wiki(self.root, "umbrella", "top", False, False)
        index_path = self.root / "llm-wiki" / "index.md"
        index_path.write_text(index_path.read_text(encoding="utf-8") + "\nCustom line.\n", encoding="utf-8")
        updated, note = register_project(discover(self.root), "api", "api")
        self.assertIsNone(note)
        self.assertEqual(updated.projects["api"], "api")
        content = index_path.read_text(encoding="utf-8")
        self.assertIn("Custom line.", content)
        self.assertIn("api", content)

    def test_project_validation_and_paths(self) -> None:
        init_wiki(self.root, "umbrella", "top", False, False)
        wiki = discover(self.root)
        for name in (".", "..", "shared", "Shared"):
            with self.assertRaises(WikiError):
                add_project(wiki, name, "path")
        wiki = add_project(wiki, "Api", "services/api")
        with self.assertRaises(WikiError):
            add_project(wiki, "api", "other")
        with self.assertRaises(WikiError):
            add_project(wiki, "other", "/tmp/other")
        with self.assertRaises(WikiError):
            add_project(wiki, "other", "services/api")

    def test_add_project_refuses_single_mode(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        with self.assertRaisesRegex(WikiError, "single mode"):
            add_project(discover(self.root), "other", "other")

    def test_redaction_new_patterns_and_cookie_prose(self) -> None:
        private, _ = redact("-----BEGIN PRIVATE KEY-----\nsecret")
        self.assertEqual(private, "[REDACTED_PRIVATE_KEY]")
        text, _ = redact(
            '"password": "x y" \'token\'=\'x y\' AWS_SECRET_ACCESS_KEY=value '
            "sk_live_abc xoxb-token AIza" + "a" * 35 + "\nAuthorization: Basic dXNlcjpwYXNz"
        )
        for secret in ("x y", "value", "sk_live_abc", "xoxb-token", "AIza" + "a" * 35, "dXNlcjpwYXNz"):
            self.assertNotIn(secret, text)
        prose, _ = redact("A cookie is a small piece of text.")
        self.assertEqual(prose, "A cookie is a small piece of text.")

    def test_record_single_line_fields_and_wrap_collision(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        wiki = discover(self.root)
        with patch("llm_wiki.records._now") as now:
            from datetime import datetime, timezone
            now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
            first, _ = wrap(wiki, "topic\nstatus: stable", "task", "done", None, None, None, "agent\nx", None, [], self.root)
            second, _ = wrap(wiki, "topic\nstatus: stable", "task", "done", None, None, None, "agent\nx", None, [], self.root)
        self.assertNotEqual(first, second)
        self.assertIn("# topic status: stable", first.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
