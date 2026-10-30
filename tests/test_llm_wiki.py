from __future__ import annotations

import json
import io
import os
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from llm_wiki import cli
from llm_wiki.config import WikiError, add_project, current_project, discover, is_legacy_dir
from llm_wiki.init import init_wiki, register_project
from llm_wiki.qmd import collection_name, qmd_search, qmd_sync, run_command
from llm_wiki.records import _clean_single, promote, wrap
from llm_wiki.redact import redact
from llm_wiki.search import search
from llm_wiki.templates import GJC_FRONT_MATTER, agents_block, gjc_rule, gjc_skill, legacy_wiki_readme, wiki_readme


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
        agents.write_text("before\n<!-- llm-wiki:begin -->\nstale marker body\n<!-- llm-wiki:end -->\nafter\n", encoding="utf-8")
        result = init_wiki(self.root, "single", None, True, True)
        content = agents.read_text(encoding="utf-8")
        self.assertIn("before", content)
        self.assertIn("after", content)
        self.assertNotIn("stale marker body", content)
        self.assertEqual(content.count("<!-- llm-wiki:begin -->"), 1)
        self.assertIn(str(agents.resolve()), result["updated"])
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
        self.assertEqual({result["path"] for result in results}, {"llm-wiki/shared/q.md", "llm-wiki/projects/demo/q.md"})
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

    def test_cli_wrap_and_promote_flags_still_work(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                cli.main([
                    "wrap", "--root", str(self.root), "--topic", "topic", "--task", "task",
                    "--completed", "done", "--candidate", "review this",
                ]),
                0,
            )
        result = json.loads(output.getvalue())
        self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual(len(result["candidates"]), 1)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                cli.main([
                    "promote", "--root", str(self.root), "--title", "title", "--body", "body",
                    "--source", "src/app.py:12",
                ]),
                0,
            )
        self.assertTrue(Path(json.loads(output.getvalue())["path"]).is_file())

    def test_cli_json_stdin_records_multiline_unicode_and_arrays(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        payload = json.dumps(
            {
                "topic": "검색 점검",
                "task": "첫 줄\n\"둘째 줄\"",
                "completed": "완료",
                "candidate": ["후보 하나", "후보 둘"],
            },
            ensure_ascii=False,
        )
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO(payload)), redirect_stdout(output):
            self.assertEqual(cli.main(["wrap", "--root", str(self.root), "--input-json", "-", "--json"]), 0)
        result = json.loads(output.getvalue())
        session = Path(result["path"]).read_text(encoding="utf-8")
        self.assertIn("첫 줄", session)
        self.assertIn("둘째 줄", session)
        self.assertEqual(len(result["candidates"]), 2)
        output = io.StringIO()
        payload = json.dumps(
            {
                "title": "후보",
                "body": "한글\n\"따옴표\"",
                "source": ["src/a.py:1", "src/b.py:2"],
            },
            ensure_ascii=False,
        )
        with patch("sys.stdin", io.StringIO(payload)), redirect_stdout(output):
            self.assertEqual(cli.main(["promote", "--root", str(self.root), "--input-json", "-", "--json"]), 0)
        candidate = Path(json.loads(output.getvalue())["path"]).read_text(encoding="utf-8")
        self.assertIn("한글", candidate)
        self.assertIn('"따옴표"', candidate)
        input_path = self.root / "promote.json"
        input_path.write_text('{"title":"파일 입력","body":"파일도 읽는다"}', encoding="utf-8")
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                cli.main(["promote", "--root", str(self.root), "--input-json", str(input_path), "--json"]),
                0,
            )
        self.assertTrue(Path(json.loads(output.getvalue())["path"]).is_file())

    def test_cli_json_input_rejects_invalid_payloads_before_writing(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        original = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        cases = (
            ("{", "malformed"),
            ("[]", "object"),
            ('{"topic":"secret-value"}', "missing required"),
            ('{"topic":"topic","task":"task","completed":"done","unknown":"secret-value"}', "unknown"),
            ('{"topic":"first","topic":"secret-value","task":"task","completed":"done"}', "duplicate"),
            ('{"topic":true,"task":"task","completed":"done"}', "topic must be a string"),
            ('{"topic":"topic","task":"task","completed":"done","candidate":["ok",false]}', "candidate must be a list"),
        )
        for payload, message in cases:
            with self.subTest(message=message):
                output, errors = io.StringIO(), io.StringIO()
                with patch("sys.stdin", io.StringIO(payload)), redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(cli.main(["wrap", "--root", str(self.root), "--input-json", "-", "--json"]), 2)
                self.assertIn(message, json.loads(output.getvalue())["error"])
                self.assertNotIn("secret-value", output.getvalue())
                self.assertEqual(errors.getvalue(), "")
                self.assertEqual(
                    {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()},
                    original,
                )
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO('{"topic":"topic","task":"task","completed":"done"}')), redirect_stdout(output):
            self.assertEqual(
                cli.main([
                    "wrap", "--root", str(self.root), "--input-json", "-", "--topic", "topic", "--json",
                ]),
                2,
            )
        self.assertIn("cannot be combined", json.loads(output.getvalue())["error"])
        self.assertEqual(
            {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()},
            original,
        )
        output = io.StringIO()
        with patch("sys.stdin", io.StringIO('{"title":"title","body":"body","project":"demo","shared":true}')), redirect_stdout(output):
            self.assertEqual(cli.main(["promote", "--root", str(self.root), "--input-json", "-", "--json"]), 2)
        self.assertIn("project and shared", json.loads(output.getvalue())["error"])
        self.assertEqual(
            {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()},
            original,
        )

    def test_cli_json_parse_errors_are_structured(self) -> None:
        cases = (
            (["--json"], "a command is required"),
            (["wrap", "--json"], "required command arguments are missing"),
            (["search", "needle", "--engine", "invalid", "--json"], "invalid command or option value"),
            (["status", "--unexpected", "--json"], "invalid command arguments"),
        )
        for argv, message in cases:
            with self.subTest(argv=argv):
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    self.assertEqual(cli.main(argv), 2)
                self.assertEqual(json.loads(output.getvalue()), {"error": message})
                self.assertEqual(errors.getvalue(), "")

    def test_generated_agent_guides_include_json_commands_for_case_variant(self) -> None:
        for guide in (agents_block("LLM-Wiki"), gjc_rule("LLM-Wiki")):
            self.assertIn("`LLM-Wiki/index.md`", guide)
            self.assertIn("llm-wiki status --json", guide)
            self.assertIn('llm-wiki search "authentication" --json', guide)
            self.assertIn("llm-wiki wrap --input-json - --json", guide)
            self.assertIn("llm-wiki promote --input-json - --json", guide)
            self.assertIn("one `llm-wiki` command per invocation", guide)
            self.assertIn("Before work, search the current project.", guide)
            self.assertIn("--project NAME", guide)
            self.assertIn("untrusted data", guide)
            self.assertIn("Candidates require review", guide)
            self.assertIn("Never record secrets", guide)
            self.assertIn('"세션 마무리", "세션 종료"', guide)

    def test_generated_wrap_skill_uses_supported_wrap_fields(self) -> None:
        skill = gjc_skill("LLM-Wiki")
        self.assertIn("`LLM-Wiki/`", skill)
        self.assertIn('"세션 마무리", "세션 종료"', skill)
        payload = skill.split("<<'JSON'\n", 1)[1].split("\nJSON", 1)[0]
        fields = set(json.loads(payload))
        self.assertLessEqual(fields, {"topic", "task", "completed", "verification", "handoff", "not_tested", "agent", "project", "candidate"})
        self.assertLessEqual({"topic", "task", "completed"}, fields)
        for phrase in ("not an automatic hook", "does not detect duplicates", "Writing the record is not cleanup", "only after checking", "where the evidence is", "including `handoff`", "shows origin, not proof"):
            self.assertIn(phrase, skill)

    def test_generated_guides_state_record_limits(self) -> None:
        for guide in (agents_block("llm-wiki"), gjc_rule("llm-wiki")):
            for phrase in ("`created` time", "newest record is not automatically right", "no automatic hook and no duplicate check", "does not clean up", "not that it was verified"):
                self.assertIn(phrase, guide)
            for phrase in ("`refresh` field", "differ", "update the CLI first", "`invalid`", "`unreadable`"):
                self.assertIn(phrase, guide)
            self.assertNotIn("older than the installed CLI", guide)
            self.assertEqual(guide.count("<!-- llm-wiki:begin -->"), 1)
            self.assertEqual(guide.count("<!-- llm-wiki:end -->"), 1)

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

    def test_wrap_candidate_links_origin_session_without_claiming_evidence(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        wiki = discover(self.root)
        path, candidates = wrap(wiki, "token=hunter2 topic", "task", "done", None, None, None, "gjc", None, ["possible fact"], self.root)
        candidate = candidates[0].read_text(encoding="utf-8")
        relative = path.relative_to(wiki.directory).as_posix()
        self.assertIn(f'session: "{relative}"', candidate)
        self.assertTrue((wiki.directory / relative).is_file())
        self.assertNotIn("hunter2", candidate)
        self.assertIn("status: needs-review", candidate)
        self.assertIn("- None recorded.", candidate)
        plain = promote(wiki, "plain", "body", [], "demo").read_text(encoding="utf-8")
        self.assertNotIn("session:", plain)

    def test_search_results_show_record_type_status_and_time(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        wiki = discover(self.root)
        path, candidates = wrap(wiki, "needle topic", "needle task", "done", None, None, None, "gjc", None, ["needle claim"], self.root)
        (wiki.directory / "shared" / "plain.md").write_text("needle without front matter\n", encoding="utf-8")
        (wiki.directory / "shared" / "unclosed.md").write_text("---\ntype: session\nstatus: draft\nneedle\n", encoding="utf-8")
        results, engine, _ = search(wiki, "needle", self.root, engine="text")
        self.assertEqual(engine, "text")
        by_path = {result["path"]: result for result in results}
        session = by_path[path.relative_to(wiki.root).as_posix()]
        self.assertEqual((session["type"], session["status"]), ("session", "draft"))
        self.assertRegex(session["created"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
        candidate = by_path[candidates[0].relative_to(wiki.root).as_posix()]
        self.assertEqual((candidate["type"], candidate["status"]), ("candidate", "needs-review"))
        plain = by_path["llm-wiki/shared/plain.md"]
        self.assertEqual((plain["type"], plain["status"], plain["created"]), (None, None, None))
        unclosed = by_path["llm-wiki/shared/unclosed.md"]
        self.assertEqual((unclosed["type"], unclosed["status"], unclosed["created"]), (None, None, None))

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
        with self.assertRaises(WikiError):
            wrap(wiki, "topic", "task", "done", None, None, None, None, None, [], legacy_root)
        with self.assertRaises(WikiError):
            add_project(wiki, "other", "other")
        self.assertEqual([path.name for path in (legacy_root / "llm-wiki").iterdir()], ["index.md"])

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

    def make_case_variant_wiki(self) -> Path:
        init_wiki(self.root, "umbrella", "top", False, False)
        variant = self.root / "LLM-Wiki"
        (self.root / "llm-wiki").rename(variant)
        return variant

    def test_discovery_finds_case_variant_wiki_from_cwd(self) -> None:
        variant = self.make_case_variant_wiki()
        nested = self.root / "apps" / "service"
        nested.mkdir(parents=True)
        with in_directory(nested), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLM_WIKI_ROOT", None)
            wiki = discover()
        self.assertEqual(wiki.name, "top")
        self.assertEqual(wiki.directory.name, "LLM-Wiki")
        self.assertEqual(wiki.directory.resolve(), variant.resolve())

    def test_reinit_reuses_case_variant_wiki(self) -> None:
        self.make_case_variant_wiki()
        result = init_wiki(self.root, None, "top", False, False)
        self.assertEqual(result["mode"], "umbrella")
        self.assertEqual([child.name for child in self.root.iterdir() if child.name.casefold() == "llm-wiki"], ["LLM-Wiki"])
        self.assertIn(str(self.root.resolve() / "LLM-Wiki" / "wiki.toml"), result["existing"])

    def test_init_points_agents_and_gjc_at_case_variant_wiki(self) -> None:
        self.make_case_variant_wiki()
        init_wiki(self.root, None, "top", True, True)
        self.assertIn("`LLM-Wiki/index.md`", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertIn("`LLM-Wiki/index.md`", (self.root / ".gjc" / "rules" / "llm-wiki.md").read_text(encoding="utf-8"))

    def test_unlistable_folder_still_finds_exact_wiki(self) -> None:
        outer = self.root / "outer"
        inner = outer / "inner"
        legacy = self.root / "legacy-root"
        (inner / "proj").mkdir(parents=True)
        (legacy / "llm-wiki").mkdir(parents=True)
        (legacy / "llm-wiki" / "index.md").write_text("old", encoding="utf-8")
        init_wiki(outer, "single", "outer", False, False)
        init_wiki(inner, "single", "inner", False, False)
        unlistable = {inner.resolve(), legacy.resolve()}
        real_iterdir = Path.iterdir

        def iterdir(path: Path):
            if path.resolve() in unlistable:
                raise PermissionError(13, "Permission denied", str(path))
            return real_iterdir(path)

        with patch.object(Path, "iterdir", iterdir):
            self.assertEqual(discover(inner / "proj").name, "inner")
            with self.assertRaisesRegex(WikiError, "legacy"):
                init_wiki(legacy, "single", None, False, False)
        self.assertFalse((legacy / "llm-wiki" / "wiki.toml").exists())

    def test_unsearchable_unconfigured_wiki_dir_falls_through_to_outer_wiki(self) -> None:
        init_wiki(self.root, "single", "outer", False, False)
        blocked = self.root.resolve() / "child" / "llm-wiki"
        blocked.mkdir(parents=True)
        real_stat = os.stat
        denied = []

        def stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
            if Path(os.fspath(path)).parent == blocked:
                denied.append(path)
                raise PermissionError(13, "Permission denied", os.fspath(path))
            return real_stat(path, *args, **kwargs)

        with patch("os.stat", stat):
            self.assertEqual(discover(self.root / "child").name, "outer")
        self.assertGreater(len(denied), 0)

    def test_init_refuses_inside_case_variant_wiki_without_changes(self) -> None:
        variant = self.make_case_variant_wiki()
        nested = variant / "shared" / "knowledge"
        original = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        with self.assertRaisesRegex(WikiError, "Cannot initialize inside an existing wiki directory"):
            init_wiki(nested, "single", None, True, True)
        self.assertEqual(
            {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()},
            original,
        )

    @unittest.skipUnless(hasattr(time, "tzset"), "needs time.tzset")
    def test_wrap_uses_local_time_for_folder_and_created(self) -> None:
        # Pin a non-UTC zone so a UTC clock cannot pass by accident.
        # POSIX TZ string, so no zone database is needed.
        try:
            with patch.dict(os.environ, {"TZ": "KST-9"}):
                time.tzset()
                init_wiki(self.root, "single", "demo", False, False)
                path, _ = wrap(discover(self.root), "topic", "task", "done", None, None, None, "agent", None, [], self.root)
        finally:
            time.tzset()
        created = next(line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("created: "))
        when = datetime.fromisoformat(created.removeprefix("created: "))
        self.assertEqual(when.utcoffset().total_seconds(), 9 * 3600)
        self.assertEqual(path.parent.relative_to(path.parents[2]).as_posix(), when.strftime("%Y/%m"))
        self.assertIn(when.strftime("[%Y-%m-%d %H:%M]"), (self.root / "llm-wiki" / "log.md").read_text(encoding="utf-8"))

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
        skill = self.root / ".gjc" / "skills" / "llm-wiki-wrap" / "SKILL.md"
        initial_agents, initial_rule, initial_skill = agents.read_bytes(), rule.read_bytes(), skill.read_bytes()
        self.assertTrue(skill.read_text(encoding="utf-8").startswith("---\nname: llm-wiki-wrap\n"))
        self.assertIn(str(skill.resolve()), first["created"])
        result = init_wiki(self.root, "single", "demo", True, True)
        self.assertEqual(agents.read_bytes(), initial_agents)
        self.assertEqual(rule.read_bytes(), initial_rule)
        self.assertEqual(skill.read_bytes(), initial_skill)
        self.assertIn(str(skill.resolve()), result["existing"])
        self.assertNotIn(str(agents.resolve()), result["created"])
        self.assertNotIn(str(rule.resolve()), result["created"])
        self.assertIn(str(agents.resolve()), result["existing"])
        self.assertIn(str(rule.resolve()), result["existing"])
        self.assertEqual(rule.read_text(encoding="utf-8").count("---\n"), 2)
        self.assertIn(str(agents.resolve()), first["created"])
        self.assertEqual(result["updated"], [])

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

    def test_redaction_covers_other_separators_and_secret_carriers(self) -> None:
        cases = {
            'password := "hunter2"': "password := [REDACTED_SECRET]",
            "'password' => 'hunter2',": "'password' => [REDACTED_SECRET],",
            "SECRET_KEY = 'django-insecure-abc123'": "SECRET_KEY = [REDACTED_SECRET]",
            "redis://:hunter2@localhost:6379/0": "redis://[REDACTED_CREDENTIALS]@localhost:6379/0",
            'curl -H "Authorization: Basic dXNlcjpodW50ZXIy" https://api': 'curl -H "Authorization: Basic [REDACTED_TOKEN]" https://api',
            'curl -H "Cookie: sessionid=abc123xyz" https://x': 'curl -H "Cookie: [REDACTED_AUTH]" https://x',
            r'"{\"password\":\"a\\\"b hunter2\"}"': r'"{\"password\":[REDACTED_SECRET]}"',
            "<password>hunter2</password>": "<password>[REDACTED_SECRET]</password>",
            "sessionid=abc123xyz next": "sessionid=[REDACTED_TOKEN] next",
            'Cookie: AUTH="Zx9QpL2rT"; HttpOnly': "Cookie: [REDACTED_AUTH]",
            'Set-Cookie: a=b; c="x y"; remember=QQQ': "Set-Cookie: [REDACTED_AUTH]",
            r'curl -H "Cookie: AUTH=\"Zx9\"" -H "X: keep"': 'curl -H "Cookie: [REDACTED_AUTH]" -H "X: keep"',
            '{"Cookie": "AUTH=Zx9"}': '{"Cookie": [REDACTED_AUTH]',
            "{'Set-Cookie': 'AUTH=Zx9'}": "{'Set-Cookie': [REDACTED_AUTH]",
            'curl -H "Cookie: AUTH="Zx9secret -H "X: y"': 'curl -H "Cookie: [REDACTED_AUTH]',
            'curl -H "Cookie: AUTH=x"tail -H "X: keep"': 'curl -H "Cookie: [REDACTED_AUTH]',
            '"body":"{\\"Cookie\\":\\"AUTH=Zx9\\"}"': '"body":"{\\"Cookie\\": [REDACTED_AUTH]',
            '{\\"Set-Cookie\\": \\"AUTH=Zx9\\"}': '{\\"Set-Cookie\\": [REDACTED_AUTH]',
            "x-session-id=abc123 next": "x-session-id=[REDACTED_TOKEN] next",
            "session-key=abc next": "session-key=[REDACTED_TOKEN] next",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(redact(raw)[0], expected)
                self.assert_fixed_point(raw, expected)

    def assert_fixed_point(self, raw: str, text: str) -> None:
        """What the tool writes for raw survives a second pass: the redacted body text, and the
        one-line header value json.dumps stores (agent, decided_by, reason)."""
        header = _clean_single(raw)
        for written in (text, json.dumps(header), json.dumps(header, ensure_ascii=False), f"reason: {json.dumps(header)}\nnext: 1"):
            self.assertEqual(redact(written)[0], written)

    def test_redaction_keeps_earlier_placeholders_intact(self) -> None:
        text, count = redact("token=ghp_" + "a" * 36 + " session=eyJa.b.c")
        self.assertEqual(text, "token=[REDACTED_TOKEN] session=[REDACTED_JWT]")
        self.assertEqual(count, 2)
        partial, partial_count = redact("password=[REDACTED_hunter2")
        self.assertEqual(partial, "password=[REDACTED_SECRET]")
        self.assertEqual(partial_count, 1)
        for value in ("[redacted_hunter]", "[REDACTED_MyPass]", "[REDACTED_X]hunter2", "[a]b]"):
            self.assertEqual(redact(f"password={value}"), ("password=[REDACTED_SECRET]", 1))
        self.assertEqual(redact("token=[REDACTED_TOKEN], next"), ("token=[REDACTED_TOKEN], next", 0))

    def test_redaction_covers_whole_unquoted_value(self) -> None:
        cases = {
            "password=Xk9;mQ2)vLp next": "password=[REDACTED_SECRET] next",
            "password=abc,def": "password=[REDACTED_SECRET]",
            "(password=abc)": "(password=[REDACTED_SECRET])",
            "token=abc123, next": "token=[REDACTED_SECRET], next",
            "sid=xy;z;": "sid=[REDACTED_TOKEN];",
        }
        for raw, expected in cases.items():
            self.assertEqual(redact(raw)[0], expected)
            self.assert_fixed_point(raw, expected)

    def test_redaction_keeps_fields_after_quoted_value(self) -> None:
        cases = {
            '{"password":"x","user":"bob"}': ('{"password":[REDACTED_SECRET],"user":"bob"}', 1),
            'password="x",user=bob': ("password=[REDACTED_SECRET],user=bob", 1),
            '["password": "x"]': ('["password": [REDACTED_SECRET]]', 1),
            'password="abc"def': ("password=[REDACTED_SECRET]", 1),
            'password="a b".': ("password=[REDACTED_SECRET].", 1),
            '`DB_PASSWORD="a b"`': ("`DB_PASSWORD=[REDACTED_SECRET]", 1),
            "\"X_PASSWORD='a b'\"": ('"X_PASSWORD=[REDACTED_SECRET]', 1),
            'password="a b"def': ("password=[REDACTED_SECRET]", 1),
            'password="a b"def.': ("password=[REDACTED_SECRET].", 1),
            '{"password":"abc\\",def","user":"bob"}': ('{"password":[REDACTED_SECRET],"user":"bob"}', 1),
            "password='a\\'b c',x=1": ("password=[REDACTED_SECRET],x=1", 1),
            'password="a b c\nnext': ("password=[REDACTED_SECRET]\nnext", 1),
            "secret: 'x y": ("secret: [REDACTED_SECRET]", 1),
            "password=sk-abcdefghijklmnopqrstu,XYZ": ("password=[REDACTED_SECRET]", 2),
        }
        for raw, expected in cases.items():
            self.assertEqual(redact(raw), expected)
            self.assert_fixed_point(raw, expected[0])

    def test_redaction_handles_escaped_and_repeated_quotes(self) -> None:
        cases = {
            '"body":"{\\"password\\":\\"hunter2\\",\\"user\\":\\"bob\\"}"':
                ('"body":"{\\"password\\":[REDACTED_SECRET],\\"user\\":\\"bob\\"}"', 1),
            '{"cmd":"export DB_PASSWORD=\\"a b\\""}': ('{"cmd":"export DB_PASSWORD=[REDACTED_SECRET]}', 1),
            "password: 'it''s a secret'": ("password: [REDACTED_SECRET]", 1),
            'password = """a b"""': ("password = [REDACTED_SECRET]", 1),
            "password=\"a b\"' c d'": ("password=[REDACTED_SECRET]", 1),
            "password=\"a\"b'c d": ("password=[REDACTED_SECRET]", 1),
            "\"X_PASSWORD='a b'\" user=keep": ('"X_PASSWORD=[REDACTED_SECRET] user=keep', 1),
            # A stray quote followed by a delimiter must not mask the rest of a short line.
            "token=abc\" user=bob": ("token=[REDACTED_SECRET] user=bob", 1),
            "password=$'a b' next": ("password=[REDACTED_SECRET] next", 1),
            'password=abc"def ghi" next': ("password=[REDACTED_SECRET] next", 1),
        }
        for raw, expected in cases.items():
            self.assertEqual(redact(raw), expected)
            self.assert_fixed_point(raw, expected[0])

    def test_redaction_backslash_terminated_value_runs_to_line_end(self) -> None:
        # Documented trade-off: a backslash escapes the closing quote, so the value stays open.
        self.assertEqual(redact("PASSWORD='abc\\' USER=bob"), ("PASSWORD=[REDACTED_SECRET]", 1))

    def test_log_heading_is_redacted_once(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        wiki = discover(self.root)
        promote(wiki, '{"password":"x","user":"bob"}', "body", [], "demo")
        log = (wiki.directory / "log.md").read_text(encoding="utf-8")
        self.assertIn('{"password":[REDACTED_SECRET],"user":"bob"}', log)

    def test_redaction_is_linear_on_punctuation_runs(self) -> None:
        started = time.monotonic()
        for raw in ("token=a" + ")" * 200_000 + "b", "password=[" + "]" * 200_000 + "x", 'password="a ' * 50_000,
                    'password="a\\' * 50_000, '"password":"' + '\\"' * 100_000, "a" * 200_000,
                    "password=" + "'\"" * 100_000, 'password="x"' + '"a"' * 100_000, "password=x" + '"a' * 100_000, '\\"password\\":\\"' + "\\x" * 100_000,
                    "token=[REDACTED_TOKEN]" + '\\"' * 100_000 + "x", "password=" + '\\"' * 100_000 + "x",
                    "Cookie: [REDACTED_AUTH]" + "\\'" * 100_000 + "x",
                    ):
            redact(raw)
        self.assertLess(time.monotonic() - started, 5)

    def test_redaction_is_linear_on_repeated_keywords_and_escapes(self) -> None:
        for raw in ('password=\\"' + "\\\\\\\\a" * 30, "token" * 20_000, "session" * 15_000, "xsid" + " " * 50_000 + "x", "sid =" * 20_000, "<" + "token" * 20_000,
                    "password=x" + '",\\' * 20_000, "eyJ-" * 25_000, '"Cookie:' * 40_000,
                    'token=x\\" ' * 20_000, "\n".join([('token=x\\" ' * 26)[:250]] * 2_000), "a-" * 50_000):
            with self.subTest(raw=raw[:20], length=len(raw)):
                started = time.monotonic()
                redact(raw)
                self.assertLess(time.monotonic() - started, 2)

    def test_redaction_never_retries_a_shorter_separator(self) -> None:
        for raw in ("token := ghp_" + "a" * 36, "token => ghp_" + "a" * 36):
            self.assertEqual(redact(raw)[0], raw[:9] + "[REDACTED_TOKEN]")
        self.assertEqual(redact("password==abc next")[0], "password=[REDACTED_SECRET] next")

    def test_redaction_reads_whole_key_names(self) -> None:
        tail = "_v" * 100
        long_key = f"x_password{tail}"
        self.assertEqual(redact(f"{long_key}=hunter2 ok")[0], f"{long_key}=[REDACTED_SECRET] ok")
        tag = f"db_password{tail}"
        self.assertEqual(redact(f"<{tag}>x</{tag}>")[0], f"<{tag}>[REDACTED_SECRET]</{tag}>")
        session = f"xsession{tail}"
        self.assertEqual(redact(f"mysid=abc {session}=def sidebar=keep")[0],
                         f"mysid=[REDACTED_TOKEN] {session}=[REDACTED_TOKEN] sidebar=keep")
        prefix = "legacy_reporting" + "_v" * 100
        for raw in (f"{prefix}_password=hunter2 ok", f"{prefix}_xsessionid=def ok"):
            self.assertEqual(redact(raw)[0], raw.split("=")[0] + "=" + ("[REDACTED_SECRET]" if "password" in raw else "[REDACTED_TOKEN]") + " ok")
        self.assertEqual(redact(f"<{prefix}_db_token>x</{prefix}_db_token>")[0],
                         f"<{prefix}_db_token>[REDACTED_SECRET]</{prefix}_db_token>")
        self.assertEqual(redact(f"{prefix}_name=keep ok")[0], f"{prefix}_name=keep ok")

    def test_redaction_masks_jwt_inside_dash_runs(self) -> None:
        jwt = "eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"
        for raw, expected in ((f"id {jwt} ok", "id [REDACTED_JWT] ok"),
                              (f"x-{jwt} ok", "x-[REDACTED_JWT] ok"),
                              (f"eyJ-{jwt} ok", "[REDACTED_JWT] ok"),
                              ("xeyJa.b.c ok", "xeyJa.b.c ok")):
            self.assertEqual(redact(raw)[0], expected)

    def test_redaction_stops_scanning_after_a_stray_quote(self) -> None:
        self.assertEqual(redact('password=x",y next')[0], "password=[REDACTED_SECRET] next")
        self.assertEqual(redact("password=x\",y'z next")[0], "password=[REDACTED_SECRET]")
        # With 256 or more characters left on the line, a stray quote masks the rest of it.
        self.assertEqual(redact('password=x" ' + "k" * 254)[0], "password=[REDACTED_SECRET] " + "k" * 254)
        self.assertEqual(redact('password=x" ' + "k" * 255)[0], "password=[REDACTED_SECRET]")
        # A quoted segment that closes stays a segment however long it is.
        self.assertEqual(redact(f'password=x"{"y" * 300}" next')[0], "password=[REDACTED_SECRET] next")

    def test_init_refuses_git_repo_named_like_wiki(self) -> None:
        (self.root / "llm-wiki" / ".git").mkdir(parents=True)
        with self.assertRaisesRegex(WikiError, "git repository"):
            init_wiki(self.root, "single", "demo", False, False)
        self.assertFalse((self.root / "llm-wiki" / "wiki.toml").exists())

    def test_umbrella_scan_skips_wiki_named_folders(self) -> None:
        # Only reachable when two case variants can coexist; the target wiki is the plain one.
        (self.root / "llm-wiki").mkdir()
        if (self.root / "LLM-Wiki").exists():
            self.skipTest("needs a case-sensitive filesystem")
        (self.root / "api" / ".git").mkdir(parents=True)
        (self.root / "LLM-Wiki" / ".git").mkdir(parents=True)
        result = init_wiki(self.root, "umbrella", "top", False, False)
        self.assertEqual(result["projects"], {"api": "api"})

    def test_init_umbrella_accepts_root_name_that_is_not_a_project_name(self) -> None:
        root = self.root / "My Workspace"
        (root / "api" / ".git").mkdir(parents=True)
        result = init_wiki(root, "umbrella", None, False, False)
        self.assertEqual(result["projects"], {"api": "api"})
        self.assertEqual(discover(root).name, "My Workspace")
        with self.assertRaisesRegex(WikiError, "Project name"):
            init_wiki(self.root / "Other Space", "single", None, False, False)
        self.assertFalse((self.root / "Other Space").exists())
        bad = self.root / "bad"
        bad.mkdir()
        with self.assertRaisesRegex(WikiError, "control characters"):
            init_wiki(bad, "umbrella", "bad\x7fname", False, False)
        with self.assertRaisesRegex(WikiError, "valid wiki configuration"):
            init_wiki(bad, "umbrella", "bad\udcffname", False, False)
        self.assertEqual(list(bad.iterdir()), [])

    def test_init_fails_without_residue_when_wiki_toml_is_a_directory(self) -> None:
        (self.root / "llm-wiki" / "wiki.toml").mkdir(parents=True)
        with self.assertRaises(OSError):
            init_wiki(self.root, "single", "demo", False, False)
        self.assertEqual([path.name for path in (self.root / "llm-wiki").iterdir()], ["wiki.toml"])

    def test_record_single_line_fields_and_wrap_collision(self) -> None:
        init_wiki(self.root, "single", "demo", False, False)
        wiki = discover(self.root)
        with patch("llm_wiki.records._now") as now:
            now.return_value = datetime(2026, 1, 1, tzinfo=timezone.utc)
            first, _ = wrap(wiki, "topic\nstatus: stable", "task", "done", None, None, None, "agent\nx", None, [], self.root)
            second, _ = wrap(wiki, "topic\nstatus: stable", "task", "done", None, None, None, "agent\nx", None, [], self.root)
        self.assertNotEqual(first, second)
        self.assertIn("# topic status: stable", first.read_text(encoding="utf-8"))
        self.assertIn('agent: "agent x"', first.read_text(encoding="utf-8"))


BEGIN = b"<!-- llm-wiki:begin -->"
END = b"<!-- llm-wiki:end -->"
MANAGED = ["AGENTS.md", ".gjc/rules/llm-wiki.md", ".gjc/skills/llm-wiki-wrap/SKILL.md", "llm-wiki/README.md"]
LEGACY_README = (
    "# demo LLM Wiki\n\nRead `index.md` before using this wiki. Search the current project before work and write a session record "
    "with `llm-wiki wrap` when work ends. Use candidates for uncertain claims.\n\nSource code, tests, and AGENTS.md outrank this wiki. "
    "Do not record secrets.\n"
)


class ManagedFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def path(self, relative: str) -> Path:
        return self.root / relative

    def snapshot(self) -> dict[str, bytes]:
        return {str(path.relative_to(self.root)): path.read_bytes() for path in sorted(self.root.rglob("*")) if path.is_file()}

    def status(self) -> dict[str, object]:
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["status", "--root", str(self.root), "--json"]), 0)
        return json.loads(out.getvalue())

    def states(self) -> dict[str, str]:
        return {entry["path"]: entry["state"] for entry in self.status()["managed"]}

    def refresh(self, *flags: str) -> str:
        return shlex.join(["llm-wiki", "init", "--path", str(self.root), *flags])

    def init(self, agents: bool = True, gjc: bool = True) -> dict[str, object]:
        return init_wiki(self.root, "single", "demo", agents, gjc)

    def write(self, relative: str, data: bytes) -> None:
        self.path(relative).parent.mkdir(parents=True, exist_ok=True)
        self.path(relative).write_bytes(data)

    def stale_rule(self) -> None:
        self.write(MANAGED[1], GJC_FRONT_MATTER.encode("utf-8") + BEGIN + b"\nold\n" + END + b"\n")

    def test_status_reports_managed_files_and_writes_nothing(self) -> None:
        self.init()
        before = self.snapshot()
        result = self.status()
        self.assertEqual([entry["path"] for entry in result["managed"]], MANAGED)
        self.assertEqual({entry["state"] for entry in result["managed"]}, {"current"})
        self.assertEqual([entry.get("front_matter") for entry in result["managed"]], [None, "current", "current", None])
        self.assertIsNone(result["refresh"])
        self.assertEqual(self.snapshot(), before)
        readme = self.path(MANAGED[3]).read_bytes()
        self.assertEqual((readme.count(BEGIN), readme.count(END)), (1, 1))

    def test_managed_states_cover_each_branch(self) -> None:
        self.init(False, False)
        with self.subTest("missing"):
            before = self.snapshot()
            managed = self.status()["managed"]
            self.assertEqual([entry["state"] for entry in managed], ["missing", "missing", "missing", "current"])
            self.assertTrue(all("front_matter" not in entry for entry in managed))
            self.assertEqual(self.snapshot(), before)
        with self.subTest("missing then init"):
            result = self.init()
            for relative in MANAGED[:3]:
                self.assertIn(str(self.path(relative)), result["created"])
                self.assertNotIn(str(self.path(relative)), result["updated"])
            self.assertEqual(set(self.states().values()), {"current"})
        cases = [
            ("unmarked agents", MANAGED[0], b"my rules\n", "unmarked"),
            ("unmarked rule", MANAGED[1], b"my rule\n", "unmarked"),
            ("stale", MANAGED[0], BEGIN + b"\nold\n" + END + b"\n", "stale"),
            ("stale rule", MANAGED[1], b"---\nmine: true\n---\n" + BEGIN + b"\nold\n" + END + b"\n", "stale"),
            ("begin only", MANAGED[0], BEGIN + b"\nx\n", "invalid"),
            ("end only", MANAGED[0], b"x\n" + END + b"\n", "invalid"),
            ("end before begin", MANAGED[0], END + b"\n" + BEGIN + b"\n", "invalid"),
            ("duplicate pair", MANAGED[0], (BEGIN + b"\nx\n" + END + b"\n") * 2, "invalid"),
            ("leading orphan end", MANAGED[0], END + BEGIN + b"x" + END, "invalid"),
            ("not utf-8", MANAGED[3], b"\xff\n", "unreadable"),
        ]
        for label, relative, data, state in cases:
            with self.subTest(label):
                original = self.path(relative).read_bytes()
                self.write(relative, data)
                before = self.snapshot()
                entry = next(entry for entry in self.status()["managed"] if entry["path"] == relative)
                self.assertEqual(entry["state"], state)
                self.assertEqual(entry.get("front_matter"), "differs" if label == "stale rule" else None)
                self.assertEqual(self.snapshot(), before)
                self.write(relative, original)
        with self.subTest("directory"):
            agents = self.path(MANAGED[0]).read_bytes()
            self.path(MANAGED[0]).unlink()
            self.path(MANAGED[0]).mkdir()
            self.assertEqual(self.states()[MANAGED[0]], "unreadable")
            self.path(MANAGED[0]).rmdir()
            self.write(MANAGED[0], agents)
        with self.subTest(".gjc is a file"):
            shutil.rmtree(self.path(".gjc"))
            self.write(".gjc", b"not a directory\n")
            before = self.snapshot()
            states = self.states()
            self.assertEqual((states[MANAGED[1]], states[MANAGED[2]]), ("unreadable", "unreadable"))
            self.assertEqual(self.snapshot(), before)

    def test_init_refresh_matches_prestate_and_keeps_outside_bytes(self) -> None:
        self.init()
        wiki = discover(self.root)
        wrap(wiki, "topic", "task", "done", None, None, None, "gjc", None, ["possible fact"], self.root)
        self.write("llm-wiki/projects/demo/knowledge/k.md", b"k\n")
        index_path = self.path("llm-wiki/index.md")
        index_path.write_bytes(index_path.read_bytes().replace(b"- [demo]", b"- [demo hand]") + b"Custom line.\n")
        agents_prefix, agents_suffix = b"before\r\n", b"\r\nafter\r\n"
        self.write(MANAGED[0], agents_prefix + BEGIN + b"\nold\n" + END + agents_suffix)
        self.stale_rule()
        readme_suffix = b"\r\nuser line\nmore\r\n"
        self.write(MANAGED[3], BEGIN + b"\r\nold\n" + END + readme_suffix)
        records = {key: value for key, value in self.snapshot().items() if not key.endswith(("AGENTS.md", "llm-wiki.md", "SKILL.md", "README.md"))}
        self.assertEqual(list(self.states().values()), ["stale", "stale", "current", "stale"])
        result = self.init()
        self.assertEqual(result["updated"], [str(self.path(relative)) for relative in (MANAGED[0], MANAGED[1], MANAGED[3])])
        self.assertFalse(set(result["created"]) & set(result["updated"]))
        agents = self.path(MANAGED[0]).read_bytes()
        self.assertTrue(agents.startswith(agents_prefix + BEGIN))
        self.assertTrue(agents.endswith(END + agents_suffix))
        self.assertTrue(self.path(MANAGED[1]).read_bytes().startswith(GJC_FRONT_MATTER.encode("utf-8") + BEGIN))
        self.assertTrue(self.path(MANAGED[3]).read_bytes().endswith(END + readme_suffix))
        self.assertEqual(set(self.states().values()), {"current"})
        after = self.snapshot()
        self.assertEqual(self.init()["updated"], [])
        self.assertEqual(self.snapshot(), after)
        self.assertEqual({key: value for key, value in after.items() if key in records}, records)

    def test_init_appends_to_unmarked_agents_only(self) -> None:
        self.init()
        self.write(MANAGED[0], b"my rules\r\n")
        self.write(MANAGED[1], b"my rule\n")
        result = self.init()
        agents = self.path(MANAGED[0]).read_bytes()
        self.assertTrue(agents.startswith(b"my rules\r\n"))
        self.assertEqual((agents.count(BEGIN), agents.count(END)), (1, 1))
        self.assertEqual(agents[agents.index(BEGIN): agents.index(END) + len(END)].decode("utf-8"), agents_block("llm-wiki").rstrip("\n"))
        self.assertIn(str(self.path(MANAGED[0])), result["updated"])
        self.assertEqual(self.path(MANAGED[1]).read_bytes(), b"my rule\n")
        self.assertNotIn(str(self.path(MANAGED[1])), result["updated"] + result["created"])
        states = self.states()
        self.assertEqual((states[MANAGED[0]], states[MANAGED[1]]), ("current", "unmarked"))

    def test_init_replaces_only_untouched_legacy_readme(self) -> None:
        self.assertEqual(legacy_wiki_readme("demo"), LEGACY_README)
        self.init()
        readme = self.path(MANAGED[3])
        for label, data, state in (
            ("legacy", LEGACY_README.encode("utf-8"), "legacy"),
            ("legacy plus a line", (LEGACY_README + "custom\n").encode("utf-8"), "unmarked"),
            ("custom", b"# My wiki\n", "unmarked"),
            ("crlf legacy", LEGACY_README.replace("\n", "\r\n").encode("utf-8"), "unmarked"),
        ):
            with self.subTest(label):
                readme.write_bytes(data)
                self.assertEqual(self.states()[MANAGED[3]], state)
                if state == "legacy":
                    self.assertEqual(self.status()["refresh"], self.refresh())
                result = self.init()
                self.assertEqual(str(readme) in result["updated"], state == "legacy")
                self.assertEqual(readme.read_bytes(), wiki_readme("demo").encode("utf-8") if state == "legacy" else data)

    def test_front_matter_difference_is_reported_and_kept(self) -> None:
        self.init()
        front = b"---\r\nalwaysApply: true\r\n---\r\n"
        self.write(MANAGED[1], front + BEGIN + b"\nold\n" + END + b"\n")
        entry = self.status()["managed"][1]
        self.assertEqual((entry["state"], entry["front_matter"]), ("stale", "differs"))
        self.init()
        self.assertTrue(self.path(MANAGED[1]).read_bytes().startswith(front + BEGIN))
        entry = self.status()["managed"][1]
        self.assertEqual((entry["state"], entry["front_matter"]), ("current", "differs"))
        self.assertIsNone(self.status()["refresh"])

    def test_init_rejects_invalid_or_unreadable_targets_before_writing(self) -> None:
        with self.subTest("fresh root"):
            self.write(MANAGED[0], BEGIN + b"\nonly begin\n")
            with self.assertRaisesRegex(WikiError, "--no-agents"):
                self.init()
            self.assertFalse(self.path("llm-wiki").exists())
            self.path(MANAGED[0]).unlink()
        self.init()
        with self.subTest("late invalid skill"):
            self.write(MANAGED[0], BEGIN + b"\nold\n" + END + b"\n")
            self.write(MANAGED[2], END + BEGIN + b"x" + END)
            before = self.snapshot()
            with self.assertRaises(WikiError) as raised:
                self.init()
            self.assertIn(MANAGED[2], str(raised.exception))
            self.assertIn("--no-gjc", str(raised.exception))
            self.assertEqual(self.snapshot(), before)
        with self.subTest("opted-out invalid skill and stale rule"):
            self.stale_rule()
            before = self.snapshot()
            result = self.init(True, False)
            for relative in MANAGED[1:3]:
                self.assertEqual(self.path(relative).read_bytes(), before[relative])
                self.assertNotIn(str(self.path(relative)), result["updated"])
            self.assertIn(str(self.path(MANAGED[0])), result["updated"])
        with self.subTest("opted-out invalid agents"):
            self.write(MANAGED[0], BEGIN + b"\nonly begin\n")
            self.init(False, False)
            self.assertEqual(self.path(MANAGED[0]).read_bytes(), BEGIN + b"\nonly begin\n")
        with self.subTest("unreadable readme"):
            self.write(MANAGED[3], b"\xff\n")
            before = self.snapshot()
            with self.assertRaises(WikiError) as raised:
                self.init(False, False)
            self.assertIn("fix or delete it", str(raised.exception))
            self.assertNotIn("--no-", str(raised.exception))
            self.assertEqual(self.snapshot(), before)

    def test_status_refresh_command_derives_opt_out_flags(self) -> None:
        stale_agents = BEGIN + b"\nold\n" + END + b"\n"
        cases = [
            ("all current", (True, True), {}, None),
            ("agents stale", (True, True), {MANAGED[0]: stale_agents}, ()),
            ("agents unmarked, rule stale", (True, True), {MANAGED[0]: b"my rules\n", MANAGED[1]: None}, ("--no-agents",)),
            ("agents stale, gjc missing", (True, False), {MANAGED[0]: stale_agents}, ("--no-gjc",)),
            ("invalid", (True, True), {MANAGED[0]: stale_agents, MANAGED[2]: END + BEGIN + END}, None),
        ]
        for label, groups, writes, flags in cases:
            with self.subTest(label):
                self.temp.cleanup()
                self.root.mkdir()
                self.init(*groups)
                for relative, data in writes.items():
                    self.stale_rule() if data is None else self.write(relative, data)
                refresh = self.status()["refresh"]
                self.assertEqual(refresh, None if flags is None else self.refresh(*flags))
                if flags is None:
                    continue
                skipped = [relative for relative, state in self.states().items() if state in {"unmarked", "missing"}]
                before = self.snapshot()
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(shlex.split(refresh)[1:]), 0)
                states = self.states()
                for relative in skipped:
                    self.assertEqual(self.path(relative).read_bytes() if relative in before else None, before.get(relative))
                self.assertEqual({state for relative, state in states.items() if relative not in skipped}, {"current"})
                self.assertIsNone(self.status()["refresh"])
        with self.subTest("legacy wiki"):
            self.temp.cleanup()
            self.path("llm-wiki").mkdir(parents=True)
            self.write("llm-wiki/index.md", b"legacy\n")
            result = self.status()
            self.assertEqual((result["managed"], result["refresh"]), ([], None))

    def test_add_project_rejects_leading_orphan_projects_marker(self) -> None:
        init_wiki(self.root, "umbrella", "top", False, False)
        index_path = self.path("llm-wiki/index.md")
        index_path.write_bytes(b"<!-- llm-wiki:projects:end -->\n" + index_path.read_bytes())
        before = index_path.read_bytes()
        config_before = self.path("llm-wiki/wiki.toml").read_bytes()
        with self.assertRaises(WikiError):
            register_project(discover(self.root), "api", "api")
        self.assertEqual(index_path.read_bytes(), before)
        self.assertEqual(self.path("llm-wiki/wiki.toml").read_bytes(), config_before)
        self.assertFalse(self.path("llm-wiki/projects/api").exists())


if __name__ == "__main__":
    unittest.main()
