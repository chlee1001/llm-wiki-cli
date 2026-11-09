from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from llm_wiki.config import Wiki, WikiError
from llm_wiki.history import _validate, content_hash, ingest, state
from llm_wiki.cli import main
from unittest.mock import patch


def payload(week="2026-W45", day="2026-11-02"):
    return {"schema": "work-history/v2", "week": week, "period": {"start": day, "end": day}, "extraction": {"bundle_sha256": "a" * 64, "skill": "weekly-extract@1", "model": "test"}, "records": [{"id": "improve-flow", "project": "sample", "date": day, "title": "Improve flow", "summary": "Built a flow", "work_status": "in-progress", "carry_key": "flow", "evidence": "inferred", "sources": [{"agent": "gjc", "session": "abc", "date": day}]}]}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wiki = Wiki(self.root, self.root, "umbrella", "test", {"sample": "sample", "other": "other"})

    def test_vector(self):
        vector = json.loads((Path(__file__).parent / "fixtures/content_hash_vector.json").read_text())
        self.assertEqual(content_hash(vector["record"]), vector["sha256"])

    def test_project_pages_replace_and_are_deterministic(self):
        data = payload()
        ingest(self.wiki, data)
        page = self.root / "projects/sample/history/2026/2026-W45.md"
        self.assertTrue(page.exists())
        self.assertIn('type: history', page.read_text())
        self.assertIn('status: "inferred"', page.read_text())
        self.assertIn('category: ""', page.read_text())
        before = {p: p.read_bytes() for p in self.root.rglob("*.md")}
        ingest(self.wiki, data)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*.md")})
        data["records"][0]["project"] = "other"
        ingest(self.wiki, data)
        self.assertFalse(page.exists())
        self.assertTrue((self.root / "projects/other/history/2026/2026-W45.md").exists())
        self.assertFalse((self.root / "history").exists())

    def test_state_carry_and_decisions(self):
        first = payload()
        first["records"][0]["decisions"] = [{"id": "a", "decision": "A", "options": [], "supersedes": [], "source": "gjc:abc", "quote": "A"}]
        ingest(self.wiki, first)
        later = payload("2026-W46", "2026-11-09")
        later["records"][0]["decisions"] = [{"id": "b", "decision": "B", "options": [], "supersedes": ["a"], "source": "gjc:abc", "quote": "B"}]
        ingest(self.wiki, later)
        current = state(self.wiki)
        self.assertEqual(current["open_work"][0]["since"], "2026-W45")
        self.assertEqual(current["open_work"][0]["last_week"], "2026-W46")
        self.assertEqual(current["decisions"][0]["status"], "superseded by b (2026-W46)")
        self.assertEqual(state(self.wiki, before="2026-W46")["decisions"][0]["status"], "active")
        self.assertEqual(state(self.wiki, through="2026-W45", extra=first)["weeks"], ["2026-W45"])
        first["records"][0]["decisions"] = []
        with self.assertRaises(WikiError):
            ingest(self.wiki, first)

    def test_check_is_read_only_and_includes_input(self):
        result = ingest(self.wiki, payload(), check=True)
        self.assertTrue(result["check"])
        self.assertEqual(result["state"]["open_work"][0]["carry_key"], "flow")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_state_cli_is_read_only(self):
        ingest(self.wiki, payload())
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        out = io.StringIO()
        with patch("llm_wiki.cli.discover_for", return_value=(self.wiki, None)), contextlib.redirect_stdout(out):
            self.assertEqual(main(["history", "state", "--before", "2026-W46", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue()), state(self.wiki, before="2026-W46"))
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()})

    def test_wrap_validation_and_quote_errors(self):
        wrap = self.root / "projects/sample/sessions/2026/11/session-test.md"
        wrap.parent.mkdir(parents=True)
        wrap.write_text("synthetic")
        data = payload()
        data["records"][0]["sources"] = [{"agent": "자유 이름", "wrap": wrap.relative_to(self.root).as_posix(), "date": "2026-11-02"}]
        _validate(self.wiki, data)
        wrap.unlink()
        with self.assertRaises(WikiError):
            _validate(self.wiki, data)
        target = self.root / "target"
        target.write_text("synthetic")
        wrap.symlink_to(target)
        with self.assertRaises(WikiError):
            _validate(self.wiki, data)
        data = payload()
        decision = {"id": "a", "decision": "yes", "options": [], "supersedes": [], "source": "gjc:abc", "quote": "yes"}
        for change in ({"reason": "because"}, {"quote": ""}, {"quote": "x" * 201}):
            data["records"][0]["decisions"] = [{**decision, **change}]
            with self.subTest(change=change), self.assertRaises(WikiError):
                _validate(self.wiki, data)

    def test_history_destination_symlink_is_rejected(self):
        target = self.root / "outside"
        target.mkdir()
        project = self.root / "projects/sample"
        project.mkdir(parents=True)
        (project / "history").symlink_to(target, target_is_directory=True)
        with self.assertRaises(WikiError):
            ingest(self.wiki, payload())
        self.assertEqual(list(target.iterdir()), [])

    def test_iso_year_stale_unregistered_and_unowned_pages(self):
        first = payload("2026-W53", "2026-12-28")
        ingest(self.wiki, first)
        self.assertTrue((self.root / "projects/sample/history/2026/2026-W53.md").is_file())
        second = payload("2027-W01", "2027-01-04")
        ingest(self.wiki, second)
        self.assertTrue((self.root / "projects/sample/history/2027/2027-W01.md").is_file())
        with self.assertRaises(WikiError):
            _validate(self.wiki, payload("2027-W53", "2027-12-31"))
        stale = self.root / "projects/old/history/2026/2026-W53.md"
        stale.parent.mkdir(parents=True)
        stale.write_text((self.root / "projects/sample/history/2026/2026-W53.md").read_text())
        ingest(self.wiki, first)
        self.assertFalse(stale.exists())
        unowned = self.root / "projects/other/history/2026/2026-W53.md"
        unowned.parent.mkdir(parents=True)
        unowned.write_text("personal notes")
        with self.assertRaises(WikiError):
            ingest(self.wiki, first)
        self.assertEqual(unowned.read_text(), "personal notes")

    def test_carry_closed_reopened_and_cross_project_independent(self):
        first = payload()
        other = dict(first["records"][0], project="other", id="other-task")
        first["records"].append(other)
        ingest(self.wiki, first)
        third = payload("2026-W47", "2026-11-16")
        ingest(self.wiki, third)
        prior = state(self.wiki, before="2026-W47")
        self.assertTrue(all(x["last_week"] == "2026-W45" for x in prior["open_work"]))
        by_project = {x["project"]: x for x in state(self.wiki)["open_work"]}
        self.assertEqual((by_project["sample"]["since"], by_project["sample"]["last_week"]), ("2026-W45", "2026-W47"))
        self.assertEqual(by_project["other"]["last_week"], "2026-W45")
        closed = payload("2026-W48", "2026-11-23")
        closed["records"][0]["work_status"] = "done"
        ingest(self.wiki, closed)
        self.assertEqual([x["project"] for x in state(self.wiki)["open_work"]], ["other"])
        reopened = payload("2026-W49", "2026-11-30")
        ingest(self.wiki, reopened)
        self.assertEqual(next(x for x in state(self.wiki)["open_work"] if x["project"] == "sample")["since"], "2026-W49")

    def test_bad_state_metadata_reports_path(self):
        page = self.root / "projects/sample/history/2026/2026-W45.md"
        page.parent.mkdir(parents=True)
        page.write_text('---\norigin: history-ingest\n---\n<!-- history-records: [{}] -->\n')
        self.assertEqual(state(self.wiki, before="2026-W45")["weeks"], [])
        with self.assertRaises(WikiError) as error:
            state(self.wiki)
        self.assertIn(str(page), str(error.exception))
        out = io.StringIO()
        with patch("llm_wiki.cli.discover_for", return_value=(self.wiki, None)), contextlib.redirect_stdout(out):
            self.assertEqual(main(["history", "state", "--json"]), 1)
        self.assertIn(str(page), json.loads(out.getvalue())["error"])

    def test_supersedes_graph_rejects_future_duplicate_and_cycles(self):
        first = payload()
        decision = {"id": "a", "decision": "A", "options": [], "supersedes": [], "source": "gjc:abc", "quote": "A"}
        first["records"][0]["decisions"] = [decision]
        ingest(self.wiki, first)
        later = payload("2026-W46", "2026-11-09")
        later["records"][0]["decisions"] = [{**decision, "id": "b", "supersedes": ["a"]}]
        ingest(self.wiki, later)
        ingest(self.wiki, first)
        self.assertEqual(state(self.wiki)["decisions"][0]["superseded_by"], "b")
        invalid = copy.deepcopy(first)
        invalid["records"][0]["decisions"][0]["supersedes"] = ["b"]
        with self.assertRaises(WikiError):
            ingest(self.wiki, invalid)
        duplicate = copy.deepcopy(later)
        duplicate["records"][0]["decisions"][0]["id"] = "a"
        with self.assertRaises(WikiError):
            ingest(self.wiki, duplicate)
        cycle = copy.deepcopy(first)
        cycle["records"][0]["decisions"] = [{**decision, "supersedes": ["c"]}, {**decision, "id": "c", "supersedes": ["a"]}]
        with self.assertRaises(WikiError):
            ingest(self.wiki, cycle)
        later2 = payload("2026-W47", "2026-11-16")
        later2["records"][0]["decisions"] = [{**decision, "id": "c", "supersedes": ["a"]}]
        with self.assertRaises(WikiError):
            ingest(self.wiki, later2)
        self.assertEqual(state(self.wiki, before="2026-W45")["weeks"], [])

    def test_ticket_index_has_links_not_titles_and_cleans_project_index(self):
        data = payload()
        data["records"][0]["refs"] = [{"kind": "jira", "key": "IHWP-1774", "state": "unchecked"}]
        data["records"].append(dict(data["records"][0], id="other", project="other"))
        ingest(self.wiki, data)
        text = (self.root / "shared/history/tickets.md").read_text()
        self.assertIn("projects/sample/history/2026/2026-W45.md", text)
        self.assertIn("projects/other/history/2026/2026-W45.md", text)
        self.assertNotIn("Improve flow", text)
        data["records"].pop()
        ingest(self.wiki, data)
        self.assertNotIn("2026-W45", (self.root / "projects/other/history/index.md").read_text())

    def test_check_cli_excludes_future_and_invalid_is_one(self):
        later = payload("2026-W46", "2026-11-09")
        ingest(self.wiki, later)
        input_path = self.root.parent / f"{self.root.name}-input.json"
        self.addCleanup(input_path.unlink, missing_ok=True)
        input_path.write_text(json.dumps(payload()))
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        out = io.StringIO()
        with patch("llm_wiki.cli.discover_for", return_value=(self.wiki, None)), contextlib.redirect_stdout(out):
            self.assertEqual(main(["history", "ingest", "--input-json", str(input_path), "--check", "--json"]), 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["state"]["weeks"], ["2026-W45"])
        self.assertTrue(result["check"])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()})
        input_path.write_text(json.dumps({**payload(), "schema": "work-history/v1"}))
        with patch("llm_wiki.cli.discover_for", return_value=(self.wiki, None)), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["history", "ingest", "--input-json", str(input_path), "--check", "--json"]), 1)

    def test_state_cli_rejects_bad_before_and_project(self):
        for args in (["--before", "2026-W99"], ["--project", "missing"]):
            with self.subTest(args=args), patch("llm_wiki.cli.discover_for", return_value=(self.wiki, None)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["history", "state", *args, "--json"]), 1)

    def test_valid(self):
        data = payload()
        self.assertEqual(_validate(self.wiki, data), (data["week"], data["records"]))
        data["records"][0]["verified_hash"] = content_hash(data["records"][0])
        data["records"][0]["evidence"] = "verified"
        _validate(self.wiki, data)

    def test_invalid_contract(self):
        mutations = [
            lambda d: d.update(schema="work-history/v1"),
            lambda d: d.update(report={}),
            lambda d: d["records"][0].update(project="missing"),
            lambda d: d["records"][0].pop("carry_key"),
            lambda d: d["records"].append(dict(d["records"][0], id="other")),
            lambda d: d["records"][0].update(why="reason"),
            lambda d: d["records"][0].update(sources=[{"agent": "bad", "session": "abc", "date": "2026-11-02"}]),
            lambda d: d["records"][0].update(sources=[{"agent": "gjc", "session": "abc", "date": "2026-11-09"}]),
            lambda d: d["records"][0].update(date="2026-11-03"),
            lambda d: d["records"][0].update(evidence="verified", verified_hash="bad"),
            lambda d: d["extraction"].update(bundle_sha256="bad"),
            lambda d: d["records"][0].update(tags=["password=hunter2"]),
            lambda d: d["records"][0].update(sources=[{"agent": "gjc", "wrap": "../escape", "date": "2026-11-02"}]),
            lambda d: d["records"][0].update(refs=[{"kind": "pr", "repo": "a/b", "number": 1, "state": "open"}]),
            lambda d: d["records"][0].update(refs=[{"kind": "pr", "repo": "a/b", "number": 1, "state": "open", "checked_at": "2026-11-02T12:00:00"}]),
            lambda d: d["records"][0].update(refs=[{"kind": "pr", "repo": "a/b", "number": 1, "state": "unchecked"}]),
            lambda d: d["records"][0].update(decisions=[{"id": "a", "decision": "yes", "options": [], "supersedes": [], "source": "gjc:other", "quote": "yes"}]),
        ]
        for mutate in mutations:
            data = payload()
            mutate(data)
            with self.subTest(data=data), self.assertRaises(WikiError) as caught:
                _validate(self.wiki, data)
            self.assertNotIn("hunter2", str(caught.exception))
            self.assertEqual(list(self.root.iterdir()), [])
