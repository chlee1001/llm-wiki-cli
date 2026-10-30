from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gitwiki import _body, assert_immutable, read_all, snapshot

from llm_wiki import cli
from llm_wiki.brief import brief
from llm_wiki.config import WikiError, load_wiki_at
from llm_wiki.curate import decide, pending, review
from llm_wiki.init import init_central, register_project
from llm_wiki.records import promote
from llm_wiki.search import record_metadata, search

KST = timezone(timedelta(hours=9))
WHEN = datetime(2026, 5, 1, 10, 0, tzinfo=KST)
FAKE_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


class CurateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clone = Path(self.temp.name).resolve() / "company"
        self.clone.mkdir()
        init_central(self.clone, "company")
        register_project(load_wiki_at(self.clone), "api", None)
        self.wiki = load_wiki_at(self.clone)

    def candidate(self, title: str = "후보", sources: tuple[str, ...] = (), shared: bool = False) -> Path:
        return promote(self.wiki, title, f"{title} 본문", list(sources), "api", shared=shared, now=WHEN)

    def relative(self, path: Path) -> str:
        return path.relative_to(self.clone).as_posix()

    def frozen(self) -> tuple[dict[str, str], dict[str, bytes]]:
        return snapshot(self.clone), read_all(self.clone)

    def assert_unchanged(self, before: tuple[dict[str, str], dict[str, bytes]]) -> None:
        self.assertEqual(snapshot(self.clone), before[0])


class ReviewTests(CurateCase):
    def test_review_reports_material_and_changes_nothing(self) -> None:
        path = self.candidate("근거 있음", ("src/app.py:10", "https://example.com/x"))
        before = self.frozen()
        data = review(self.wiki, self.relative(path))
        self.assert_unchanged(before)
        self.assertEqual(data["path"], self.relative(path))
        self.assertEqual(data["status"], "candidate-with-evidence")
        self.assertEqual(data["project"], "api")
        self.assertEqual(data["title"], "근거 있음")
        self.assertEqual(data["sources"], ["src/app.py:10", "https://example.com/x"])
        self.assertIn("근거 있음 본문", data["body"])
        self.assertEqual(data["front_matter"]["type"], "candidate")
        self.assertFalse(data["decided"])
        self.assertIsNone(data["session"])

    def test_review_reports_session_link_and_whether_it_exists(self) -> None:
        session = self.clone / "projects" / "api" / "sessions" / "s.md"
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_text("# s\n", encoding="utf-8")
        path = promote(self.wiki, "t", "b", [], "api", now=WHEN, session=session)
        data = review(self.wiki, str(path))
        self.assertEqual(data["session"], {"path": "projects/api/sessions/s.md", "exists": True})
        self.assertEqual(data["sources"], [])
        session.unlink()
        self.assertFalse(review(self.wiki, str(path))["session"]["exists"])

    def test_pending_lists_only_undecided_candidates(self) -> None:
        first = self.candidate("one")
        second = self.candidate("two", ("x",))
        shared = self.candidate("three", shared=True)
        decide(self.wiki, self.relative(first), "rejected", "owner", "dup", now=WHEN)
        listed = pending(self.wiki, "api")
        self.assertEqual(sorted(item["path"] for item in listed), sorted([self.relative(second), self.relative(shared)]))
        self.assertEqual({item["status"] for item in listed}, {"needs-review", "candidate-with-evidence"})


