from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from llm_wiki import cli
from llm_wiki.brief import DEFAULT_LIMIT, brief
from llm_wiki.config import load_wiki_at
from llm_wiki.handoff import take
from llm_wiki.init import init_central, register_project
from llm_wiki.records import promote

from gitwiki import GitWikiCase

BASE = datetime(2026, 1, 1, 9, 0, 0).astimezone()


def put(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class BriefCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.clone = Path(self.temp.name).resolve() / "company"
        self.clone.mkdir()
        init_central(self.clone, "company")
        register_project(load_wiki_at(self.clone), "api", None)
        self.wiki = load_wiki_at(self.clone)
        self.project_dir = self.clone / "projects" / "api"

    def session(self, index: int, completed: str = "did a thing", topic: str | None = None) -> Path:
        when = BASE + timedelta(minutes=index)
        name = f"session-{when.strftime('%Y%m%d-%H%M%S')}-gjc-s{index}.md"
        return put(self.project_dir / "sessions" / "2026" / "01" / name, f"""---
type: session
status: draft
project: "api"
agent: "gjc"
created: {when.isoformat(timespec='seconds')}
---

# {topic or f'session {index}'}

## Task

task {index}

## Completed

{completed}

## Verification

Not recorded.

## Handoff

None.

## Not tested

None.
""")

    def handoff(self, index: int, text: str = "carry on") -> Path:
        when = BASE + timedelta(minutes=index)
        stem = f"session-{when.strftime('%Y%m%d-%H%M%S')}-gjc-h{index}"
        return put(self.project_dir / "handoffs" / f"{stem}.md", f"""---
type: handoff
project: "api"
session: "projects/api/sessions/2026/01/{stem}.md"
created: {when.isoformat(timespec='seconds')}
---

# handoff {index}

Session: `projects/api/sessions/2026/01/{stem}.md`

{text}
""")

    def candidate(self, index: int, status: str = "needs-review") -> Path:
        when = BASE + timedelta(minutes=index)
        return put(self.project_dir / "inbox" / "candidates" / f"candidate-{when.strftime('%Y%m%d-%H%M%S')}-c{index}.md", f"""---
type: candidate
status: {status}
project: "api"
created: {when.isoformat(timespec='seconds')}
sources:
- None recorded.
---

# candidate {index}

body {index}
""")


class BriefContentTests(BriefCase):
    def test_lists_sessions_open_handoffs_and_pending_candidates_newest_first(self) -> None:
        for i in range(3):
            self.session(i)
            self.handoff(i)
        self.candidate(1)
        self.candidate(2, "candidate-with-evidence")
        self.candidate(3, "accepted")
        self.candidate(4, "rejected")
        result = brief(self.wiki, "api")
        sections = result["sections"]
        self.assertEqual([item["title"] for item in sections["handoffs"]["items"]], ["handoff 2", "handoff 1", "handoff 0"])
        self.assertEqual([item["title"] for item in sections["sessions"]["items"]], ["session 2", "session 1", "session 0"])
        self.assertEqual([item["title"] for item in sections["candidates"]["items"]], ["candidate 2", "candidate 1"])
        self.assertEqual({s["omitted"] for s in sections.values()}, {0})
        text = result["text"]
        self.assertLess(text.index("handoff 2"), text.index("handoff 1"))
        self.assertIn("did a thing", text)
        self.assertIn("carry on", text)

    def test_taken_handoffs_are_not_open(self) -> None:
        h = self.handoff(1)
        self.handoff(2)
        take(self.wiki, "api", h.stem, "claude")
        result = brief(self.wiki, "api")
        self.assertEqual([item["id"] for item in result["sections"]["handoffs"]["items"]], [self.handoff(2).stem])

    def test_duplicate_take_is_flagged(self) -> None:
        h = self.handoff(1)
        take(self.wiki, "api", h.stem, "claude")
        take(self.wiki, "api", h.stem, "codex")
        result = brief(self.wiki, "api")
        self.assertEqual(result["duplicate_takes"], [{"id": h.stem, "agents": ["claude", "codex"]}])
        self.assertIn("duplicate take", result["text"])
        self.assertIn(h.stem, result["text"])

    def test_shared_candidates_are_included(self) -> None:
        promote(self.wiki, "shared idea", "body", [], None, shared=True)
        result = brief(self.wiki, "api")
        self.assertEqual([item["title"] for item in result["sections"]["candidates"]["items"]], ["shared idea"])

    def test_all_projects_when_no_project_is_selected(self) -> None:
        register_project(load_wiki_at(self.clone), "web", None)
        wiki = load_wiki_at(self.clone)
        self.session(1)
        put(self.clone / "projects" / "web" / "sessions" / "2026" / "01" / "session-20260101-100000-gjc-w.md", "---\ntype: session\ncreated: 2026-01-01T10:00:00+09:00\n---\n\n# web session\n\n## Completed\n\nweb\n")
        result = brief(wiki, None)
        self.assertEqual(sorted(item["title"] for item in result["sections"]["sessions"]["items"]), ["session 1", "web session"])

    def test_empty_wiki_still_renders(self) -> None:
        result = brief(self.wiki, "api")
        self.assertIn("No open handoffs", result["text"])
        self.assertEqual(result["bytes"], len(result["text"].encode("utf-8")))


class BriefBudgetTests(BriefCase):
    def test_fifty_old_open_handoffs_do_not_starve_candidates_or_sessions(self) -> None:
        for i in range(50):
            self.handoff(i, "오래된 인수인계 내용 " * 20)
        for i in range(8):
            self.candidate(100 + i)
            self.session(100 + i, "세션 완료 요약 " * 30)
        result = brief(self.wiki, "api")
        data = result["text"].encode("utf-8")
        self.assertLessEqual(len(data), DEFAULT_LIMIT)
        self.assertEqual(result["bytes"], len(data))
        sections = result["sections"]
        self.assertGreaterEqual(len(sections["candidates"]["items"]), 1)
        self.assertGreaterEqual(len(sections["sessions"]["items"]), 1)
        self.assertGreater(sections["handoffs"]["omitted"], 0)
        shown = [item["title"] for item in sections["handoffs"]["items"]]
        self.assertEqual(shown, [f"handoff {i}" for i in range(49, 49 - len(shown), -1)])
        self.assertIn(f"{sections['handoffs']['omitted']} omitted", result["text"])
        self.assertEqual(len(sections["handoffs"]["items"]) + sections["handoffs"]["omitted"], 50)

    def test_omission_drops_oldest_items_and_reports_counts_per_section(self) -> None:
        for i in range(40):
            self.session(i, "가나다라마바사 " * 40)
        result = brief(self.wiki, "api")
        sessions = result["sections"]["sessions"]
        self.assertGreater(sessions["omitted"], 0)
        titles = [item["title"] for item in sessions["items"]]
        self.assertEqual(titles, [f"session {i}" for i in range(39, 39 - len(titles), -1)])
        self.assertLessEqual(result["bytes"], DEFAULT_LIMIT)

    def test_unused_budget_flows_to_the_section_that_needs_it(self) -> None:
        for i in range(40):
            self.session(i, "x" * 500)
        result = brief(self.wiki, "api")
        # with no handoffs or candidates, sessions may use far more than their 3 KiB minimum
        self.assertGreater(sum(len(item["text"].encode()) for item in result["sections"]["sessions"]["items"]), 3 * 1024 * 2)

    def test_multibyte_text_is_never_cut_mid_character(self) -> None:
        for i in range(30):
            self.handoff(i, "한글🙂" * 300)
        result = brief(self.wiki, "api", limit=4096)
        data = result["text"].encode("utf-8")
        self.assertLessEqual(len(data), 4096)
        data.decode("utf-8")  # strict decode must succeed
        self.assertNotIn("\ufffd", result["text"])

    def test_tiny_limit_is_refused(self) -> None:
        from llm_wiki.config import WikiError

        with self.assertRaises(WikiError):
            brief(self.wiki, "api", limit=100)

    def test_long_single_item_is_excerpted_not_dropped(self) -> None:
        self.session(1, "긴 내용 " * 5000)
        result = brief(self.wiki, "api")
        self.assertEqual(len(result["sections"]["sessions"]["items"]), 1)
        self.assertIn("…", result["text"])


class BriefFreshnessTests(GitWikiCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.clone("m3")
        init_central(self.repo, "company")
        register_project(load_wiki_at(self.repo), "api", None)
        self.wiki = load_wiki_at(self.repo)

    def test_never_fetched_is_warned(self) -> None:
        result = brief(self.wiki, "api")
        self.assertIsNone(result["last_fetch"])
        self.assertTrue(any("never" in w for w in result["warnings"]))

    def test_recent_fetch_is_not_warned_and_old_fetch_is(self) -> None:
        from llm_wiki.sync import sync_wiki

        sync_wiki(self.repo)
        self.git("fetch", "-q", cwd=self.repo)
        fetch_head = self.repo / ".git" / "FETCH_HEAD"
        self.assertTrue(fetch_head.exists())
        result = brief(self.wiki, "api")
        self.assertIsNotNone(result["last_fetch"])
        self.assertEqual(result["warnings"], [])
        old = time.time() - 30 * 3600
        os.utime(fetch_head, (old, old))
        result = brief(self.wiki, "api")
        self.assertTrue(any("30" in w or "hours" in w for w in result["warnings"]))

    def test_non_git_wiki_has_no_freshness_warning(self) -> None:
        plain = self.tmp / "plain"
        plain.mkdir()
        init_central(plain, "plain")
        result = brief(load_wiki_at(plain), None)
        self.assertEqual(result["warnings"], [])


class BriefCliTests(BriefCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_plain_output_is_the_brief_text(self) -> None:
        self.session(1)
        code, out = self.run_cli("brief", "--root", str(self.clone), "--project", "api")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("# "))
        self.assertIn("session 1", out)
        self.assertLessEqual(len(out.encode("utf-8")), DEFAULT_LIMIT + 1)

    def test_json_output_carries_counts(self) -> None:
        self.session(1)
        code, out = self.run_cli("brief", "--root", str(self.clone), "--project", "api", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["sections"]["sessions"]["omitted"], 0)
        self.assertIn("text", data)

    def test_max_bytes_option(self) -> None:
        for i in range(30):
            self.session(i, "y" * 400)
        code, out = self.run_cli("brief", "--root", str(self.clone), "--project", "api", "--max-bytes", "4096")
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out.encode("utf-8")), 4097)

    def test_unknown_project_is_an_error(self) -> None:
        code, out = self.run_cli("brief", "--root", str(self.clone), "--project", "ghost", "--json")
        self.assertEqual(code, 1)