class DecideTests(CurateCase):
    def test_accept_requires_decided_by_owner_and_leaves_the_file_alone(self) -> None:
        path = self.candidate()
        before = self.frozen()
        for who in ("", "Owner", "owner ", "agent", "gjc"):
            with self.subTest(who=who), self.assertRaisesRegex(WikiError, "owner"):
                decide(self.wiki, self.relative(path), "accepted", who, "ok", now=WHEN)
        self.assert_unchanged(before)

    def test_accept_rewrites_only_the_front_matter(self) -> None:
        for status_sources in ((), ("src/a.py",)):
            with self.subTest(sources=status_sources):
                path = self.candidate("accept me " + str(len(status_sources)), status_sources)
                before = self.frozen()
                result = decide(self.wiki, self.relative(path), "accepted", "owner", "검증 완료", now=WHEN)
                self.assertEqual(result, {"path": self.relative(path), "status": "accepted", "previous": "candidate-with-evidence" if status_sources else "needs-review", "decided_by": "owner", "decided_at": WHEN.isoformat(timespec="seconds"), "reason": "검증 완료"})
                assert_immutable(*before[:1], snapshot(self.clone), self.clone, before[1])
                new = path.read_bytes()
                self.assertEqual(_body(new), _body(before[1][self.relative(path)]))
                head = new[: len(new) - len(_body(new))].decode("utf-8")
                self.assertIn("status: accepted\n", head)
                self.assertTrue(head.endswith('decided_by: "owner"\ndecided_at: 2026-05-01T10:00:00+09:00\nreason: "검증 완료"\n---\n'))
                self.assertEqual(record_metadata(path)["status"], "accepted")
                self.assertIn(("sources:\n- src/a.py\n" if status_sources else "sources:\n- None recorded.\n"), head)

    def test_reject_keeps_the_file_and_allows_any_decider(self) -> None:
        path = self.candidate()
        before = self.frozen()
        result = decide(self.wiki, self.relative(path), "rejected", "codex", "중복", now=WHEN)
        self.assertEqual((result["status"], result["decided_by"]), ("rejected", "codex"))
        self.assertTrue(path.exists())
        assert_immutable(before[0], snapshot(self.clone), self.clone, before[1])
        self.assertEqual(record_metadata(path)["status"], "rejected")

    def test_redeciding_is_refused_with_the_fixed_message(self) -> None:
        path = self.candidate()
        decide(self.wiki, self.relative(path), "rejected", "owner", "no", now=WHEN)
        before = self.frozen()
        for decision in ("accepted", "rejected"):
            with self.subTest(decision), self.assertRaises(WikiError) as caught:
                decide(self.wiki, str(path), decision, "owner", "again", now=WHEN)
            self.assertEqual(str(caught.exception), f"이미 결정된 후보입니다: {self.relative(path)} (rejected)")
        self.assert_unchanged(before)

    def test_decider_and_reason_are_single_line_redacted_json(self) -> None:
        path = self.candidate()
        decide(self.wiki, self.relative(path), "rejected", 'bob"\ndecided_by: owner', f"leak {FAKE_KEY}\nstatus: accepted", now=WHEN)
        head = path.read_bytes()[: -len(_body(path.read_bytes()))].decode("utf-8")
        self.assertNotIn(FAKE_KEY, path.read_text(encoding="utf-8"))
        self.assertEqual(head.count("\nstatus: "), 1)
        self.assertEqual(head.count("\ndecided_by: "), 1)
        self.assertIn('decided_by: "bob\\" decided_by: owner"\n', head)
        self.assertEqual(review(self.wiki, str(path))["status"], "rejected")
        self.assertNotIn(FAKE_KEY, (self.clone / "log.md").read_text(encoding="utf-8"))

    def test_empty_reason_is_refused(self) -> None:
        path = self.candidate()
        before = self.frozen()
        with self.assertRaisesRegex(WikiError, "reason"):
            decide(self.wiki, self.relative(path), "rejected", "owner", "  \n ", now=WHEN)
        self.assert_unchanged(before)

    def test_body_bytes_survive_crlf_trailing_spaces_and_missing_final_newline(self) -> None:
        directory = self.clone / "projects" / "api" / "inbox" / "candidates"
        body = b"\n# CRLF\r\n\r\nline with trailing   \r\nmixed\nend without newline  "
        path = directory / "candidate-crlf.md"
        path.write_bytes(b'---\ntype: candidate\nstatus: needs-review\nproject: "api"\ncreated: 2026-05-01T10:00:00+09:00\nsources:\n- None recorded.\n---\n' + body)
        before = self.frozen()
        decide(self.wiki, self.relative(path), "accepted", "owner", "ok", now=WHEN)
        self.assertEqual(_body(path.read_bytes()), body)
        assert_immutable(before[0], snapshot(self.clone), self.clone, before[1])

    def test_malformed_candidates_are_refused_without_changes(self) -> None:
        directory = self.clone / "projects" / "api" / "inbox" / "candidates"
        cases = {
            "no-front-matter.md": b"# just text\n",
            "unclosed.md": b"---\ntype: candidate\nstatus: needs-review\n",
            "wrong-type.md": b"---\ntype: session\nstatus: needs-review\n---\nx\n",
            "unknown-status.md": b"---\ntype: candidate\nstatus: reviewed\n---\nx\n",
            "duplicate-status.md": b"---\ntype: candidate\nstatus: needs-review\nstatus: candidate-with-evidence\n---\nx\n",
            "crlf-head.md": b"---\r\ntype: candidate\r\nstatus: needs-review\r\n---\r\nx\r\n",
            "half-decided.md": b'---\ntype: candidate\nstatus: needs-review\ndecided_by: "x"\n---\nx\n',
            "bad-utf8.md": b"---\ntype: candidate\nstatus: needs-review\n---\n\xff\n",
        }
        for name, data in cases.items():
            (directory / name).write_bytes(data)
        before = self.frozen()
        for name in cases:
            with self.subTest(name), self.assertRaises(WikiError):
                decide(self.wiki, f"projects/api/inbox/candidates/{name}", "rejected", "owner", "x", now=WHEN)
        self.assert_unchanged(before)

    def test_only_candidate_files_inside_the_wiki_are_accepted(self) -> None:
        path = self.candidate()
        session = self.clone / "projects" / "api" / "sessions" / "s.md"
        session.parent.mkdir(parents=True, exist_ok=True)
        session.write_bytes(path.read_bytes())
        outside = Path(self.temp.name) / "outside.md"
        outside.write_bytes(path.read_bytes())
        link = self.clone / "projects" / "api" / "inbox" / "candidates" / "link.md"
        os.symlink(outside, link)
        before = self.frozen()
        for raw in ("projects/api/sessions/s.md", str(outside), "projects/api/inbox/candidates/link.md", "projects/api/inbox/candidates/../../sessions/s.md", "../outside.md", "projects/api/inbox/candidates/missing.md", "log.md"):
            with self.subTest(raw), self.assertRaises(WikiError):
                decide(self.wiki, raw, "rejected", "owner", "x", now=WHEN)
            with self.subTest(review=raw), self.assertRaises(WikiError):
                review(self.wiki, raw)
        self.assert_unchanged(before)
        self.assertEqual(outside.read_bytes(), path.read_bytes())

    def test_decision_is_appended_to_log(self) -> None:
        path = self.candidate("logged")
        old = (self.clone / "log.md").read_bytes()
        decide(self.wiki, self.relative(path), "accepted", "owner", "ok", now=WHEN)
        new = (self.clone / "log.md").read_bytes()
        self.assertTrue(new.startswith(old))
        self.assertIn(f"accept | api | logged\n\n- `{self.relative(path)}`".encode(), new[len(old) :])

    def test_decided_candidates_leave_brief_and_show_in_search(self) -> None:
        path = self.candidate("walrus")
        decide(self.wiki, self.relative(path), "accepted", "owner", "ok", now=WHEN)
        self.assertEqual(brief(self.wiki, "api")["sections"]["candidates"]["items"], [])
        results, _, _ = search(self.wiki, "walrus", self.clone, "api", False, "text", 5)
        self.assertEqual(results[0]["status"], "accepted")


class CurateCliTests(CurateCase):
    def run_cli(self, *argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main([*argv, "--root", str(self.clone), "--json"])
        return code, json.loads(out.getvalue())

    def test_accept_without_decided_by_is_refused(self) -> None:
        path = self.relative(self.candidate())
        before = self.frozen()
        code, data = self.run_cli("accept", path, "--reason", "ok")
        self.assertEqual(code, 1)
        self.assertIn("owner", data["error"])
        code, _ = self.run_cli("accept", path, "--decided-by", "me", "--reason", "ok")
        self.assertEqual(code, 1)
        self.assert_unchanged(before)

    def test_review_accept_reject_round_trip(self) -> None:
        first = self.relative(self.candidate("a"))
        second = self.relative(self.candidate("b"))
        code, data = self.run_cli("review")
        self.assertEqual((code, sorted(item["path"] for item in data["candidates"])), (0, sorted([first, second])))
        code, data = self.run_cli("review", first)
        self.assertEqual((code, data["status"]), (0, "needs-review"))
        code, data = self.run_cli("accept", first, "--decided-by", "owner", "--reason", "확인")
        self.assertEqual((code, data["status"]), (0, "accepted"))
        code, data = self.run_cli("reject", second, "--decided-by", "gjc", "--reason", "중복")
        self.assertEqual((code, data["status"]), (0, "rejected"))
        code, data = self.run_cli("reject", second, "--decided-by", "owner", "--reason", "again")
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], f"이미 결정된 후보입니다: {second} (rejected)")
        code, data = self.run_cli("review")
        self.assertEqual(data["candidates"], [])


if __name__ == "__main__":
    unittest.main()