class BriefUnreadableTests(BriefCase):
    def test_unreadable_records_are_reported_not_dropped(self) -> None:
        good = self.handoff(1, "readable")
        bad = self.handoff(2, "x")
        bad.write_bytes(bad.read_bytes() + b"\xff\xfe broken\n")
        candidate = self.candidate(3)
        candidate.write_bytes(candidate.read_bytes() + b"\xff\n")
        session = self.session(4)
        session.write_bytes(session.read_bytes() + b"\xff\n")
        result = brief(self.wiki, "api")
        self.assertEqual([item["path"] for item in result["sections"]["handoffs"]["items"]], [good.relative_to(self.clone).as_posix()])
        unreadable = [warning for warning in result["warnings"] if "unreadable" in warning]
        for path in (bad, candidate, session):
            self.assertTrue(any(path.relative_to(self.clone).as_posix() in warning for warning in unreadable), path)
        self.assertIn("unreadable", result["text"])

    def test_freshness_check_failures_are_warned(self) -> None:
        from llm_wiki.sync import SyncError

        with patch("llm_wiki.brief.owns_repository", side_effect=SyncError("git_timeout", "git rev-parse timed out after 120s.")):
            result = brief(self.wiki, "api")
        self.assertTrue(any("freshness" in warning and "git_timeout" in warning for warning in result["warnings"]))


    def test_many_unreadable_records_are_folded_and_brief_still_fits(self) -> None:
        for index in range(40):
            path = self.handoff(index, "x")
            path.write_bytes(path.read_bytes() + b"\xff\n")
        result = brief(self.wiki, "api", 2048)
        self.assertLessEqual(len(result["text"].encode("utf-8")), 2048)
        self.assertEqual(len(result["unreadable"]), 40)
        self.assertTrue(any("35 more" in warning for warning in result["warnings"]))

    def test_no_git_is_silent_like_a_plain_wiki(self) -> None:
        from llm_wiki.sync import SyncError

        with patch("llm_wiki.brief.owns_repository", side_effect=SyncError("guard", "git is not installed.", reason="no_git")):
            result = brief(self.wiki, "api")
        self.assertEqual(result["warnings"], [])


if __name__ == "__main__":
    unittest.main()
